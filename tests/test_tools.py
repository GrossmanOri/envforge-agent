"""The two tools the model may call, on their own.

Moved here when the loop was deleted, because they were never the loop's: they read a
region of the script or search it, and both bound and label what they return at the point
of production, which is the only place the rule cannot be forgotten by a later caller.
"""

import pytest

from envforge.tools import (LISTED_OFFSETS, SEARCH_MATCHES, SLICE_HEADER, SLICE_LIMIT,
                            bound, labelled, read_region, search_text)

# The names these tests were written against, kept so the bodies read unchanged.
search = search_text


def test_read_region_clamps_rather_than_refusing():
    """The offsets came from a model reading a notice, so an off-by-something is an
    ordinary mistake. Refusing would spend a look teaching it to count."""
    text = "0123456789"
    assert "characters 0 to 4 of 10" in read_region(text, -50, 4)
    assert read_region(text, -50, 4).endswith("0123")
    assert "characters 10 to 10 of 10" in read_region(text, 99, 200)
    assert "past the end of the file" in read_region(text, 99, 200)
    # Reversed, so there is nothing between them and nothing comes back.
    assert read_region(text, 8, 2).endswith("of 10:\n")


def test_read_region_says_when_it_gave_less_than_was_asked_for():
    text = "x" * 9000
    result = read_region(text, 0, 9000)
    assert f"only the first {SLICE_LIMIT} characters" in result
    assert result.count("x") == SLICE_LIMIT


def test_read_region_survives_arguments_that_are_not_numbers():
    """`True` satisfies a JSON integer in Python, and Groq's schema guarantee does not
    cover tool use at all, so the type is checked here rather than assumed."""
    assert "whole numbers" in read_region("abc", "start", None)
    assert "characters 1 to 2 of 3" in read_region("abc", True, 2)


def test_search_is_a_literal_and_never_a_regular_expression():
    """The pattern is chosen by a model that has just read attacker-controlled text,
    and `re` on a model-chosen pattern is catastrophic backtracking on the one machine
    in this design that is not in a sandbox."""
    text = "a" * 200 + "\nprint(1)\n"
    # As a regex this matches; as a literal it does not, which is the point.
    assert "does not occur" in search(text, "a+b?")
    assert "does not occur" in search(text, ".*")
    # And the pathological one returns instead of running until the run is killed.
    assert "does not occur" in search("a" * 4000, "(a+)+$")
    # A literal dot is a dot.
    assert "occurs 1 time(s)" in search("print(1)\nx.y\n", "x.y")
    assert "does not occur" in search("print(1)\nxzy\n", "x.y")


def test_search_reports_every_offset_and_shows_a_spread_of_them():
    text = "".join(f"import mod{i}\n" for i in range(20))
    result = search(text, "import ")
    assert "occurs 20 time(s)" in result
    # Every offset as a bare number, because an offset is what read_script takes.
    offsets, at = [], text.find("import ")
    while at != -1:
        offsets.append(at)
        at = text.find("import ", at + len("import "))
    assert len(offsets) == 20
    listed = result.split("every offset: ")[1].splitlines()[0]
    assert listed == ", ".join(str(offset) for offset in offsets)
    assert result.count("\nat character ") == SEARCH_MATCHES


def test_search_does_not_spend_a_look_showing_only_what_was_already_shown():
    """The defect the first real run exposed, and the reason this tool exists at all.

    A model searching a Python file for `import` matches the import block at the top
    first, and the top is the half it was already given. Showing the first five matches
    returned nothing new, so the look was wasted and the model fell back to reading the
    middle in slices, finding the answer on the last of four. Measured on the fixture:
    eleven matches, and the one that mattered was the eleventh.
    """
    head = "".join(f"import stdlib{i}\n" for i in range(9))
    buried = "\n" + "# padding\n" * 400 + "    from tabulate import tabulate\n"
    text = head + buried
    result = search(text, "import")

    answer = text.index("from tabulate import") + len("from tabulate ")
    assert str(answer) in result                      # its offset is listed
    assert "from tabulate import tabulate" in result  # and a window covers it
    # Not by luck: the last match is always one of the ones shown.
    assert result.rindex("at character") > result.index("at character")


def test_search_counts_the_way_str_count_does():
    """Stepping by one finds overlapping matches, so "aaa" would hold two "aa". True,
    and not what anybody asked."""
    assert "occurs 1 time(s)" in search("aaa", "aa")
    # "at character" counts only the windows. The line listing every offset is headed
    # "every offset" precisely so the two cannot be confused, here or by the model.
    assert search("aaa", "aa").count("\nat character ") == 1


def test_search_says_so_when_there_is_nothing_to_look_for():
    assert "nothing to look for" in search("abc", "")
