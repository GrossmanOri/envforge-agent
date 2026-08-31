"""What can happen during a run, and who wrote each string when it does.

This module is the seam between engines. The plain loop yields these events and the
LangGraph port will yield the same ones, because a topology-shaped interface cannot be
honoured by a plain loop while a vocabulary can be honoured by both. Making the set
closed is what turns that from an intention into a check: an engine that invents
`node_entered` fails at construction rather than producing a trace with a kind nobody
can label.

The labels are the part that cannot be added later. A reader of the finished trace
cannot tell whether a string was written by us, by the model, by a container or by the
files the run was handed, and neither can the trace module: only the code that emits
the event knows. The trace module renders these records, and a renderer that guesses wrong
about which text is attacker-controlled guesses wrong in a browser.

Nothing consumes the labels yet. They are written now because emission is the only
place the answer exists.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class Provenance(Enum):
    """Who a string came from.

    `US` means every character was written by this program. Everything else is
    text somebody outside it produced, and each name says which outsider, because
    the report has to be able to say where a claim came from.
    """

    US = "us"
    # The run's own inputs: the script, the files gathered beside it, and the
    # arguments the caller supplied. Untrusted, and the first thing an attacker
    # controls if they control anything.
    INPUT = "input"
    MODEL = "model"
    # A tool result. Nothing emits one yet; the tool loop will.
    TOOL = "tool"
    CONTAINER = "container"


@dataclass(frozen=True)
class Kind:
    """One event that can happen. `authors` covers the message and every data key.

    A set rather than one value per string, because most of these strings have more
    than one author: `gate_rejected` is our sentence quoting the model's line back at
    it. Picking a single label would mean ranking the model against a container, and
    there is no honest ranking there. The set says which sources contributed, and a
    consumer's real question is answered by `authors == {US}` or not.

    The set is the union over every path that emits the kind, not a description of
    one occurrence. `finished` says `INPUT` because one of its five paths splices in
    the language the caller asked for, even though the other four do not. That is
    coarse on purpose: this table is checkable and a label chosen at each emission
    is not, and a seam that every emitter fills in freely is not a contract.
    """

    name: str
    authors: Mapping[str, frozenset[Provenance]]


US, INPUT, MODEL, CONTAINER = (
    Provenance.US, Provenance.INPUT, Provenance.MODEL, Provenance.CONTAINER)

# One rule the table applies without saying so on every line: a number we format into
# our own sentence does not make whoever chose it an author. "build exited 137" is ours
# even though the container chose the 137, because a value can be chosen while the text
# cannot. The moment a message splices in a string somebody else wrote, that changes.


def _kind(name: str, message: tuple[Provenance, ...], **data: tuple[Provenance, ...]) -> Kind:
    """The message is a parameter rather than a data key, so a data key called
    "message" cannot be declared and cannot collide with it."""
    return Kind(name, {"message": frozenset(message),
                       **{key: frozenset(value) for key, value in data.items()}})


VOCABULARY: dict[str, Kind] = {kind.name: kind for kind in (
    _kind("asking", (US,)),
    _kind("refused", (US, MODEL),          # str(exc) quotes what the provider said
          reason=(MODEL,)),
    _kind("fell_back", (US,)),
    # A count of tokens against a total, both of them ours.
    _kind("budget_spent", (US,)),
    # str(exc) on InvalidArguments names the offending field, which the model chose.
    _kind("unusable_reply", (US, MODEL)),
    _kind("wrote", (US,),                  # a character count and nothing else
          base_image=(MODEL,),
          # The request carries our system prompt and the script; the response is the
          # model's. One value could not say that, which is why these are sets.
          call=(US, INPUT, MODEL),
          run_id=(US,)),
    # The Dockerfile is the model's on every path but one: after two refusals the file
    # we wrote ourselves goes through the same gate and can be rejected there too.
    _kind("gate_rejected", (US, MODEL),    # our reason, quoting the line it refused
          dockerfile=(US, MODEL)),
    _kind("building", (US,)),
    _kind("build_failed", (US,)),          # an exit code, never the log
    _kind("running", (US,)),
    # Docker's own words, but the part that varies is the model's ENTRYPOINT being
    # reported back. The daemon is not a fifth author; it is quoting the model.
    _kind("exec_failed", (US, MODEL)),
    _kind("finished", (US, INPUT),         # one path names the language it was asked for
          # Dockerfile and refusals from the model, build log and output from the
          # container, and the reason from us.
          outcome=(US, INPUT, MODEL, CONTAINER)),
)}


@dataclass(frozen=True)
class Event:
    """One thing that happened, in order. `data` is for machines, `message` for people.

    The kind and the exact set of data keys are checked here, at construction, which
    is the only place both the engine and the trace pass through. A missing key is as
    much a drift as an unknown one: a consumer reading `event.data["outcome"]` has
    every right to expect it on a `finished`.
    """

    kind: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        declared = VOCABULARY.get(self.kind)
        if declared is None:
            raise ValueError(f"{self.kind!r} is not an event this project emits")
        expected = set(declared.authors) - {"message"}
        if set(self.data) != expected:
            raise ValueError(
                f"{self.kind!r} carries {sorted(expected)}, got {sorted(self.data)}")

    def authors(self, key: str = "message") -> frozenset[Provenance]:
        """Who wrote one string on this event. `authors() == {Provenance.US}` is the
        question a renderer actually asks; the rest is for the report."""
        try:
            return VOCABULARY[self.kind].authors[key]
        except KeyError:
            raise KeyError(f"{self.kind!r} has no {key!r}") from None
