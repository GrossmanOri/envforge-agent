"""The public records stay readable by a stranger, and name nothing private.

This repository is public. The notes it is written alongside are not, and the rule that
they never meet has been kept by memory until now. Memory is the wrong mechanism here, and
this project has the evidence: five sentences went stale before anyone noticed, the
operating rules described this repository as private when it was public, and a public file
named a notes file by path for three days. Every rule that survived here is one a test
enforces.

Two different failures are caught, and only the first is about privacy.

A path under `private/` is a leak of structure. It says a notes directory exists and what
is in it, and it is a broken link for every reader who cannot open it, which makes it bad
documentation before it is anything else.

`Ori` and `sitting` are not secret at all. They are internal vocabulary, and a record
written for a stranger cannot use a word only the author understands. The identical mistake
is recorded in the predecessor project, where twenty-one comments dated themselves "session
1" and "step 1" and read as private shorthand to anyone outside. Renaming to what happened
rather than when it was scheduled is the fix, and this test is what makes it stick.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Every Markdown file that ships in the public repository. Discovered rather than listed,
# so a new record is covered the day it is added instead of the day someone remembers.
PUBLIC_DOCS = sorted(
    p for p in ROOT.glob("*.md")
    if p.is_file()
)

BANNED = {
    # Structure. Names a directory a reader cannot open, and says what is filed in it.
    "a path under private/": re.compile(r"private/"),
    # The notes themselves, in case one is ever named without its directory.
    "a notes file by name": re.compile(r"interview-qa|ben-findings|linkedin\.md"),
    # Internal vocabulary. Not secret, just meaningless to a reader who is not us.
    "the author's name": re.compile(r"\bOri\b"),
    "the word 'sitting'": re.compile(r"\bsittings?\b", re.IGNORECASE),
}


@pytest.mark.parametrize("doc", PUBLIC_DOCS, ids=lambda p: p.name)
def test_a_public_record_names_nothing_private_and_no_internal_vocabulary(doc):
    text = doc.read_text()
    found = []
    for description, pattern in BANNED.items():
        for match in pattern.finditer(text):
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{doc.name}:{line} has {description}: {match.group()!r}")
    assert not found, (
        f"{len(found)} thing(s) that do not belong in a public record:\n  "
        + "\n  ".join(found)
        + "\n\nWorkflow notes belong in the private notes repository. If a heading is "
          "numbered by when it was scheduled, rename it to what it was instead."
    )


# A run of characters long enough to be a real token rather than a placeholder. Real keys
# from every provider here are far longer than this; `sk-ant-REPLACE_ME` is not.
CREDENTIAL = re.compile(r"[A-Za-z0-9_\-]{32,}")


def test_the_env_file_is_ignored_by_git():
    """ADR-016. The whole `.env` convention rests on this one line in .gitignore, so it is
    asserted rather than assumed. Checked against git itself rather than by reading the
    file, because a pattern that looks right and does not match is the failure mode."""
    import subprocess
    result = subprocess.run(
        ["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True)
    assert result.returncode == 0, (
        ".env is not ignored by git. A developer's real key is one `git add -A` from a "
        "public repository until this passes.")


def test_the_example_env_holds_no_credential():
    """ADR-016. The example ships in a public repository, so the failure it guards is
    somebody pasting a real key in place of a placeholder and committing it. That is not
    prevented by a convention, and a diff full of placeholders is exactly where an eye
    slides past one value that is not."""
    example = ROOT / ".env.example"
    assert example.exists(), (
        ".env.example is missing. It is what a person cloning this copies to .env, and "
        "the file this test exists to keep honest.")
    offenders = []
    for number, line in enumerate(example.read_text().splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, _, value = stripped.partition("=")
        if CREDENTIAL.search(value):
            offenders.append(f".env.example:{number}: {name.strip()} looks like a real value")
    assert not offenders, "\n  ".join(["a credential in a public file:"] + offenders)


def test_the_check_would_actually_catch_something():
    """A test that can only pass is not a test. This asserts the patterns match the
    strings they are meant to match, so a future edit that guts the regexes fails here
    rather than silently passing everything."""
    samples = {
        "a path under private/": "recorded in `private/LATER.md` rather than here",
        "a notes file by name": "written up in interview-qa.md",
        "the author's name": "Ori's request 2026-08-25",
        "the word 'sitting'": "Sitting 6, fourth of five",
    }
    for description, pattern in BANNED.items():
        assert pattern.search(samples[description]), f"{description} stopped matching"
    # And that the name check is not so eager it catches ordinary words. `Ori` inside
    # `origin` or `GrossmanOri` is not the author being named.
    assert not BANNED["the author's name"].search("git push origin main")
    assert not BANNED["the author's name"].search("github.com/GrossmanOri/envforge-agent")
    # And that the credential check separates a real key from a placeholder, which is the
    # only judgment it makes and the one worth pinning down.
    assert CREDENTIAL.search("sk-ant-api03-" + "x" * 40), "a real-length key must be caught"
    assert not CREDENTIAL.search("sk-ant-REPLACE_ME"), "a placeholder must not be caught"
