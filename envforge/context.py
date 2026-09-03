"""What a run needs that must never be written into graph state.

LangGraph checkpoints state. That is the point of it, and it is also the rule that
decides what may live there: state is the run's facts, saved and restored, so it has to
be plain serialisable data. Everything here is the opposite of that. A chat model holds
an HTTP client and an API key. A sandbox talks to a daemon. An event sink is a callable
belonging to whoever started the run.

Passed as LangGraph runtime context instead, which reaches every node and is never
persisted. Keeping the two apart is not tidiness: a checkpoint containing a credential
is a credential written to wherever checkpoints go.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from .events import Event


def _discard(event: Event) -> None:
    """The default sink. A run with nobody listening still runs."""


@dataclass
class Context:
    """The live things one run is given.

    `gate` has no default on purpose. Every Dockerfile reaching the daemon was written by
    a model that had just read untrusted text, so a run that can start without a gate is
    a run that can build one unchecked.
    """

    model: Any
    gate: Callable[[str, str, frozenset[str]], str | None]
    sandbox: Any = None
    emit: Callable[[Event], None] = field(default=_discard)
    # Whether a container of this name is still on the host. The replay guard: `run`
    # kills its container and leaves it, and removal happens only once the result is
    # durable, so one that survived was left by a process that died, which is how a
    # resumed run learns the sample already ran. In context
    # rather than imported directly by the node, so a test can answer it without Docker.
    exists: Callable[[str], bool] = field(default=lambda name: False)
    # Remove a container once its result is durable. Called from the node *after* the
    # one that ran it, because a checkpoint commits when a node returns: by the time the
    # next node runs, the result has been written down and the container has stopped
    # being the only record of it.
    remove_container: Callable[[str], None] = field(default=lambda name: None)
    # Whether a container is running, and how to stop one without removing it. The two
    # are separate on purpose: stopping ends an execution, removing destroys the evidence
    # that an execution happened, and the replay guard needs the first without the second.
    running: Callable[[str], bool] = field(default=lambda name: False)
    stop_container: Callable[[str], None] = field(default=lambda name: None)
