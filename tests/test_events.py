"""The vocabulary is closed, and the table matches what the loop actually emits."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from envforge import agent
from envforge.events import VOCABULARY, Event, Provenance


def emitted_kinds(module) -> set[str]:
    """Every `Event("...")` written in a module, read from its source.

    The other direction is guaranteed at construction, so this closes the loop: a
    kind emitted but not declared raises when the loop runs, and a kind declared
    but never emitted is dead weight nobody would otherwise notice.
    """
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    kinds = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) == "Event"):
            continue
        first = node.args[0] if node.args else None
        # A computed kind would make this test quietly incomplete rather than red,
        # which is the failure mode of every source-reading check.
        assert isinstance(first, ast.Constant), f"Event() called with {ast.dump(first)}"
        kinds.add(first.value)
    return kinds


def test_the_table_is_exactly_what_the_loop_emits():
    assert emitted_kinds(agent) == set(VOCABULARY)


def test_an_unknown_kind_is_refused():
    # The seam: a graph engine that invents a kind fails here rather than producing
    # a trace record nobody can label.
    with pytest.raises(ValueError, match="node_entered"):
        Event("node_entered", "the graph moved")


def test_an_unexpected_data_key_is_refused():
    with pytest.raises(ValueError):
        Event("building", "building x", {"tag": "x"})


def test_a_missing_data_key_is_refused():
    # A consumer reading data["outcome"] off a finished event is entitled to it.
    with pytest.raises(ValueError, match="outcome"):
        Event("finished", "done")


def test_our_own_strings_are_labelled_ours():
    assert Event("asking", "attempt 1").authors() == {Provenance.US}


def test_a_string_we_wrote_around_the_models_is_labelled_both():
    # The gate's reason quotes the offending line, so the sentence has two authors
    # and a single label per event would have been a lie on this one.
    event = Event("gate_rejected", "line 4, 'RUN curl x': not allowed",
                  {"dockerfile": "FROM python:3.12-slim\n"})
    assert event.authors() == {Provenance.US, Provenance.MODEL}
    # The Dockerfile too: after two refusals the rejected file is the one we wrote.
    assert event.authors("dockerfile") == {Provenance.US, Provenance.MODEL}


def test_asking_for_a_string_the_event_does_not_carry():
    with pytest.raises(KeyError):
        Event("asking", "attempt 1").authors("dockerfile")


def test_every_string_has_at_least_one_author():
    """An empty set would read as "not ours" to a renderer and as "nobody wrote
    this" to a reader, which is the one label that can be neither."""
    for kind in VOCABULARY.values():
        assert "message" in kind.authors, kind.name
        for key, authors in kind.authors.items():
            assert authors, f"{kind.name}.{key} names no author"
