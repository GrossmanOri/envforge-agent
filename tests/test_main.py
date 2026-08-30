"""The command line, driven without Docker or an API key.

The interesting part of a CLI is not the happy path, which the end-to-end Docker test
covers. It is what the shell learns when things go wrong, because that is the half a
script calling this tool has to branch on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from envforge.__main__ import (
    EXIT_BUDGET, EXIT_OK, EXIT_RUN_FAILED, EXIT_UNAVAILABLE, EXIT_USAGE,
    budget_from, exit_code_for, main, render, report,
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
    ran = Outcome(ok=True, reason="the script ran")
    failed = Outcome(ok=False, reason="the script exited 1")
    spent = Outcome(ok=False, reason="token budget exhausted: 300 of 256 spent")
    gone = Outcome(ok=False, reason="the model could not be reached (auth): 401")

    assert exit_code_for(ran) == EXIT_OK
    assert exit_code_for(failed) == EXIT_RUN_FAILED
    assert exit_code_for(spent) == EXIT_BUDGET
    assert exit_code_for(gone) == EXIT_UNAVAILABLE
    # All four are distinct, which is the property a caller depends on.
    assert len({EXIT_OK, EXIT_RUN_FAILED, EXIT_USAGE, EXIT_UNAVAILABLE, EXIT_BUDGET}) == 5


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
        ok=True, reason="the script ran", dockerfile="FROM python:3.12-slim\n",
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
        ok=True, reason="the script ran",
        run=RunResult(exit_code=0, stdout="", stderr="", truncated=False,
                      timed_out=False, seconds=0.1, start_error=""))
    report(outcome)
    assert "produced no output" in capsys.readouterr().out
