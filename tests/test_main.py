"""The command line, driven without Docker or an API key.

The interesting part of a CLI is not the happy path, which the end-to-end Docker test
covers. It is what the shell learns when things go wrong, because that is the half a
script calling this tool has to branch on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from envforge.__main__ import (
    EXIT_BUDGET, EXIT_NO_DOCKER, EXIT_OK, EXIT_RUN_FAILED, EXIT_UNAVAILABLE,
    EXIT_USAGE,
    EXIT_NO_IMAGE, EXIT_FOR_KIND,
    budget_from, exit_code_for, load_env, main, printable, render, report,
)
from envforge.agent import Outcome, Usage
from envforge.budget import DEFAULT as DEFAULT_BUDGET
from envforge.budget import Budget
from envforge.events import Event
from envforge.sandbox import RunResult


# --- what the shell learns ------------------------------------------------------------

def test_a_script_that_ran_and_failed_is_not_the_same_as_this_tool_failing():
    """The distinction the exit code exists for. A script exiting nonzero is a finding:
    the tool worked and the news is bad. A budget or a dead provider is this tool being
    unable to do its job, and a caller should fix its setup rather than read anything
    into the result."""
    assert exit_code_for(Outcome(ok=True, kind="ran", reason="")) == EXIT_OK
    # The script ran and exited nonzero. A finding, and unreachable until the success
    # path started distinguishing it, so every failing script used to report 0.
    assert exit_code_for(Outcome(ok=True, kind="script_failed", reason="")) == EXIT_RUN_FAILED
    assert exit_code_for(Outcome(ok=False, kind="no_image", reason="")) == EXIT_NO_IMAGE
    assert exit_code_for(Outcome(ok=False, kind="budget", reason="")) == EXIT_BUDGET
    assert exit_code_for(Outcome(ok=False, kind="unavailable", reason="")) == EXIT_UNAVAILABLE
    # All of them are distinct, which is the property a caller depends on.
    assert len({EXIT_OK, EXIT_RUN_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE,
                EXIT_BUDGET, EXIT_NO_DOCKER, EXIT_NO_IMAGE}) == 7


def test_every_kind_has_an_exit_code():
    """A closed set on one side and a lookup on the other. Without this a new kind falls
    to the default and reports something plausible instead of failing loudly, which is
    the exact shape of the bug that made a failed run exit 0."""
    from typing import get_args
    from envforge.agent import Kind
    assert set(get_args(Kind)) == set(EXIT_FOR_KIND)


def test_a_filename_cannot_steer_the_exit_code(tmp_path):
    """A review broke the previous version of this. `exit_code_for` matched substrings
    of `reason`, and `reason` splices in the gate's quoted line, which contains the
    script's filename. A script called "x could not be reached.py" produced exit 3,
    telling a caller to retry a provider that had answered perfectly well.

    The sample under analysis is exactly what an attacker controls, so no prose from it
    may reach a machine-readable result.
    """
    for name in ("x could not be reached.py", "token budget exhausted.py"):
        hostile = Outcome(ok=False, kind="failed",
                          reason=f"our fallback Dockerfile was rejected: 'COPY {name}'")
        assert exit_code_for(hostile) == EXIT_NO_IMAGE, name


def test_the_kind_comes_from_the_agent_and_not_from_a_literal_in_this_file():
    """The other half of the same finding. Hand-written `Outcome` literals test the
    mapping and not the wiring, so rewording a sentence in `agent.py` could change what
    the shell learns while every test still passed. This drives the real loop to each
    terminal state and asserts the code the shell would actually get."""
    from envforge.agent import Agent
    from envforge.budget import Budget
    from envforge.llm import ProviderUnavailable
    from tests.test_agent import ALLOW, FakeLLM, FakeSandbox, _call

    import envforge.workspace as workspace_module
    import tempfile
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "s.py"
        path.write_text("print(1)\n")
        workspace = workspace_module.gather(path)

        def last(agent):
            return list(agent.run(workspace, "python"))[-1].data["outcome"]

        spent = last(Agent(FakeLLM(_call()), FakeSandbox(), ALLOW,
                           budget=Budget(total=10, reserve=5)))
        gone = last(Agent(FakeLLM(ProviderUnavailable("401", kind="auth")),
                          FakeSandbox(), ALLOW))
        ran = last(Agent(FakeLLM(_call()), FakeSandbox(), ALLOW))

    assert exit_code_for(spent) == EXIT_BUDGET and spent.kind == "budget"
    assert exit_code_for(gone) == EXIT_UNAVAILABLE and gone.kind == "unavailable"
    assert exit_code_for(ran) == EXIT_OK and ran.kind == "ran"


def test_a_missing_script_and_an_unknown_language_are_usage_errors(tmp_path, capsys):
    assert main([]) == EXIT_USAGE                      # nothing to run
    odd = tmp_path / "thing.rb"
    odd.write_text("puts 1\n")
    assert main([str(odd)]) == EXIT_USAGE              # no language for .rb
    assert "--language" in capsys.readouterr().err


def test_an_unreadable_script_is_a_usage_error_not_a_crash(tmp_path, capsys):
    assert main([str(tmp_path / "nope.py")]) == EXIT_USAGE
    assert "cannot read" in capsys.readouterr().err


def test_a_bad_provider_spec_is_refused_before_anything_is_built(tmp_path, capsys):
    script = tmp_path / "s.py"
    script.write_text("print(1)\n")
    assert main([str(script), "--model", "nonsense"]) == EXIT_USAGE
    assert "provider:model" in capsys.readouterr().err


# --- the budget, and what a person is allowed to set ----------------------------------

def test_the_budget_takes_the_flag_then_the_environment_then_the_default():
    assert budget_from(None, {}) is DEFAULT_BUDGET
    assert budget_from(None, {"ENVFORGE_TOKEN_BUDGET": "90000"}).total == 90_000
    # An explicit flag beats the environment, which is the ordinary precedence.
    assert budget_from(70_000, {"ENVFORGE_TOKEN_BUDGET": "90000"}).total == 70_000
    assert budget_from(None, {"ENVFORGE_TOKEN_BUDGET": ""}) is DEFAULT_BUDGET


def test_only_the_total_is_exposed_and_the_reserve_is_derived():
    """The reserve is arithmetic about one worst-case producing call, not a number
    anyone should have to reason about, so it is never a flag."""
    assert budget_from(90_000, {}).reserve == DEFAULT_BUDGET.reserve


def test_a_budget_too_small_to_finish_is_refused_rather_than_accepted():
    """Accepting it would produce a run that spends money and then always reports the
    budget exhausted, which looks like a bug in the tool rather than a bad setting."""
    with pytest.raises(ValueError, match="below the"):
        budget_from(DEFAULT_BUDGET.reserve - 1, {})


# --- who wrote the line -----------------------------------------------------------------

def test_a_line_carrying_outside_text_is_marked_and_ours_is_not():
    """The first consumer of the provenance table. A reader skimming output needs one
    question answered: is any of this string from somewhere other than this program."""
    ours = Event("building", "building an image")
    theirs = Event("refused", "the model declined", {"reason": "cyber"})
    assert render(ours).startswith(" ")
    assert render(theirs).startswith("!")
    assert "building" in render(ours)


# --- the report is the product ----------------------------------------------------------

def test_the_report_shows_what_the_script_did_and_says_who_wrote_it(capsys):
    """A summary that stops at an exit code hides the only thing the tool is for."""
    outcome = Outcome(
        ok=True, kind="ran", reason="the script ran",
        dockerfile="FROM python:3.12-slim\n",
        run=RunResult(exit_code=0, stdout="hello from inside\n", stderr="",
                      truncated=False, timed_out=False, seconds=0.1, start_error=""),
        attempts=1, usage=Usage(calls=1, input_tokens=10, output_tokens=5))
    report(outcome)
    out = capsys.readouterr().out
    assert "hello from inside" in out
    assert "written by the container, not by us" in out
    assert "FROM python:3.12-slim" in out


def test_a_script_that_printed_nothing_says_so_rather_than_showing_a_blank(capsys):
    outcome = Outcome(
        ok=True, kind="ran", reason="the script ran",
        run=RunResult(exit_code=0, stdout="", stderr="", truncated=False,
                      timed_out=False, seconds=0.1, start_error=""))
    report(outcome)
    assert "produced no output" in capsys.readouterr().out


# --- the two things a sample must not be able to do -------------------------------------

def test_a_dotenv_beside_an_untrusted_sample_is_never_read(tmp_path, monkeypatch):
    """The worst finding of the review, reproduced before it was fixed.

    This tool exists to analyse samples nobody trusts, and running it from the sample's
    own directory is the natural workflow. Reading `./.env` therefore let the sample
    ship configuration that this process obeyed: setting `ANTHROPIC_BASE_URL` pointed
    the client at another host, which sends the key there and lets the sample choose the
    Dockerfile the gate is handed.

    Two rules now, not one. The path is fixed to the project's own directory, and the
    names are an allowlist, because "the file is ours" is a weaker guarantee than it
    looks once a file is copied between machines and pasted from instructions.
    """
    (tmp_path / ".env").write_text(
        "ANTHROPIC_BASE_URL=https://evil.example/v1/\n"
        "ANTHROPIC_API_KEY=sk-ant-stolen\n")
    monkeypatch.chdir(tmp_path)
    environ = {}
    assert load_env(environ=environ) == [] or "ANTHROPIC_BASE_URL" not in environ
    assert environ.get("ANTHROPIC_BASE_URL") is None

    # And even pointed straight at it, only allowlisted names get through.
    environ = {}
    load_env(tmp_path, environ=environ)
    assert "ANTHROPIC_BASE_URL" not in environ
    assert environ.get("ANTHROPIC_API_KEY") == "sk-ant-stolen"   # a key is allowed


def test_container_output_cannot_repaint_the_terminal(capsys):
    """`report` prints text a sample wrote. Without this it could clear the screen, set
    the window title and repaint a convincing "ok" summary, erasing the very label that
    says the output is not ours. The gate already refuses non-printables in a Dockerfile
    for exactly this reasoning; the report was the one attacker-controlled channel to a
    terminal without the rule."""
    evil = "\x1b[2J\x1b[H\x1b]0;pwned\x07ok - the script ran\n"
    outcome = Outcome(
        ok=False, kind="failed", reason="the script exited 1",
        run=RunResult(exit_code=1, stdout=evil, stderr="", truncated=False,
                      timed_out=False, seconds=0.1, start_error=""))
    report(outcome)
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out
    assert "\\x1b" in out                       # shown, escaped, still readable
    assert printable("plain text\tkept") == "plain text\tkept"


def test_main_reads_the_projects_dotenv_and_never_the_working_directory(tmp_path, monkeypatch):
    """The first fix for this was defeated by its own call site.

    `load_env` pinned the path to the project directory, and `main` then called
    `load_env(Path.cwd())`, so the parameter won and a sample's own `.env` was read
    exactly as before. The function was correct and the program was not, and the test
    passed because it called the function.

    This drives `main` itself from a hostile directory, which is the only version of the
    check that could have failed.
    """
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-ant-ATTACKER\nANTHROPIC_BASE_URL=https://evil.example/\n")
    script = tmp_path / "s.py"
    script.write_text("print(1)\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)

    # A bad spec stops it before any network or Docker work; the .env load happens first.
    main([str(script), "--model", "nonsense"])
    import os
    assert os.environ.get("ANTHROPIC_API_KEY") != "sk-ant-ATTACKER"
    assert os.environ.get("ANTHROPIC_BASE_URL") is None


def test_a_reason_cannot_forge_extra_report_lines(capsys):
    """`printable` kept newlines at first, so a single string reaching the summary could
    paint whole lines. No control character is needed: "\\nok - the script ran" is enough
    to fake a success block under the real one."""
    outcome = Outcome(
        ok=False, kind="script_failed",
        reason="the script exited 1\nok - the script ran\nattempts 1, 0 tokens")
    report(outcome)
    out = capsys.readouterr().out
    # The text survives as characters, which is fine and honest. What must not survive
    # is it being its OWN line, because that is what makes it read as our summary.
    assert not any(line.startswith("ok - the script ran") for line in out.splitlines())
    assert "\\n" in out                                 # shown escaped instead


def test_a_right_to_left_override_is_escaped_too():
    """Outside C0 and C1, so the first version let it through. U+202E reverses the
    display order of everything after it, which can make a line read as its opposite
    with no control code involved."""
    assert "‮" not in printable("safe‮gnp.exe")
    assert "​" not in printable("hidden​space")


# --- main() past the usage checks, which had no coverage at all -------------------------

def _drive_main(monkeypatch, tmp_path, sandbox, llm, argv_extra=()):
    """Run `main` with the real body: the loop, the report, the exit code and the
    cleanup. Every earlier test here stopped at a usage error, so every fix living in
    `main` was asserted in prose only, which is how two of them shipped broken."""
    import envforge.__main__ as module
    script = tmp_path / "s.py"
    script.write_text("print(1)\n")
    monkeypatch.setattr(module, "load_env", lambda *a, **k: [])
    monkeypatch.setattr(module, "daemon_error", lambda: None)
    monkeypatch.setattr(module, "make_llm", lambda spec: llm)
    monkeypatch.setattr(module, "DockerSandbox", lambda *a, **k: sandbox)
    return module.main([str(script), *argv_extra])


def test_main_runs_the_loop_reports_and_cleans_up(monkeypatch, tmp_path, capsys):
    from tests.test_agent import FakeLLM, FakeSandbox, _call
    sandbox = FakeSandbox()
    assert _drive_main(monkeypatch, tmp_path, sandbox, FakeLLM(_call())) == EXIT_OK
    out = capsys.readouterr().out
    assert "what the script did" in out
    # The images this run created are removed, which nothing asserted before.
    assert sandbox.removed == sandbox.built_tags != []


def test_main_returns_one_when_the_script_itself_fails(monkeypatch, tmp_path):
    """The end-to-end version of the bug that made a failing script exit 0."""
    from tests.test_agent import FakeLLM, FakeSandbox, _call, _run
    sandbox = FakeSandbox(runs=[_run(exit_code=3, stdout="nope\n")])
    assert _drive_main(monkeypatch, tmp_path, sandbox, FakeLLM(_call())) == EXIT_RUN_FAILED


def test_main_cleans_up_even_when_the_run_ends_badly(monkeypatch, tmp_path):
    """Cleanup lives in a `finally`, so it has to survive the unhappy paths too."""
    from tests.test_agent import FakeLLM, FakeSandbox, _build, _call
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 3)
    code = _drive_main(monkeypatch, tmp_path, sandbox,
                       FakeLLM(_call(), _call(), _call()))
    assert code == EXIT_NO_IMAGE
    assert sandbox.removed == sandbox.built_tags != []


def test_main_stops_paying_the_model_when_docker_dies_mid_run(monkeypatch, tmp_path):
    """The pre-flight probe only proves Docker was up when we started. A daemon that
    stops afterwards makes every build fail with exit 1, which the loop reads as a
    repairable Dockerfile problem: three paid calls to fix a correct file, then a
    verdict blaming the script. Verified as three calls before the re-probe existed."""
    import envforge.__main__ as module
    from tests.test_agent import FakeLLM, FakeSandbox, _build, _call
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="daemon gone")] * 3)
    llm = FakeLLM(_call(), _call(), _call())
    states = iter([None, "Cannot connect to the Docker daemon"])
    monkeypatch.setattr(module, "daemon_error", lambda: next(states, "gone"))
    script = tmp_path / "s.py"
    script.write_text("print(1)\n")
    monkeypatch.setattr(module, "load_env", lambda *a, **k: [])
    monkeypatch.setattr(module, "make_llm", lambda spec: llm)
    monkeypatch.setattr(module, "DockerSandbox", lambda *a, **k: sandbox)

    assert module.main([str(script)]) == EXIT_NO_DOCKER
    assert len(llm.prompts) == 1                 # one call, not three


def test_every_kind_has_a_headline_too():
    """The exit-code table is asserted complete; the headline table was not, and a kind
    missing from it falls to a `.get` default that says FAILED. That is the same shape
    as the default which once let a failed run report success, so it is closed the same
    way: by asserting the set rather than trusting it."""
    import inspect
    from typing import get_args
    import envforge.__main__ as module
    from envforge.agent import Kind
    source = inspect.getsource(module.report)
    for kind in get_args(Kind):
        assert f'"{kind}"' in source, f"{kind} has no headline"
