"""The bound is tokens, the estimate errs high, and the reserve is what the two
questions differ by."""

import pytest

from envforge.budget import (
    CHARS_PER_TOKEN, DEFAULT_RESERVE, DEFAULT_TOTAL, Budget, Usage, estimate,
)
from envforge.llm import MAX_TOKENS


def test_the_estimate_assumes_the_reply_runs_to_the_ceiling():
    # Nobody can know a call's price before making it, so the guess is pessimistic in
    # both halves: a low characters-per-token rate, and the largest reply allowed.
    assert estimate("") == MAX_TOKENS
    assert estimate("x" * 300) == 300 // CHARS_PER_TOKEN + MAX_TOKENS


def test_the_estimate_counts_every_piece_of_the_prompt():
    assert estimate("aaa", "bbb") == estimate("aaabbb")


def test_a_reserve_larger_than_the_budget_is_a_mistake():
    with pytest.raises(ValueError):
        Budget(total=1000, reserve=2000)


def test_the_reserve_is_the_gap_between_the_two_questions():
    """The whole point of piece 5. There is a state where the run may still make the
    call that produces a Dockerfile, and may no longer spend anything on looking
    around, because looking around would eat the call that pays for it."""
    budget = Budget(total=100_000, reserve=40_000)
    spent = Usage(calls=3, input_tokens=40_000, output_tokens=5_000)
    assert budget.can_write(spent, "short prompt")
    assert not budget.can_investigate(spent, "short prompt")


def test_a_spent_budget_refuses_even_the_producing_call():
    budget = Budget(total=100_000, reserve=40_000)
    spent = Usage(calls=9, input_tokens=90_000, output_tokens=5_000)
    assert not budget.can_write(spent, "short prompt")


def test_a_fresh_run_can_afford_a_call_under_the_default():
    assert Budget().can_write(Usage(), "a script and a prompt around it")
    assert DEFAULT_RESERVE < DEFAULT_TOTAL


def test_tokens_are_the_two_halves_added():
    assert Usage(calls=2, input_tokens=10, output_tokens=5).tokens == 15
