"""The repair loop, driven by fakes, plus one end-to-end run against real Docker.

The loop's only judgement is whether another attempt could possibly help, so almost
every test here is the same shape: hand it a failure, assert whether it spent an
attempt on it.
"""

from pathlib import Path

import pytest

from envforge.agent import (
    EVIDENCE_LIMIT, SCRIPT_LIMIT, Agent, Event, Outcome, bound, default_dockerfile,
)
from envforge.llm import Call, InvalidArguments, Refused, Truncated
from envforge.sandbox import BuildResult, DockerSandbox, Limits, RunResult

GOOD = "FROM python:3.12-slim\nCOPY s.py /app/s.py\nENTRYPOINT [\"python\", \"/app/s.py\"]\n"
ALLOW = lambda dockerfile: None                     # the sitting 6 gate, stubbed open
DENY = lambda dockerfile: "FROM is not pinned"


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
    events, kinds, outcome = drive(Agent(llm, sandbox, lambda d: DENY(d) if "\n" in d and "slim" not in d else None), script)
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
def test_an_unusable_reply_spends_an_attempt(script, failure):
    llm = FakeLLM(failure, _call())
    events, kinds, outcome = drive(Agent(llm, FakeSandbox(), ALLOW), script)
    assert kinds[:2] == ["asking", "unusable_reply"] and outcome.attempts == 2


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
    assert "our fallback was rejected" in outcome.reason


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
