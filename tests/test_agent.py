"""The pieces a run is built from: the language table, the prompts, and the bound.

The repair loop this file was named for is gone; the graph replaced it and
`test_graph.py` covers what it did. What is left here is the vocabulary the graph reaches
for, which has no engine in it at all.
"""

from pathlib import Path

import pytest

from envforge.agent import (LANGUAGES, SCRIPT_LIMIT, bound, default_dockerfile,
                            language_for)


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


def test_bound_keeps_both_ends_and_says_how_much_it_cut():
    text = "A" * 100 + "B" * 100
    cut = bound(text, 100)
    assert cut.startswith("A" * 50) and cut.endswith("B" * 50) and "100 characters removed" in cut
    assert bound("short", 100) == "short"


def test_the_fallback_runs_the_script_as_argv_not_as_a_shell_string():
    dockerfile = default_dockerfile("python", "s.py")
    assert 'ENTRYPOINT ["python", "/app/s.py"]' in dockerfile
    assert "\\\n" not in dockerfile          # the gate will ban continuations
    with pytest.raises(ValueError):
        default_dockerfile("ruby", "s.rb")
