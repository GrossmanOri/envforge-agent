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
    p for p in (
        [p for p in ROOT.glob("*.md") if p.is_file()]
        + list((ROOT / "envforge").glob("*.py"))
        + list((ROOT / "tests").glob("*.py"))
        # The sample scripts. They ship in the public repository like everything else,
        # and a fixture is exactly the kind of file somebody pastes a real path or a
        # real key into while making it realistic. Added the day `examples/` was, which
        # is later than the docstring above promises: the glob was discovered per
        # directory, so a new directory was not covered by it.
        + list((ROOT / "examples").glob("*.py"))
    )
    # This file states the patterns, so it necessarily contains every one of them. It is
    # the only exemption, and it is named rather than pattern-matched so that adding a
    # second one is a visible decision instead of a widened glob.
    if p.name != "test_records.py"
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


def test_no_two_decisions_share_a_number():
    """ADR numbers are how one entry refers to another, so a duplicate makes both
    references ambiguous and neither wrong enough to notice.

    Written after ADR-018 was used twice: once for the looking tools and once, a
    fortnight later, for the engine. Counting the headings by hand is exactly the check
    a person skips.
    """
    text = (ROOT / "ARCHITECTURE.md").read_text()
    numbers = re.findall(r"^### (ADR-\d+):", text, flags=re.M)
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"used more than once: {', '.join(duplicates)}"
    assert numbers == sorted(numbers), "the decision log is out of order"


def test_no_two_invariants_share_a_number():
    """Six places in the code and tests cite an invariant by number, so a duplicate makes
    each of those citations name two different rules.

    Written after an edit appended a new block of 23 to 31 without retiring the old 23 to
    28, leaving two different invariant 24s and two different invariant 28s. The sibling
    test guards ADR numbers and would not have seen this, which is the whole reason both
    exist rather than one.
    """
    text = (ROOT / "ARCHITECTURE.md").read_text()
    section = text.split("## Invariants", 1)[1].split("\n## ", 1)[0]
    numbers = [int(n) for n in re.findall(r"^(\d+)\. ", section, flags=re.M)]
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"invariant number used more than once: {duplicates}"
    assert numbers == sorted(numbers), "the invariants are out of order"
    assert numbers == list(range(1, len(numbers) + 1)), "the numbering has a gap"


NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20,
}


def test_the_records_state_the_real_number_of_event_kinds():
    """ADR-014 names a count and it has gone stale twice: thirteen while the code had
    twelve, then still thirteen after two kinds were added, then more wrong again when
    this change added `swept`.

    So the number is checked rather than remembered. Written as a word because that is
    how the records write it, and a test that only accepted digits would pass by never
    matching anything.
    """
    from envforge.events import VOCABULARY

    text = (ROOT / "ARCHITECTURE.md").read_text()
    match = re.search(r"`envforge/events\.py` holds the (\w+) kinds", text)
    assert match, "ADR-014 no longer states a count in the form this checks"
    stated = NUMBER_WORDS.get(match.group(1))
    assert stated is not None, f"{match.group(1)!r} is not a number word this test knows"
    assert stated == len(VOCABULARY), (
        f"ARCHITECTURE.md says {match.group(1)} ({stated}) event kinds, the code has "
        f"{len(VOCABULARY)}: {', '.join(sorted(VOCABULARY))}")
