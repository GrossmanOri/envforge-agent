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
import re
import sys
from pathlib import Path
from typing import Sequence

from .agent import Agent, LANGUAGES, Outcome, language_for
from .events import Event, Provenance
from .gate import check
from .llm import MissingKey, ProviderUnavailable, kind_for_status, make_llm
from .sandbox import DockerSandbox, SandboxError, daemon_error
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
EXIT_NO_DOCKER = 5
EXIT_NO_IMAGE = 6
# Our own request was refused. Not the provider being down, so retrying is wrong.
EXIT_BAD_REQUEST = 7


# The only variables a `.env` may set. An allowlist, like the gate, and for the same
# reason: the interesting attack is never the name you thought of. `ANTHROPIC_BASE_URL`
# is the one that matters, because setting it points the client at another server and
# sends the key there, but no blocklist would have caught every equivalent.
ENV_ALLOWED = frozenset({
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GROQ_API_KEY",
})

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_env(root: Path | None = None, environ=None) -> list[str]:
    """Read the project's own `.env`, and only ever that one.

    The location is fixed to the directory containing this package, not the working
    directory. That is the whole point rather than a detail. This tool analyses samples
    nobody trusts, and running it from the sample's own directory is the natural
    workflow, so reading `./.env` means an untrusted sample can ship a config file that
    this process then obeys. Verified before it was fixed: a `.env` beside a sample set
    `ANTHROPIC_BASE_URL`, and the client would have sent the key to that host.

    Names are filtered as well as the path, because the file being ours is a weaker
    guarantee than it looks: it is edited by hand, copied between machines, and pasted
    from instructions. Only credentials get through.

    The process environment always wins, so an exported key beats the file and CI needs
    no file at all. Returns the names it set, so a caller can say so rather than having
    the effect be invisible.
    """
    import os
    environ = os.environ if environ is None else environ
    path = (root or PROJECT_ROOT) / ".env"
    try:
        from dotenv import dotenv_values
    except ImportError:
        return []
    try:
        values = dotenv_values(path)
    except (OSError, ValueError) as exc:
        # A `.env` that exists and cannot be read: wrong permissions after a copy, or
        # saved as UTF-16 by an editor. Both raised out of `main` before it had entered
        # any handler, so a mis-permissioned config file produced a raw traceback and
        # exit 1, which this project defines as the script running and failing. Under
        # `--check` there is no script at all.
        #
        # Reported and survivable rather than fatal, because this file is optional by
        # design: the environment may still carry the key, and if it does not, the run
        # ends with the message that actually says so.
        print(f"ignoring {printable(str(path))}: {printable(str(exc))}", file=sys.stderr)
        return []
    applied = []
    for name, value in values.items():
        if name not in ENV_ALLOWED or value is None:
            continue
        if environ.get(name):          # already exported: the environment wins
            continue
        environ[name] = value
        applied.append(name)
    return applied


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
    except MissingKey as exc:
        print(f"{printable(exc.variable)} is not set", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ProviderUnavailable as exc:
        # The parent, not only the subclass. Catching `MissingKey` alone let a plain
        # `ProviderUnavailable` from the SDK constructor, which a broken or missing
        # credential profile raises, escape both entry points as a traceback and exit 1.
        # Exit 1 is defined here as the script running and failing, so a setup mistake
        # was reporting itself as a finding about the sample.
        print(printable(str(exc)), file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ValueError as exc:
        print(f"bad provider spec: {printable(str(exc))}", file=sys.stderr)
        return EXIT_USAGE

    client = getattr(llm, "_client", None)
    if client is None or not hasattr(client, "models"):
        print(f"{printable(spec)}: key present. This provider has no cheap way to verify it "
              f"without spending a call, so it was not checked further.")
        return EXIT_OK
    try:
        # No kwargs: `limit` is Anthropic-only and made this command report a
        # perfectly good OpenAI or Groq key as unusable.
        models = list(client.models.list())
    except Exception as exc:
        status = getattr(exc, "status_code", "?")
        reported = getattr(exc, "type", "") or type(exc).__name__
        print(f"{printable(spec)}: FAILED ({printable(str(status))} "
              f"{printable(str(reported))}) {printable(str(exc))}", file=sys.stderr)
        # The same rule a run uses, called rather than copied. This carried its own
        # mapping and stayed on 400 alone while `reachable` widened to every 4xx, so a
        # 422 was "our bug, do not retry" from a run and "provider unavailable, retry"
        # from here, for one event. Two entry points disagreeing is the thing to avoid.
        if not isinstance(status, int):
            return EXIT_UNAVAILABLE
        kind = kind_for_status(status, str(reported))
        return EXIT_BAD_REQUEST if kind == "rejected" else EXIT_UNAVAILABLE

    wanted = spec.partition(":")[2]
    names = [getattr(m, "id", "") for m in models]
    print(f"{printable(spec)}: key works, {len(names)} model(s) visible.")
    if names and wanted not in names:
        # Not fatal. The listing is paginated and a model missing from it may still
        # answer, so this is a warning rather than a refusal to proceed.
        print(f"  note: {wanted!r} is not in the visible listing", file=sys.stderr)
    return EXIT_OK


# Everything except tab. A terminal treats these as commands, not as text: a sample can
# clear the screen, set the window title, and repaint a convincing "ok" summary, erasing
# the very label that says the output is not ours. The gate already refuses non-printables
# in a Dockerfile for this reason; the report was the one attacker-controlled channel to a
# terminal without the rule.
CONTROL = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f"
    # Bidirectional overrides and invisible separators, which are outside C0 and C1 and
    # were still getting through. U+202E reverses the display order of everything after
    # it, so a line can be made to read as its own opposite without a control code.
    "\u200b-\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069\ufeff]")


def printable(text: str, keep_newlines: bool = False) -> str:
    """Replace anything that acts on a terminal with a visible escape.

    `keep_newlines` is for text we split ourselves and prefix line by line. Everywhere
    else a newline is escaped too, because a single string reaching the summary can
    otherwise forge whole lines: a reason containing "\nok - the script ran" paints a
    convincing success block with no control character in it at all.
    """
    escaped = CONTROL.sub(lambda m: f"\\x{ord(m.group()):02x}", text)
    if keep_newlines:
        return escaped
    return escaped.replace("\r", "\\r").replace("\n", "\\n")


def render(event: Event) -> str:
    """One line per event, marked with who wrote it.

    The provenance table exists so a reader can tell our sentences from text the model,
    the container or the script produced, and this is its first consumer. A leading `!`
    means at least part of the line came from outside this program, which is the only
    question a person skimming output actually needs answered.
    """
    marker = " " if event.authors() == {Provenance.US} else "!"
    return f"{marker} {event.kind:<20} {printable(event.message)}"


def report(outcome: Outcome) -> None:
    """What the run produced, ending with what the script actually did.

    The observed behaviour goes last and is the reason this tool exists, so the summary
    is not allowed to stop at an exit code. Everything the container wrote is
    attacker-controlled text, already bounded by the sandbox, and it is labelled here
    rather than printed bare so nobody reads it as ours.
    """
    print()
    # Keyed to the kind, not to `ok`. `ok` means the tool did its job, which is true
    # even when the script it was watching failed, so keying the header to it printed
    # "ok" above an exit code of 1. The person and the shell now agree.
    print(HEADLINE_FOR_KIND[outcome.kind], "-", printable(outcome.reason))
    print(f"attempts {outcome.attempts}, "
          f"{outcome.usage.calls} model call(s), {outcome.usage.looks} of them "
          f"looking at the script, {outcome.usage.tokens} tokens")
    if outcome.used_fallback:
        print("the Dockerfile came from us, not from the model")

    if outcome.dockerfile:
        # Prefixed and labelled like the container block, and for the same reason. This
        # was printed at column zero with no marker, so the model could end a Dockerfile
        # with lines that read as this program's own summary: a forged
        # "--- what the script did (exit 0) ---" was the last thing on screen for a run
        # where the gate refused every attempt and nothing was ever built.
        built = outcome.build is not None and outcome.build.ok
        whose = "written by us" if outcome.used_fallback else "written by the model"
        print(f"\n--- the Dockerfile {'that was built' if built else 'last considered'}"
              f", {whose} ---")
        for line in outcome.dockerfile.rstrip().splitlines():
            print(f"  | {printable(line)}")

    run = outcome.run
    if run is None:
        return
    print(f"\n--- what the script did (exit {run.exit_code}"
          f"{', timed out' if run.timed_out else ''}) ---")
    for name, stream in (("stdout", run.stdout), ("stderr", run.stderr)):
        if stream.strip():
            print(f"[{name}, written by the container, not by us]")
            for line in stream.rstrip().splitlines():
                print(f"  | {printable(line)}")
    if not run.stdout.strip() and not run.stderr.strip():
        print("  (the script produced no output)")
    if run.truncated:
        print("  (output was bounded before it reached here)")


# Keyed rather than defaulted, and a module constant rather than an inline dict, so a
# missing kind is a KeyError here and a set-equality failure in the tests. The first
# version was an inline `.get(kind, "FAILED")` asserted by grepping this function's
# source, and a review showed the assertion survived deleting an entry.
HEADLINE_FOR_KIND = {
    "ran": "ok",
    "script_failed": "the script FAILED",
    "no_image": "FAILED",
    "build_timeout": "TIMED OUT",
    "failed": "FAILED",
    "unavailable": "STOPPED",
    "rejected": "OUR BUG",
    "unsupported": "REFUSED",
}

EXIT_FOR_KIND = {
    "ran": EXIT_OK,
    # The script ran and exited nonzero. A finding, not a malfunction: the tool did its
    # job and the news is bad. This was unreachable until the success path started
    # distinguishing the two, so every failing script reported 0.
    "script_failed": EXIT_RUN_FAILED,
    "no_image": EXIT_NO_IMAGE,
    "build_timeout": EXIT_NO_IMAGE,
    "failed": EXIT_NO_IMAGE,
    "unsupported": EXIT_USAGE,
    "unavailable": EXIT_UNAVAILABLE,
    "rejected": EXIT_BAD_REQUEST,
}


def exit_code_for(outcome: Outcome) -> int:
    """Switch on the typed kind, never on the words in `reason`.

    This matched substrings of `reason` until a review broke it. `reason` splices in
    filenames, the gate's quoted line and provider error text, so a script named
    "x could not be reached.py" produced exit 3, telling a caller to retry a provider
    that had answered fine. The sample under analysis is exactly the thing an attacker
    controls, so prose from it must never steer a machine-readable result.
    """
    return EXIT_FOR_KIND.get(outcome.kind, EXIT_RUN_FAILED)


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
    parser.add_argument("--arg", action="append", default=[], metavar="ARG",
                        help="an argument for the script, repeatable")
    return parser


def main(argv: Sequence[str] | None = None, environ=None) -> int:
    import os
    environ = os.environ if environ is None else environ
    parser = build_parser()
    args = parser.parse_args(argv)
    # No argument. Passing `Path.cwd()` here is what defeated this function's own
    # protection: the parameter won, `PROJECT_ROOT` was never used in production,
    # and a sample's directory was read exactly as before. The parameter exists for
    # tests and nothing else.
    load_env()

    if args.check:
        return check_key(args.model)
    if args.script is None:
        parser.print_usage(sys.stderr)
        print("a script is required unless --check is given", file=sys.stderr)
        return EXIT_USAGE

    language = args.language or language_for(args.script)
    if language is None:
        print(f"cannot tell what language {printable(args.script.name)} is. "
              f"Use --language.",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        workspace = gather(args.script, LANGUAGES[language].siblings)
    except (WorkspaceError, OSError) as exc:
        # `OSError` as well as our own type. `gather` raises `PermissionError` on a
        # script it cannot read, which is an OSError and was not guarded, so the run
        # died with a raw traceback carrying an unescaped path and exit 1, meaning the
        # script ran and failed. The same shape as the credential crash one commit ago.
        print(f"cannot read {printable(str(args.script))}: {printable(str(exc))}",
              file=sys.stderr)
        return EXIT_USAGE

    try:
        llm = make_llm(args.model)
    except MissingKey as exc:
        # Its own exit code, not the usage one. Reporting a missing key as a bad spec
        # told the user to fix something they had typed correctly.
        print(f"{printable(exc.variable)} is not set. Put it in the environment or in "
              f"the project's .env; see .env.example.", file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ProviderUnavailable as exc:
        print(printable(str(exc)), file=sys.stderr)
        return EXIT_UNAVAILABLE
    except ValueError as exc:
        print(f"bad provider spec: {printable(str(exc))}", file=sys.stderr)
        return EXIT_USAGE

    problem = daemon_error()
    if problem is not None:
        print(f"cannot use Docker: {printable(problem)}", file=sys.stderr)
        print("is the daemon running?", file=sys.stderr)
        return EXIT_NO_DOCKER

    sandbox = DockerSandbox()
    agent = Agent(llm, sandbox, check)
    outcome = None
    try:
        for event in agent.run(workspace, language, tuple(args.arg)):
            print(render(event), flush=True)
            if event.kind == "build_failed":
                # The pre-flight probe only proves Docker was up when we started. A
                # daemon that stops mid-run makes every build fail with exit 1, which
                # the loop reads as a repairable Dockerfile problem: three paid calls
                # asking the model to fix a file that was already correct, then a
                # verdict blaming the script.
                #
                # Every build failure, not just the first, because the daemon can stop
                # between attempt one and attempt two just as easily. Measured at 78ms,
                # against builds that take seconds, so the cost of asking is noise and
                # the cost of not asking is a wrong verdict and two paid calls.
                problem = daemon_error()
                if problem is not None:
                    print(f"\ncannot use Docker: {printable(problem)}", file=sys.stderr)
                    return EXIT_NO_DOCKER
            if event.kind == "finished":
                outcome = event.data["outcome"]
    except SandboxError as exc:
        # Our own precondition, not the daemon. Collapsing the two told an operator the
        # daemon was broken when it was fine and the caller had made a mistake.
        # Our bug, not the caller's. SandboxError's own docstring says our docker
        # command was wrong, and ADR-008 calls docker 125 our code being broken, which
        # is exactly what exit 7 was created to mean. Exit 2 told the caller they had
        # typed something wrong.
        print(f"\n{printable(str(exc))}", file=sys.stderr)
        return EXIT_BAD_REQUEST
    except OSError as exc:
        # No docker binary, or a daemon that is not running. Neither is a finding about
        # the script and neither is the model's fault, which is what made this worth
        # catching: without it a stopped daemon looked like three failed builds, spent
        # three paid repair calls on a Dockerfile that was already correct, and then
        # reported the script as having run and failed.
        print(f"\ncannot reach Docker: {printable(str(exc))}", file=sys.stderr)
        return EXIT_NO_DOCKER
    finally:
        # Every attempt builds a tagged image and nothing removed them, so a machine
        # that had run this a hundred times held a hundred images. Layers are shared, so
        # the disk cost is far smaller than the count suggests, but the clutter is real
        # and each one was built from a Dockerfile an untrusted script influenced.
        for tag in sandbox.built_tags:
            sandbox.remove_image(tag)
    if outcome is None:                     # the generator cannot end without one
        print("the run ended without an outcome", file=sys.stderr)
        return EXIT_RUN_FAILED
    report(outcome)
    return exit_code_for(outcome)


if __name__ == "__main__":
    raise SystemExit(main())
