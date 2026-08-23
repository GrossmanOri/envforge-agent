"""Two kinds of test here.

The unmarked ones read the argv the code builds and never touch Docker: they are the
enforcement of ARCHITECTURE.md invariants 4, 5 and 8, so removing a hardening flag turns
red instead of passing review. The ones marked `docker` run the real thing.
"""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from envforge.sandbox import (
    OUTPUT_LIMIT,
    DockerSandbox,
    Limits,
    SandboxError,
    _bound,
    build_argv,
    run_argv,
)

LIMITS = Limits(memory="128m", pids=64, cpus="0.5", run_timeout=20.0)


def _flag_pairs(argv: list[str]) -> set[tuple[str, str]]:
    """Flags and their values as adjacent pairs, so a value cannot drift onto a
    different flag and still satisfy a plain `in argv` check."""
    return {(a, b) for a, b in zip(argv, argv[1:]) if a.startswith("--")}


def _sample_run_argv(args=()) -> list[str]:
    return run_argv("img:test", "envforge-abc", Path("/tmp/cid"), LIMITS, args)


# --- invariants, no Docker ---------------------------------------------------------


def test_build_has_network():
    argv = build_argv("img:test", Path("/ctx"))
    assert "--network" not in argv, "build needs the network for apt and pip"


def test_run_has_no_network():
    assert ("--network", "none") in _flag_pairs(_sample_run_argv())


@pytest.mark.parametrize(
    "pair",
    [
        ("--memory", "128m"),
        ("--memory-swap", "128m"),
        ("--pids-limit", "64"),
        ("--cpus", "0.5"),
        ("--cap-drop", "ALL"),
        ("--security-opt", "no-new-privileges:true"),
        ("--user", "65534:65534"),
    ],
)
def test_run_carries_every_hardening_flag(pair):
    assert pair in _flag_pairs(_sample_run_argv())


def test_run_is_read_only_with_a_tmpfs():
    argv = _sample_run_argv()
    assert "--read-only" in argv
    tmpfs = dict(_flag_pairs(argv))["--tmpfs"]
    assert tmpfs.startswith("/tmp:") and "size=" in tmpfs


def test_container_is_named_so_it_can_be_removed_by_name():
    assert ("--name", "envforge-abc") in _flag_pairs(_sample_run_argv())


def test_script_args_land_after_the_image():
    """Invariant 8. An argument that looks like a docker flag must reach the container
    as argv, never docker as a flag."""
    argv = _sample_run_argv(["--privileged", "-v", "/:/host"])
    image_at = argv.index("img:test")
    assert argv[image_at + 1 :] == ["--privileged", "-v", "/:/host"]


def test_argv_is_a_list_of_separate_words():
    """A string command re-splits on spaces and arrived as one flag in sitting 1."""
    assert all(isinstance(word, str) and " " not in word for word in _sample_run_argv())


def test_bound_keeps_head_and_tail_and_flags_the_cut():
    text, truncated = _bound(b"A" * 10 + b"B" * OUTPUT_LIMIT + b"C" * 10)
    assert truncated
    assert text.startswith("AAAA") and text.endswith("CCCC")
    assert len(text) < OUTPUT_LIMIT + 200


def test_bound_leaves_short_output_alone():
    assert _bound(b"hello") == ("hello", False)


# --- the real thing ----------------------------------------------------------------

PROBE = '''\
import os, socket, sys
mode = sys.argv[1] if len(sys.argv) > 1 else "ok"
if mode == "net":
    socket.create_connection(("1.1.1.1", 443), timeout=5)
elif mode == "mem":
    buf = bytearray(512 * 1024 * 1024)
    for i in range(0, len(buf), 4096):
        buf[i] = 1
elif mode == "sleep":
    import time; time.sleep(120)
elif mode == "exit125":
    sys.exit(125)
elif mode == "exit127":
    sys.exit(127)
elif mode == "flood":
    sys.stdout.write("x" * 200000)
print("uid", os.getuid(), "argv", sys.argv[1:])
'''

DOCKERFILE = """\
FROM python:3.12-slim
COPY probe.py /app/probe.py
ENTRYPOINT ["python", "-u", "/app/probe.py"]
"""

@pytest.fixture(scope="module")
def image(tmp_path_factory):
    script = tmp_path_factory.mktemp("probe") / "probe.py"
    script.write_text(PROBE)
    sandbox = DockerSandbox(LIMITS)
    result = sandbox.build(DOCKERFILE, script, "envforge-test:probe")
    assert result.ok, result.log
    yield "envforge-test:probe"
    sandbox.remove_image("envforge-test:probe")


BROKEN_ENTRYPOINT = """\
FROM python:3.12-slim
COPY probe.py /app/probe.py
ENTRYPOINT ["/does-not-exist"]
"""


@pytest.fixture(scope="module")
def broken_image(tmp_path_factory):
    """An image whose command cannot be executed. It builds fine; it cannot start."""
    script = tmp_path_factory.mktemp("broken") / "probe.py"
    script.write_text(PROBE)
    sandbox = DockerSandbox(LIMITS)
    result = sandbox.build(BROKEN_ENTRYPOINT, script, "envforge-test:broken")
    assert result.ok, result.log
    yield "envforge-test:broken"
    sandbox.remove_image("envforge-test:broken")


@pytest.fixture
def sandbox():
    return DockerSandbox(LIMITS)


@pytest.mark.docker
def test_runs_as_nobody(sandbox, image):
    result = sandbox.run(image)
    assert result.exit_code == 0
    assert "uid 65534" in result.stdout


@pytest.mark.docker
def test_args_reach_the_script_not_docker(sandbox, image):
    result = sandbox.run(image, ["--privileged"])
    assert result.exit_code == 0
    assert "'--privileged'" in result.stdout


@pytest.mark.docker
def test_network_is_gone(sandbox, image):
    result = sandbox.run(image, ["net"])
    assert result.exit_code == 1  # the script raised, the cage did not kill it
    assert "Network is unreachable" in result.stderr


@pytest.mark.docker
def test_memory_cap_kills_the_process(sandbox, image):
    """The cap has to be enforced by something the script cannot argue with.

    Both facts are asserted at once so a failure prints both. 137 alone proves
    nothing, since a script can call exit(137) to imitate a kill. A MemoryError
    means Python was refused an allocation and stayed alive to handle it, which
    is catchable, so the script kept running. The two together in one run is the
    worst case of all: killed, but only after executing code past the limit.
    """
    result = sandbox.run(image, ["mem"])
    observed = (result.exit_code, "MemoryError" in result.stderr)
    assert observed == (137, False), (
        f"(exit_code, MemoryError in stderr) was {observed}, wanted (137, False). "
        f"stderr tail: {result.stderr[-300:]!r}"
    )


@pytest.mark.docker
def test_timeout_reports_itself_and_leaves_no_container(sandbox, image):
    sandbox.limits = replace(LIMITS, run_timeout=3.0)
    result = sandbox.run(image, ["sleep"])
    assert result.timed_out and result.exit_code is None
    survivors = subprocess.run(
        ["docker", "ps", "--all", "--quiet", "--filter", "name=envforge-"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert survivors == "", "a container outlived the run"


@pytest.mark.docker
def test_a_script_exiting_125_is_data_not_our_bug(sandbox, image):
    """125 is docker's own rejection code, so a hostile script can forge it."""
    assert sandbox.run(image, ["exit125"]).exit_code == 125


@pytest.mark.docker
def test_a_rejected_docker_command_raises(image):
    broken = DockerSandbox(Limits(memory="not-a-size"))
    with pytest.raises(SandboxError):
        broken.run(image)


@pytest.mark.docker
def test_output_is_bounded(sandbox, image):
    result = sandbox.run(image, ["flood"])
    assert result.truncated
    assert len(result.stdout) < OUTPUT_LIMIT + 200


@pytest.mark.docker
def test_a_broken_dockerfile_fails_the_build_without_raising(sandbox, tmp_path):
    script = tmp_path / "probe.py"
    script.write_text(PROBE)
    result = sandbox.build("FROM python:3.12-slim\nRUN exit 7\n", script, "envforge-test:bad")
    assert not result.ok and result.exit_code != 0


@pytest.mark.docker
def test_a_broken_entrypoint_leaves_the_daemons_own_account_of_why(sandbox, broken_image):
    """The witness that separates a broken image from a script imitating one.

    Both exit 127. Only one of them has a start_error, because a script that can
    choose its exit code has already started, and this field is written by the
    daemon about a process that never did.
    """
    result = sandbox.run(broken_image)
    assert result.exit_code == 127
    assert "does-not-exist" in result.start_error


@pytest.mark.docker
def test_a_script_exiting_127_leaves_no_such_account(sandbox, image):
    result = sandbox.run(image, ["exit127"])
    assert result.exit_code == 127
    assert result.start_error == ""
