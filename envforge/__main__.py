"""The command line. One script in, one verdict out.

    python -m envforge script.py
    python -m envforge --check

This is the first caller the project has ever had. Everything below it was driven only
by tests until now, which is why the two failures it exposes are handled here rather
than deferred: a run must be able to say honestly that it failed, and a person has to
be able to tell "the script did something" from "we could not reach the model".

The exit code is the machine-readable half of that distinction and is deliberately not
just zero or one. A script that ran and failed is a finding, and CI treating it as a
crash of this tool would be wrong.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Iterator, Sequence

from .agent import Agent, LANGUAGES, Outcome, language_for
from .budget import DEFAULT as DEFAULT_BUDGET
from .budget import Budget
from .events import Event, Provenance
from .gate import check
from .llm import ProviderUnavailable, make_llm
from .sandbox import DockerSandbox
from .workspace import WorkspaceError, gather

DEFAULT_SPEC = "anthropic:claude-sonnet-5"

# What the shell learns. `1` is the script failing under observation, which is a result
# rather than an error: the tool worked and the news is bad. `3` and `4` are this tool
# being unable to do its job, and a caller should retry or fix its setup rather than
# read anything into them.
EXIT_OK = 0
EXIT_RUN_FAILED = 1
EXIT_USAGE = 2
EXIT_UNAVAILABLE = 3
EXIT_BUDGET = 4


def load_env(root: Path) -> None:
    """Read a local `.env` if one is there, and never complain if it is not.

    Optional on purpose. The environment still wins, so an exported key beats the file
    and CI needs no file at all. `python-dotenv` is imported here rather than at module
    scope so that a checkout without it can still run the tests.
    """
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(root / ".env", override=False)


def check_key(spec: str) -> int:
    """Answer three questions in order, because they need different fixes.

    Is a key present, does it authenticate, and does the account have credit. The third
    is the one worth separating: an exhausted account and a key without model access are
    both HTTP 403 and only the provider's error type tells them apart.

    This is a convenience and not a guarantee. It says the key worked a second ago, not
    that it will work in the middle of a run, and credit can run out in between. What
    has to hold is the handling inside the loop; this only moves the common failure to
    before a container is built.
    """
    try:
        llm = make_llm(spec)
    except ValueError as exc:
        print(f"bad provider spec: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:                       # a missing key raises at construction
        print(f"no usable credentials for {spec}: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    client = getattr(llm, "_client", None)
    if client is None or not hasattr(client, "models"):
        print(f"{spec}: key present. This provider has no cheap way to verify it "
              f"without spending a call, so it was not checked further.")
        return EXIT_OK
    try:
        models = list(client.models.list(limit=20))
    except Exception as exc:
        status = getattr(exc, "status_code", "?")
        reported = getattr(exc, "type", "") or type(exc).__name__
        print(f"{spec}: FAILED ({status} {reported}) {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    wanted = spec.partition(":")[2]
    names = [getattr(m, "id", "") for m in models]
    print(f"{spec}: key works, {len(names)} model(s) visible.")
    if names and wanted not in names:
        # Not fatal. The listing is paginated and a model missing from it may still
        # answer, so this is a warning rather than a refusal to proceed.
        print(f"  note: {wanted!r} is not in the visible listing", file=sys.stderr)
    return EXIT_OK


def render(event: Event) -> str:
    """One line per event, marked with who wrote it.

    The provenance table exists so a reader can tell our sentences from text the model,
    the container or the script produced, and this is its first consumer. A leading `!`
    means at least part of the line came from outside this program, which is the only
    question a person skimming output actually needs answered.
    """
    marker = " " if event.authors() == {Provenance.US} else "!"
    return f"{marker} {event.kind:<20} {event.message}"


def report(outcome: Outcome) -> None:
    """What the run produced, ending with what the script actually did.

    The observed behaviour goes last and is the reason this tool exists, so the summary
    is not allowed to stop at an exit code. Everything the container wrote is
    attacker-controlled text, already bounded by the sandbox, and it is labelled here
    rather than printed bare so nobody reads it as ours.
    """
    print()
    print("ok" if outcome.ok else "FAILED", "-", outcome.reason)
    print(f"attempts {outcome.attempts}, "
          f"{outcome.usage.calls} model call(s), {outcome.usage.tokens} tokens")
    if outcome.used_fallback:
        print("the Dockerfile came from us, not from the model")

    if outcome.dockerfile:
        print("\n--- the Dockerfile that was built ---")
        print(outcome.dockerfile.rstrip())

    run = outcome.run
    if run is None:
        return
    print(f"\n--- what the script did (exit {run.exit_code}"
          f"{', timed out' if run.timed_out else ''}) ---")
    for name, stream in (("stdout", run.stdout), ("stderr", run.stderr)):
        if stream.strip():
            print(f"[{name}, written by the container, not by us]")
            for line in stream.rstrip().splitlines():
                print(f"  | {line}")
    if not run.stdout.strip() and not run.stderr.strip():
        print("  (the script produced no output)")
    if run.truncated:
        print("  (output was bounded before it reached here)")


def exit_code_for(outcome: Outcome) -> int:
    if outcome.ok:
        return EXIT_OK
    if "budget exhausted" in outcome.reason:
        return EXIT_BUDGET
    if "could not be reached" in outcome.reason:
        return EXIT_UNAVAILABLE
    return EXIT_RUN_FAILED


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m envforge",
        description="Have a model write a Dockerfile for a script you do not trust, "
                    "then build and run it in a hardened container.")
    parser.add_argument("script", nargs="?", type=Path,
                        help="the script to containerise. Python or Bash.")
    parser.add_argument("--check", action="store_true",
                        help="verify the API key and exit, without running anything")
    parser.add_argument("--model", default=DEFAULT_SPEC, metavar="PROVIDER:MODEL",
                        help=f"default {DEFAULT_SPEC}")
    parser.add_argument("--language", choices=sorted(LANGUAGES),
                        help="override the language inferred from the extension")
    parser.add_argument("--token-budget", type=int, metavar="N",
                        help="total tokens one run may spend on the model. A safety "
                             "limit rather than an allowance: the default is generous, "
                             "so hitting it means something went wrong. Overrides "
                             "ENVFORGE_TOKEN_BUDGET.")
    parser.add_argument("--arg", action="append", default=[], metavar="ARG",
                        help="an argument for the script, repeatable")
    return parser


def budget_from(cli_value: int | None, environ) -> Budget:
    """The ordinary precedence: an explicit flag, then the environment, then the
    default. Only the total is exposed. The reserve is arithmetic about one worst-case
    producing call and not a number anyone should have to reason about."""
    raw = cli_value if cli_value is not None else environ.get("ENVFORGE_TOKEN_BUDGET")
    if raw in (None, ""):
        return DEFAULT_BUDGET
    total = int(raw)
    if total < DEFAULT_BUDGET.reserve:
        raise ValueError(
            f"a budget of {total} is below the {DEFAULT_BUDGET.reserve} reserved for the "
            f"one call that has to produce a Dockerfile, so no run could finish")
    return Budget(total=total, reserve=DEFAULT_BUDGET.reserve)


def main(argv: Sequence[str] | None = None, environ=None) -> int:
    import os
    environ = os.environ if environ is None else environ
    parser = build_parser()
    args = parser.parse_args(argv)
    load_env(Path.cwd())

    if args.check:
        return check_key(args.model)
    if args.script is None:
        parser.print_usage(sys.stderr)
        print("a script is required unless --check is given", file=sys.stderr)
        return EXIT_USAGE

    language = args.language or language_for(args.script)
    if language is None:
        print(f"cannot tell what language {args.script.name} is. Use --language.",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        budget = budget_from(args.token_budget, environ)
    except ValueError as exc:
        print(f"bad token budget: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        workspace = gather(args.script, LANGUAGES[language].siblings)
    except WorkspaceError as exc:
        print(f"cannot read {args.script}: {exc}", file=sys.stderr)
        return EXIT_USAGE

    try:
        llm = make_llm(args.model)
    except ValueError as exc:
        print(f"bad provider spec: {exc}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as exc:
        print(f"no usable credentials for {args.model}: {exc}", file=sys.stderr)
        return EXIT_UNAVAILABLE

    agent = Agent(llm, DockerSandbox(), check, budget=budget)
    outcome = None
    for event in agent.run(workspace, language, tuple(args.arg)):
        print(render(event), flush=True)
        if event.kind == "finished":
            outcome = event.data["outcome"]
    if outcome is None:                     # the generator cannot end without one
        print("the run ended without an outcome", file=sys.stderr)
        return EXIT_RUN_FAILED
    report(outcome)
    return exit_code_for(outcome)


if __name__ == "__main__":
    raise SystemExit(main())
