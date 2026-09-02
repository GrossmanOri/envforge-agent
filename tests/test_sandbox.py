"""Two kinds of test here.

The unmarked ones read the argv the code builds and never touch Docker: they are the
enforcement of ARCHITECTURE.md invariants 4, 5 and 8, so removing a hardening flag turns
red instead of passing review. The ones marked `docker` run the real thing.
"""

from __future__ import annotations

import subprocess
import time
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from envforge.sandbox import (container_exists, remove_container,
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
    """A string command re-splits on spaces, and once arrived as a single flag."""
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
def image():
    """No file on disk anywhere. The build context is assembled from contents, which is
    what makes the bytes the model saw and the bytes the container runs the same bytes."""
    sandbox = DockerSandbox(LIMITS)
    result = sandbox.build(DOCKERFILE, {"probe.py": PROBE}, "envforge-test:probe")
    assert result.ok, result.log
    yield "envforge-test:probe"
    sandbox.remove_image("envforge-test:probe")


BROKEN_ENTRYPOINT = """\
FROM python:3.12-slim
COPY probe.py /app/probe.py
ENTRYPOINT ["/does-not-exist"]
"""


@pytest.fixture(scope="module")
def broken_image():
    """An image whose command cannot be executed. It builds fine; it cannot start."""
    sandbox = DockerSandbox(LIMITS)
    result = sandbox.build(BROKEN_ENTRYPOINT, {"probe.py": PROBE}, "envforge-test:broken")
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
def test_timeout_stops_the_container_but_keeps_it_as_evidence(sandbox, image):
    """Nothing keeps executing, and the container is still there.

    This asserted that no container outlived the run, which was true and was the bug:
    removing it here is what let a resumed run execute an untrusted sample twice, because
    the container is the only durable proof that an attempt already ran. Stopping is the
    safety property, removal happens once the result is written down.
    """
    sandbox.limits = replace(LIMITS, run_timeout=3.0)
    name = "envforge-evidence-test"
    result = sandbox.run(image, ["sleep"], name=name)
    assert result.timed_out and result.exit_code is None

    running = subprocess.run(
        ["docker", "ps", "--quiet", "--filter", f"name=^{name}$"],
        capture_output=True, text=True, check=True).stdout.strip()
    assert running == "", "the container was still running after the run returned"
    assert container_exists(name), "the evidence that the sample ran was thrown away"

    remove_container(name)
    assert not container_exists(name)


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
    result = sandbox.build("FROM python:3.12-slim\nRUN exit 7\n",
                           {"probe.py": PROBE}, "envforge-test:bad")
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


def test_build_asserts_its_precondition_and_should_never_fire_in_practice():
    """This is not a second copy of the workspace's rule, and the distinction matters.

    The workspace owns what a legal filename is: it decides once at ingestion, raises
    WorkspaceError, and its message is aimed at a person. `build` only states what it
    requires to be correct, because it writes these names into a directory. Reaching
    this line means our own code broke a contract, which is what SandboxError means
    everywhere else in this file: our command was wrong, never anything the script did.

    Two checks are fine when one is the rule and the other is a precondition. Two checks
    are a problem when a reader cannot tell which is which, so the error type, the
    message and this name all have to say so."""
    sandbox = DockerSandbox(LIMITS)
    for name in ["../escape.py", "sub/dir.py", "..", ".", ""]:
        with pytest.raises(SandboxError, match="bare filename"):
            sandbox.build("FROM python:3.12-slim\n", {name: "x"}, "envforge-test:never")


@pytest.mark.parametrize("name", ["Dockerfile", "dockerfile", "DOCKERFILE",
                                  ".dockerignore", ".DockerIgnore"])
def test_a_context_file_may_not_be_one_the_build_itself_interprets(name):
    """Found 2026-08-25 and verified against the daemon before this line existed.

    The files loop runs after the gated Dockerfile is written into the context, so a
    file called Dockerfile overwrote it and the container ran instructions the gate had
    never seen. Both `Dockerfile` and `dockerfile` built and ran, the second because a
    case-insensitive filesystem collides them.

    This is not a directory escape, it is a complete bypass of the only check there is,
    and it is exactly the failure the precondition above claims to prevent."""
    sandbox = DockerSandbox(LIMITS)
    with pytest.raises(SandboxError, match="build itself"):
        sandbox.build("FROM python:3.12-slim\n", {name: "FROM evil"}, "envforge-test:never")


# --- the sweep, and what it must not touch --------------------------------------------

def test_labels_carry_the_run_and_when_it_started():
    from envforge.sandbox import RUN_LABEL, STARTED_LABEL, labels_for

    made = labels_for("abc123")
    assert made[RUN_LABEL] == "abc123"
    # A number, so the sweep never parses a date. Docker prints creation times in a
    # human format that varies with locale and version, and an age rule built on
    # parsing that is a rule that breaks on somebody else's machine.
    assert int(made[STARTED_LABEL]) > 0


def test_the_labels_reach_the_argv_for_both_a_build_and_a_run():
    from envforge.sandbox import build_argv, run_argv

    labels = {"envforge.run": "r1", "envforge.started": "100"}
    build = build_argv("tag", Path("/ctx"), labels)
    assert build[build.index("--label") + 1] == "envforge.run=r1"
    assert build.count("--label") == 2

    run = run_argv("img", "name", Path("/cid"), LIMITS, (), labels)
    assert run.count("--label") == 2
    # And after the image name there is nothing but the script's own arguments, so a
    # label can never be read by docker as a flag (invariant 8).
    assert "--label" not in run[run.index("img"):]


def _sweep(monkeypatch, objects, keep="", older_than=3600.0):
    """Drive `sweep` against canned docker output, with no daemon.

    Through `monkeypatch` rather than by assigning module globals directly. An earlier
    version of these tests replaced `remove_image` and never put it back, which leaves
    every later test in the process running against a stub. A test that breaks other
    tests is worse than no test, and it only shows up when the collection order changes.
    """
    import envforge.sandbox as sandbox_module

    dropped: list[str] = []
    listings = {"container": [o for o in objects if o[0] == "container"],
                "image": [o for o in objects if o[0] == "image"]}
    monkeypatch.setattr(sandbox_module, "_ours",
                        lambda kind, argv: [(oid, run, started)
                                            for _, oid, run, started in listings[kind]])
    monkeypatch.setattr(sandbox_module, "remove_container", dropped.append)
    monkeypatch.setattr(sandbox_module, "remove_image", dropped.append)
    return sandbox_module.sweep(keep=keep, older_than=older_than), dropped


def test_the_sweep_skips_the_run_that_asked_for_it(monkeypatch):
    """A run must not delete the image it is about to run."""
    _, dropped = _sweep(monkeypatch,
                        [("container", "mine", "run-a", 0),
                         ("container", "theirs", "run-b", 0)], keep="run-a")
    assert dropped == ["theirs"]


def test_the_sweep_leaves_anything_young_alone(monkeypatch):
    """A second envforge may be running right now and its objects are labelled exactly
    like ours. There is no way to ask whether that process is alive, so age stands in."""
    now = int(time.time())
    _, dropped = _sweep(monkeypatch,
                        [("container", "fresh", "run-b", now),
                         ("container", "stale", "run-c", now - 7200)])
    assert dropped == ["stale"]


def test_the_sweep_reports_what_it_removed(monkeypatch):
    removed, _ = _sweep(monkeypatch,
                        [("image", "deadbeefcafe0000", "run-x" + "y" * 27, 0)])
    assert removed == ["image deadbeefcafe from run run-xyyy"]


def test_the_sweep_removes_images_as_well_as_containers(monkeypatch):
    _, dropped = _sweep(monkeypatch, [("container", "c1", "old", 0),
                                      ("image", "i1", "old", 0)])
    assert sorted(dropped) == ["c1", "i1"]


@pytest.mark.docker
def test_a_finished_run_leaves_no_image_or_container_of_its_own(sandbox):
    """The whole lifecycle against a real daemon: label, build, run, remove."""
    from envforge.sandbox import RUN_LABEL, labels_for, remove_image

    run_id = "sweeptest" + uuid.uuid4().hex[:8]
    labels = labels_for(run_id)
    tag = f"envforge-{run_id}:attempt1"
    name = f"envforge-{run_id}-attempt1"
    # An image already on the machine. Pulling a new one makes these tests depend on
    # registry access, which CI has and a sandboxed checkout may not, and the failure is
    # a silent hang at the build timeout rather than a clear error.
    dockerfile = 'FROM python:3.12-slim\nCMD ["true"]\n'

    build = sandbox.build(dockerfile, {}, tag, labels)
    assert build.ok, build.log
    assert _labelled("image", run_id), "the image did not carry the run label"

    sandbox.run(build.image, (), name=name, labels=labels)
    assert container_exists(name), "the container is the evidence and it was removed"
    assert _labelled("container", run_id)

    remove_container(name)
    remove_image(tag)
    assert not _labelled("image", run_id) and not _labelled("container", run_id)


@pytest.mark.docker
def test_the_sweep_collects_a_crashed_run_and_spares_a_live_one(sandbox):
    """Two runs' objects on one machine: one abandoned and old, one fresh.

    The age guard is the only thing standing between this sweep and a concurrent
    envforge, because both label their work identically and neither can ask whether the
    other process is alive.
    """
    from envforge.sandbox import RUN_LABEL, STARTED_LABEL, remove_image, sweep

    crashed = "crashed" + uuid.uuid4().hex[:8]
    live = "live" + uuid.uuid4().hex[:8]
    dockerfile = 'FROM python:3.12-slim\nCMD ["true"]\n'
    # The crashed run's objects claim to be two hours old; the live run's are now.
    old = {RUN_LABEL: crashed, STARTED_LABEL: str(int(time.time()) - 7200)}
    now = {RUN_LABEL: live, STARTED_LABEL: str(int(time.time()))}

    for run_id, labels in ((crashed, old), (live, now)):
        built = sandbox.build(dockerfile, {}, f"envforge-{run_id}:attempt1", labels)
        assert built.ok, built.log
        sandbox.run(built.image, (), name=f"envforge-{run_id}-attempt1", labels=labels)

    try:
        removed = sweep(keep="", older_than=3600.0)
        assert any(crashed[:8] in line for line in removed), removed
        assert not _labelled("container", crashed) and not _labelled("image", crashed)
        # The concurrent run is untouched, which is the point of the age guard.
        assert _labelled("container", live) and _labelled("image", live)
    finally:
        remove_container(f"envforge-{live}-attempt1")
        remove_image(f"envforge-{live}:attempt1")
        remove_container(f"envforge-{crashed}-attempt1")
        remove_image(f"envforge-{crashed}:attempt1")


@pytest.mark.docker
def test_the_sweep_never_touches_the_run_that_called_it(sandbox):
    from envforge.sandbox import RUN_LABEL, STARTED_LABEL, remove_image, sweep

    mine = "mine" + uuid.uuid4().hex[:8]
    labels = {RUN_LABEL: mine, STARTED_LABEL: str(int(time.time()) - 7200)}
    built = sandbox.build('FROM python:3.12-slim\nCMD ["true"]\n', {},
                          f"envforge-{mine}:attempt1", labels)
    assert built.ok, built.log
    try:
        sweep(keep=mine, older_than=3600.0)
        assert _labelled("image", mine), "the sweep deleted its own caller's image"
    finally:
        remove_image(f"envforge-{mine}:attempt1")


def _labelled(kind: str, run_id: str) -> bool:
    """Whether any object of this kind carries this run's label."""
    from envforge.sandbox import RUN_LABEL

    listing = (["docker", "ps", "--all", "--quiet"] if kind == "container"
               else ["docker", "image", "ls", "--quiet"])
    found = subprocess.run(listing + ["--filter", f"label={RUN_LABEL}={run_id}"],
                           capture_output=True, text=True, check=True)
    return bool(found.stdout.strip())
