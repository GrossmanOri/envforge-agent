"""The repair loop, driven by fakes, plus one end-to-end run against real Docker.

The loop's only judgement is whether another attempt could possibly help, so almost
every test here is the same shape: hand it a failure, assert whether it spent an
attempt on it.
"""

from pathlib import Path

import pytest

from envforge.agent import (
    EVIDENCE_LIMIT, LANGUAGES, SCRIPT_LIMIT, Agent, Event, Outcome, bound,
    default_dockerfile, language_for,
)
from envforge.llm import Call, InvalidArguments, Refused, Truncated
from envforge.sandbox import BuildResult, DockerSandbox, Limits, RunResult

GOOD = "FROM python:3.12-slim\nCOPY s.py /app/s.py\nENTRYPOINT [\"python\", \"/app/s.py\"]\n"
def ALLOW(dockerfile, base_image, allowed_files):   # the sitting 6 gate, stubbed open
    return None


def DENY(dockerfile, base_image, allowed_files):
    return "FROM is not pinned"


class RecordingGate:
    """Allows everything and remembers what it was handed."""

    def __init__(self):
        self.seen = []

    def __call__(self, dockerfile, base_image, allowed_files):
        self.seen.append((dockerfile, base_image, allowed_files))
        return None


def _call(dockerfile=GOOD, base="python:3.12-slim"):
    return Call(arguments={"dockerfile": dockerfile, "base_image": base},
                model="fake", input_tokens=1, output_tokens=1, request={}, response={})


class FakeLLM:
    """Replays a queue. An exception in the queue is raised instead of returned."""

    def __init__(self, *replies):
        self.model, self.queue, self.prompts = "fake", list(replies), []

    def call(self, system, user, tool):
        self.prompts.append(user)
        reply = self.queue.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


class FakeSandbox:
    def __init__(self, builds=(), runs=()):
        self.builds, self.runs = list(builds), list(runs)
        self.built = []

    def build(self, dockerfile, script, tag):
        self.built.append(dockerfile)
        return self.builds.pop(0) if self.builds else _build()

    def run(self, image, args=()):
        return self.runs.pop(0) if self.runs else _run()


def _build(ok=True, log="", exit_code=0):
    return BuildResult(ok=ok, image="img", exit_code=exit_code, log=log,
                       truncated=False, timed_out=False, seconds=0.1)


def _run(exit_code=0, stdout="", stderr="", timed_out=False, start_error=""):
    return RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr,
                     truncated=False, timed_out=timed_out, seconds=0.1,
                     start_error=start_error)


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "s.py"
    path.write_text("print('hello')\n")
    return path


def drive(agent, script, args=()):
    events = list(agent.run(script, "python", args))
    return events, [e.kind for e in events], events[-1].data["outcome"]


# --- the happy path -------------------------------------------------------------------

def test_one_attempt_when_nothing_fails(script):
    llm = FakeLLM(_call())
    agent = Agent(llm, FakeSandbox(), ALLOW)
    events, kinds, outcome = drive(agent, script)
    assert kinds == ["asking", "wrote", "building", "running", "finished"]
    assert outcome.ok and outcome.attempts == 1 and outcome.used_fallback is False
    assert outcome.dockerfile == GOOD and len(outcome.calls) == 1


# --- what spends an attempt, and what does not ---------------------------------------

def test_a_build_failure_is_repaired_with_the_bounded_log(script):
    llm = FakeLLM(_call("FROM python:3.12-slim\nRUN nope\n"), _call())
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="x" * 20_000), _build()])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert kinds.count("building") == 2 and outcome.ok and outcome.attempts == 2
    repair = llm.prompts[1]
    assert "your previous Dockerfile" in repair and "RUN nope" in repair
    assert "characters removed" in repair and len(repair) < 20_000


def test_the_gate_rejection_is_the_evidence_and_nothing_was_built(script):
    llm = FakeLLM(_call("FROM python\n"), _call())
    sandbox = FakeSandbox()
    gate = lambda d, b, f: "FROM is not pinned" if "slim" not in d else None
    events, kinds, outcome = drive(Agent(llm, sandbox, gate), script)
    assert kinds[:3] == ["asking", "wrote", "gate_rejected"]
    assert sandbox.built == [GOOD]          # the rejected one never reached the daemon
    assert "rejected before it was built" in llm.prompts[1]
    assert outcome.ok


@pytest.mark.parametrize("exit_code", [126, 127])
def test_a_container_that_never_started_is_the_dockerfiles_fault(script, exit_code):
    llm = FakeLLM(_call("FROM python:3.12-slim\nENTRYPOINT [\"nope\"]\n"), _call())
    sandbox = FakeSandbox(runs=[
        _run(exit_code=exit_code, start_error='exec: "nope": no such file or directory'),
        _run(),
    ])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert "exec_failed" in kinds and outcome.ok and outcome.attempts == 2
    assert "never started its command" in llm.prompts[1]
    assert '"nope"' in llm.prompts[1]


@pytest.mark.parametrize("exit_code", [126, 127])
def test_a_script_exiting_126_to_look_broken_does_not_buy_a_repair(script, exit_code):
    """The exit code is identical to a real exec failure. The daemon's own account
    of why the process never started is the only thing that separates them, and a
    script that can choose an exit code has already started."""
    llm = FakeLLM(_call())
    sandbox = FakeSandbox(runs=[_run(exit_code=exit_code, start_error="")])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert "exec_failed" not in kinds
    assert outcome.attempts == 1 and outcome.ok and len(outcome.calls) == 1


@pytest.mark.parametrize("exit_code, timed_out", [(0, False), (1, False), (137, False), (None, True)])
def test_the_script_choosing_how_to_die_is_not_a_repair(script, exit_code, timed_out):
    """0 is success, 1 is the script raising, 137 is the kernel killing it, and a
    timeout means the run happened and took too long. All four are observed
    behaviour, which is the verdict's problem in sitting 7, not the loop's."""
    llm = FakeLLM(_call())
    sandbox = FakeSandbox(runs=[_run(exit_code=exit_code, timed_out=timed_out)])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert kinds.count("asking") == 1 and outcome.attempts == 1 and outcome.ok
    assert outcome.run.exit_code == exit_code


@pytest.mark.parametrize("failure", [
    InvalidArguments("field 'dockerfile' should be string"),
    Truncated("hit the ceiling"),
])
def test_an_unusable_reply_spends_an_attempt_and_says_why(script, failure):
    """Bug found 2026-08-23. There is no previous Dockerfile to repair on the first
    attempt, so the code fell back to the opening template, which has no slot for
    the evidence. It was computed and then dropped, and the model got an identical
    prompt twice."""
    llm = FakeLLM(failure, _call())
    events, kinds, outcome = drive(Agent(llm, FakeSandbox(), ALLOW), script)
    assert kinds[:2] == ["asking", "unusable_reply"] and outcome.attempts == 2
    assert llm.prompts[0] != llm.prompts[1]
    assert "could not be used" in llm.prompts[1]
    assert str(failure) in llm.prompts[1]
    assert "previous Dockerfile" not in llm.prompts[1]   # there was not one


def test_the_loop_gives_up_honestly(script):
    llm = FakeLLM(*[_call() for _ in range(3)])
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom") for _ in range(3)])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert outcome.ok is False and outcome.attempts == 3
    assert "no Dockerfile worked" in outcome.reason and outcome.run is None


# --- refusals -------------------------------------------------------------------------

def test_a_refusal_retries_without_spending_a_repair_attempt(script):
    llm = FakeLLM(Refused("declined", reason={"category": "cyber"}), _call(), _call())
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom"), _build()])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    # Three asks, two attempts. The refusal cost a model call and nothing else;
    # the build failure is the only thing that consumed an attempt.
    assert kinds.count("asking") == 3 and kinds.count("refused") == 1
    assert outcome.attempts == 2 and len(outcome.calls) == 2 and outcome.ok
    assert outcome.refusals == [{"category": "cyber"}]


def test_two_refusals_fall_back_to_our_own_dockerfile(script):
    llm = FakeLLM(Refused("no", reason="a"), Refused("still no", reason="b"))
    sandbox = FakeSandbox()
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert kinds.count("refused") == 2 and "fell_back" in kinds
    assert outcome.ok and outcome.used_fallback and outcome.refusals == ["a", "b"]
    assert sandbox.built == [default_dockerfile("python", "s.py")]
    assert llm.queue == []                  # it was never asked a third time


def test_the_fallback_is_gated_like_everything_else(script):
    """One path from a Dockerfile string to the daemon, whoever wrote it."""
    llm = FakeLLM(Refused("no", reason="a"), Refused("no", reason="b"))
    sandbox = FakeSandbox()
    events, kinds, outcome = drive(Agent(llm, sandbox, DENY), script)
    assert kinds[-2:] == ["gate_rejected", "finished"]
    assert sandbox.built == [] and outcome.ok is False
    assert "our fallback Dockerfile was rejected" in outcome.reason


# --- what the gate is handed -----------------------------------------------------------

def test_the_gate_receives_the_declared_base_image_and_the_allowed_filenames(script):
    """CLAUDE.md says base_image is declared separately so the gate can check it
    without parsing. Until 2026-08-23 the gate was called with the dockerfile alone,
    so a model could declare one image and write FROM another with nothing to notice."""
    gate = RecordingGate()
    llm = FakeLLM(_call(base="python:3.12-slim"))
    drive(Agent(llm, FakeSandbox(), gate), script)
    dockerfile, base_image, allowed = gate.seen[0]
    assert base_image == "python:3.12-slim"
    assert allowed == frozenset({"s.py"})


def test_the_gate_can_catch_a_declared_image_that_is_not_the_one_written(script):
    """The reason the field is passed at all: the two can disagree."""
    def mismatch(dockerfile, base_image, allowed_files):
        return None if f"FROM {base_image}" in dockerfile else "FROM does not match base_image"

    llm = FakeLLM(_call("FROM ubuntu:22.04\nENTRYPOINT [\"true\"]\n", base="python:3.12-slim"),
                  _call())
    events, kinds, outcome = drive(Agent(llm, FakeSandbox(), mismatch), script)
    assert kinds[2] == "gate_rejected" and outcome.ok and outcome.attempts == 2


def test_the_gate_is_handed_the_fallbacks_own_base_image(script):
    gate = RecordingGate()
    llm = FakeLLM(Refused("no", reason="a"), Refused("no", reason="b"))
    drive(Agent(llm, FakeSandbox(), gate), script)
    assert gate.seen[0][1] == "python:3.12-slim"


# --- the fallback has no next move ------------------------------------------------------

def test_a_fallback_that_does_not_build_stops_instead_of_asking_again(script):
    """Bug found 2026-08-23. This path used to set dockerfile to None and re-ask the
    model, which the refusal policy rules out, and the gate check would then have
    reported a model-written Dockerfile as our own."""
    llm = FakeLLM(Refused("no", reason="a"), Refused("no", reason="b"))
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="no such image")])
    events, kinds, outcome = drive(Agent(llm, sandbox, ALLOW), script)
    assert kinds[-2:] == ["build_failed", "finished"]
    assert outcome.ok is False and outcome.reason == "our fallback Dockerfile did not build"
    assert llm.queue == []                  # never asked a third time
    assert outcome.build is not None and outcome.used_fallback


# --- reading the language off the filename ------------------------------------------------

@pytest.mark.parametrize("filename, expected", [
    ("s.py", "python"), ("S.PY", "python"),
    ("s.sh", "bash"), ("s.bash", "bash"),
    ("s.c", None), ("s.rb", None), ("Makefile", None), ("s", None),
    ("s.py.txt", None),          # the last suffix is what counts, as it does to a shell
])
def test_language_comes_from_the_extension_and_nothing_else(filename, expected):
    """Only the extension, deliberately. A shebang would be more accurate and would
    mean reading attacker-controlled content to make the decision, and the override
    flag the CLI will carry covers the cases an extension cannot answer."""
    assert language_for(Path(filename)) == expected


def test_every_language_in_the_table_is_reachable_from_a_filename():
    """A language nobody can name is a language nobody can run."""
    for name, language in LANGUAGES.items():
        assert language_for(Path("s" + language.extensions[0])) == name


def test_the_table_is_the_only_place_a_language_is_defined():
    """One table, so adding a language is one entry rather than three that can
    disagree. The gate is not one of them on purpose: it decides what may run during
    a build and has no business knowing what language anything is."""
    from envforge import gate
    assert "LANGUAGES" not in dir(gate)
    for name, language in LANGUAGES.items():
        dockerfile = default_dockerfile(name, "s" + language.extensions[0])
        assert language.base_image in dockerfile and language.command in dockerfile


# --- languages we do not handle ----------------------------------------------------------

def test_an_unsupported_language_is_refused_at_the_door(tmp_path):
    """It used to run. Nothing validated the language, so the model was asked and
    usually answered, and the README's claim of Python and Bash only was not enforced
    anywhere in the code."""
    script = tmp_path / "s.rb"
    script.write_text('puts "hi"\n')
    llm = FakeLLM()                       # never consulted
    events = list(Agent(llm, FakeSandbox(), ALLOW).run(script, "ruby"))
    outcome = events[-1].data["outcome"]
    assert [e.kind for e in events] == ["finished"]
    assert outcome.ok is False and "not 'ruby'" in outcome.reason
    assert "bash, python" in outcome.reason


def test_an_unsupported_language_used_to_crash_on_a_refusal(tmp_path):
    """The specific bug: two refusals reached default_dockerfile, which raises for a
    language it has no base image for, and the ValueError escaped the generator."""
    script = tmp_path / "s.rb"
    script.write_text('puts "hi"\n')
    agent = Agent(FakeLLM(Refused("no", reason="a"), Refused("no", reason="b")),
                  FakeSandbox(), ALLOW)
    outcome = list(agent.run(script, "ruby"))[-1].data["outcome"]
    assert outcome.ok is False           # an outcome, not an exception


def test_bash_is_supported_and_says_so(tmp_path):
    """Claimed in the README since the first commit and exercised by nothing until now."""
    script = tmp_path / "s.sh"
    script.write_text("echo hi\n")
    dockerfile = default_dockerfile("bash", "s.sh")
    assert "FROM debian:12-slim" in dockerfile
    assert 'ENTRYPOINT ["bash", "/app/s.sh"]' in dockerfile
    llm = FakeLLM(_call(dockerfile, base="debian:12-slim"))
    outcome = list(Agent(llm, FakeSandbox(), ALLOW).run(script, "bash"))[-1].data["outcome"]
    assert outcome.ok


# --- the pieces ------------------------------------------------------------------------

def test_bound_keeps_both_ends_and_says_how_much_it_cut():
    text = "A" * 100 + "B" * 100
    cut = bound(text, 100)
    assert cut.startswith("A" * 50) and cut.endswith("B" * 50) and "100 characters removed" in cut
    assert bound("short", 100) == "short"


def test_the_script_is_bounded_before_it_reaches_the_prompt(tmp_path):
    script = tmp_path / "s.py"
    script.write_text("#" * (SCRIPT_LIMIT * 3))
    llm = FakeLLM(_call())
    list(Agent(llm, FakeSandbox(), ALLOW).run(script, "python"))
    assert "characters removed" in llm.prompts[0]
    assert len(llm.prompts[0]) < SCRIPT_LIMIT * 2


def test_the_fallback_runs_the_script_as_argv_not_as_a_shell_string():
    dockerfile = default_dockerfile("python", "s.py")
    assert 'ENTRYPOINT ["python", "/app/s.py"]' in dockerfile
    assert "\\\n" not in dockerfile          # the gate will ban continuations
    with pytest.raises(ValueError):
        default_dockerfile("ruby", "s.rb")


# --- one real run ----------------------------------------------------------------------

@pytest.mark.docker
def test_the_whole_loop_against_a_real_daemon(tmp_path):
    """The fakes prove the decisions. This proves the wiring."""
    script = tmp_path / "hello.py"
    script.write_text("import sys; print('from inside'); sys.exit(3)\n")
    dockerfile = default_dockerfile("python", "hello.py")
    agent = Agent(FakeLLM(_call(dockerfile)),
                  DockerSandbox(Limits(run_timeout=60.0)), ALLOW)
    events = list(agent.run(script, "python"))
    outcome = events[-1].data["outcome"]
    assert outcome.ok and outcome.attempts == 1
    assert outcome.run.exit_code == 3 and "from inside" in outcome.run.stdout
