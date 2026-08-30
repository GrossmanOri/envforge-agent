"""How many tokens one run may spend, how many it has, and whether the next call fits.

The bound is tokens rather than turns because a turn is not a fixed size. Every turn
resends everything before it, so the tenth costs several times the first, and a cap of
"ten turns" says nothing about the bill. Sitting 7's tool loop makes that acute: the
model reads files before it writes anything, and each thing it reads stays in the
conversation for every turn after.

Two rules shape this file.

A part of the total is reserved and cannot be spent on looking around. Investigation is
only worth anything if there is enough left afterwards for the call that turns it into a
Dockerfile, and a loop that spends its last token on one more file read has paid for
knowledge it can no longer use. `can_investigate` is that rule and has no caller until
sitting 7; `can_write` is the same question without the reserve, and today every call is
that call.

The estimate decides, the provider's numbers record. `estimate` is a guess and errs high
on purpose, so the loop stops one call early rather than one call late, and the guess is
never written into the ledger. Refunding an overestimate afterwards would make the
pessimism compound until the budget was a turn cap wearing a different name.

The asymmetry with the gate is deliberate. A loop built without a gate can build a
Dockerfile nobody checked, so `gate` has no default. A loop built without a budget
overspends money, so this one does.
"""

from __future__ import annotations

from dataclasses import dataclass

from .llm import MAX_TOKENS

# Deliberately below the real ratio, which is nearer four for English and lower for
# code. A low number makes the character count read as more tokens than it is.
CHARS_PER_TOKEN = 3

# One producing call at its worst: a prompt of this many characters, and a reply that
# runs all the way to the output ceiling. The ceiling half holds on all three providers,
# because every one of them is sent MAX_TOKENS: Anthropic as `max_tokens`, OpenAI and
# Groq as `max_completion_tokens`. A provider we did not send a ceiling to would make
# this whole estimate a guess about a limit that does not exist.
WORST_PROMPT = 48_000
DEFAULT_RESERVE = WORST_PROMPT // CHARS_PER_TOKEN + MAX_TOKENS
# Expressed in worst-case calls rather than as a round number, so the figure means
# something: enough for eight calls that each go as badly as a call can go.
DEFAULT_TOTAL = 8 * DEFAULT_RESERVE


@dataclass(frozen=True)
class Usage:
    """What a run has spent so far.

    `calls` counts requests sent to the model, including replies we could not use.
    Token totals count every reply that told us its usage, a refusal and a truncation
    included: a truncated reply consumed the whole output ceiling, which is what
    truncation means, so a ledger that skipped it could be walked past forever.
    """

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0

    @property
    def tokens(self) -> int:
        return self.input_tokens + self.output_tokens


def estimate(*texts: str) -> int:
    """What one call could cost at worst, before it is made.

    Nobody can know a call's price in advance, so this is a guess that errs high in
    both halves: characters are converted at a rate below the real one, and the reply
    is assumed to run to the output ceiling. Being wrong here means refusing a call
    that would have fit, which costs a run some quality. Being wrong the other way
    costs money nobody agreed to.
    """
    return sum(len(text) for text in texts) // CHARS_PER_TOKEN + MAX_TOKENS


@dataclass(frozen=True)
class Budget:
    """The policy, not the ledger. Frozen and holding no counters on purpose: an
    `Agent` is built once and may be run more than once, and a budget carrying its own
    spent total would let the first run quietly bound the second."""

    total: int = DEFAULT_TOTAL
    reserve: int = DEFAULT_RESERVE

    def __post_init__(self) -> None:
        if self.reserve > self.total:
            raise ValueError("the reserve cannot be larger than the whole budget")

    def can_write(self, usage: Usage, *texts: str) -> bool:
        """Room for the call that has to produce a Dockerfile. The reserve exists to
        be spent here, so it is not held back from this question."""
        return usage.tokens + estimate(*texts) <= self.total

    def can_investigate(self, usage: Usage, *texts: str) -> bool:
        """Room for a call that only learns something, leaving the reserve untouched.
        Nothing asks this yet. Sitting 7's tool loop is the caller, and the question
        has to exist before that loop is written, or the loop is written against a
        counter and converting it later is a rewrite rather than an argument."""
        return usage.tokens + estimate(*texts) + self.reserve <= self.total


DEFAULT = Budget()
