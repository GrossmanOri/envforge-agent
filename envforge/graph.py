"""The agent as a LangGraph graph. First slice: ask, look, submit, gate.

    START -> model -+-> inspect -> model        the model reading the script
                    |
                    +-> gate -+-> model         rejected, so try again
                    |         |
                    |         +-> END           passed (build and run land next)
                    +-> END                     nothing more to do

`model` is the only node that talks to the model. `inspect` is a `ToolNode` holding the
two read-only tools. `gate` is deterministic and the model cannot call it: submitting a
Dockerfile is a tool the graph routes on rather than executes, so the checking is done by
a node rather than by a tool the model chose to run.

State is plain data and travels between nodes. Nodes return only the fields they change
and LangGraph merges them, so a node's whole effect on a run is the dict it returns. The
model, the sandbox, the gate and the event sink are not state: they are runtime context,
because state gets checkpointed and none of those can or should be written down.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any, Iterator, Literal, Sequence

from langchain_core.messages import (AIMessage, AnyMessage, HumanMessage, RemoveMessage,
                                     SystemMessage, ToolMessage)
from langgraph.config import get_stream_writer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.prebuilt import ToolNode
from langgraph.runtime import Runtime

from .agent import (CONTEXT, Gate, DOCKERFILE_LIMIT, EVIDENCE_LIMIT, FIRST, LANGUAGES,
                    SCRIPT_LIMIT, SYSTEM, EngineFailure, Outcome, Usage,
                    default_dockerfile, describe, manifests)
from .context import Context
from .sandbox import (container_exists, container_running, force_stop,
                      remove_container, sweep)
from .events import Event
from .workspace import Workspace
from .llm import classify
from .sandbox import BuildResult, RunResult, labels_for
from .tools import (INSPECTION, SUBMIT, bound, inspection_tools, submit_dockerfile)

# How many times the model may look at the script in one attempt.
#
# A cap in code, never a sentence in the prompt: a rule written in a prompt is a request
# made of the thing the prompt is defending against. It is enforced by binding a
# different tool list once the budget is gone, so a withdrawn tool cannot be called
# however the conversation goes.
#
# It is a security control before it is a cost control. Each look returns a bounded slice
# of an untrusted script, and a model that asks for region after region has reassembled
# the file one piece at a time without breaking any other rule.
MAX_LOOKS = 4


class State(MessagesState):
    """One run's facts. Everything here is bounded and serialisable.

    Extends `MessagesState`, so `messages` carries the conversation with the model under
    the `add_messages` reducer. Everything else is scalars, strings and small lists.

    `messages` accumulating is exactly what makes the reset below necessary, and that is
    a security property rather than housekeeping: the bound on how much of the sample can
    reach one prompt assumes each attempt starts a fresh conversation. Without the reset,
    three attempts of four looks put twelve slices of the script in one context.
    """

    run_id: str
    language: str
    script: str                  # the filename
    full: str                    # the whole script text; the tools read from this
    files: dict[str, str]        # everything the workspace gathered, for the build
    args: list[str]              # arguments for the script, after the image name
    attempt: int
    calls: int                   # requests sent to the model, usable or not
    looks: int                   # of those, ones that read the script
    seen: int                    # looks used this attempt, against MAX_LOOKS
    candidate: str | None        # the Dockerfile the model last submitted
    base_image: str
    evidence: str | None         # why the last candidate did not work
    rejection: str | None        # what the gate said, if it refused
    # The run ended for a reason that is not about the sample at all, so nothing further
    # should be asked, built or run. Only the provider failing sets this.
    stopped: bool
    # This attempt found its own container already on the host, so it refused to execute
    # the sample again. Distinct from `stopped`: that is the provider failing, this is
    # the replay guard firing, and the difference decides whether the container is kept.
    interrupted: bool

    max_attempts: int
    max_refusals: int
    refusals: list[str]          # what the model said when it declined, in order
    used_fallback: bool          # the Dockerfile is ours, so it gets no repairs
    input_tokens: int
    output_tokens: int
    # Run-scoped, not attempt-scoped: the free rebuild after a timeout is offered once
    # per run, so a Dockerfile that always times out cannot buy a fresh retry every
    # attempt.
    rebuilt_after_timeout: bool
    retry_to: str                # where `retry` sends the run: "model" or "build"
    # Results as plain dicts rather than the frozen dataclasses they came from.
    # `BuildResult` round-trips through the checkpointer today and LangGraph warns that
    # deserialising unregistered types will be blocked, so the state holds shapes the
    # serialiser knows and the dataclass is rebuilt when the outcome is made.
    build: dict[str, Any] | None
    result: dict[str, Any] | None


def refusal_reason(reply: Any) -> str | None:
    """What the model said when it declined, or None if it did not.

    Two providers, two shapes, and neither is an exception: a refusal is a successful
    HTTP 200. Anthropic reports it as `stop_reason` "refusal" in the response metadata.
    OpenAI puts the text in a `refusal` field on the message, which LangChain carries in
    `additional_kwargs`.

    Kept apart from error handling on purpose, and this is the distinction the design
    rests on. A refusal is the model judging the sample, which is information about the
    sample. A dead key or an empty account is our infrastructure failing, which is
    information about us. Confusing them would mean writing our own Dockerfile, running
    it, and printing an ordinary verdict for a run the model never saw, which for a tool
    whose only product is a judgment about untrusted code is the worst failure available.
    """
    metadata = getattr(reply, "response_metadata", None) or {}
    if metadata.get("stop_reason") == "refusal":
        details = metadata.get("stop_details") or {}
        return str(details.get("explanation") or details or "no reason given")
    extra = getattr(reply, "additional_kwargs", None) or {}
    if extra.get("refusal"):
        return str(extra["refusal"])
    return None


def _emit(runtime: Runtime[Context], event: Event) -> None:
    """Put one event where a caller can see it, by both routes.

    The stream is how `Agent.run` yields, and it was the whole hole: the nodes wrote to
    the context sink, `Agent.run` streamed with `stream_mode="custom"`, and custom yields
    only what a node writes through LangGraph's writer. Nothing did, so the only engine
    in the project produced no events at all, and four tests drove that generator without
    asserting on a single thing it yielded.

    The context sink is kept as well, because a caller that drives the graph itself
    rather than through `Agent` has no stream to read, and every test here does exactly
    that. Two sinks and one call site, so neither can be forgotten separately.
    """
    get_stream_writer()(event)
    runtime.context.emit(event)


def model_node(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Ask the model once, with the tools this attempt is still allowed.

    The tool list is the cap. Once `seen` reaches `MAX_LOOKS` the inspection tools are
    not in the request at all, so the only call the model can make is the submission.
    """
    tools = list(inspection_tools(state["full"]))
    if state["seen"] >= MAX_LOOKS:
        tools = []
    binding: dict[str, Any] = {
        # One tool call per reply, asked of the provider. `route_model` refuses a reply
        # that arrives with more anyway, because this is a request and that is a rule.
        "parallel_tool_calls": False,
        # Force a tool call rather than allowing prose. A reply with no tool call is
        # still possible, since a refusal overrides this, and `unusable` handles the
        # rest; forcing removes the ordinary case rather than the adversarial one.
        "tool_choice": "any",
    }
    if runtime.context.strict:
        # Grammar-constrained arguments, from the two providers that promise them. Groq
        # is left out on purpose: it documents its schema guarantee as not applying to
        # tool use, so asking would be claiming something it does not give.
        binding["strict"] = True
    bound_model = runtime.context.model.bind_tools(tools + [submit_dockerfile],
                                                   **binding)
    _emit(runtime, Event("asking", f"attempt {state['attempt']}: asking the model"))
    try:
        reply = bound_model.invoke(state["messages"])
    except Exception as exc:                              # noqa: BLE001, narrowed below
        # Not repairable, and not a finding about the script. Asking again spends money
        # to fail identically, and falling back would print a verdict on a run the model
        # never saw. A rejected request is not an unreachable provider: the model was
        # reached and it answered, and one of those is worth retrying while the other is
        # a bug in us.
        #
        # This escaped the graph entirely until a review ran it: a dead key came out as
        # a raw SDK exception with no `finished` event, so a caller got no verdict and no
        # sign that one was missing.
        failure = classify(exc)
        if failure is None:
            raise
        if failure.kind == "rejected":
            reason = (f"the provider rejected our request, which is our bug rather than "
                      f"theirs: {failure}")
        else:
            reason = f"the model could not be reached ({failure.kind}): {failure}"
        _emit(runtime, Event("provider_unavailable", reason, {"kind": failure.kind}))
        _emit(runtime, finished_event(state, reason, ok=False,
                                      kind="rejected" if failure.kind == "rejected"
                                      else "unavailable"))
        return {"stopped": True, "calls": state["calls"] + 1}
    # Charged from the reply rather than estimated. `usage_metadata` is LangChain's
    # normalised shape, so this is the same arithmetic on every provider.
    used = getattr(reply, "usage_metadata", None) or {}
    charged = {"calls": state["calls"] + 1,
               "input_tokens": state["input_tokens"] + used.get("input_tokens", 0),
               "output_tokens": state["output_tokens"] + used.get("output_tokens", 0)}

    said = refusal_reason(reply)
    if said is None:
        return {"messages": [reply], **charged}

    # A refusal is the model judging the sample, which is a finding about the sample and
    # not a failure of ours. It never spends a repair attempt: it has its own counter,
    # because asking a model that has just declined to try again is not a repair.
    refusals = state["refusals"] + [said]
    _emit(runtime, Event("refused", f"the model declined: {said}",
                               {"reason": said}))
    if len(refusals) <= state["max_refusals"]:
        return {"messages": [reply], "refusals": refusals, **charged}

    # Declined twice, so we write the Dockerfile ourselves and stop asking. Its
    # existence is what makes a refusal survivable without another call, and it goes
    # through the same gate as anything the model wrote: one path to the daemon.
    _emit(runtime, Event("fell_back", "refused twice, using our own Dockerfile"))
    return {"messages": [reply], "refusals": refusals, "used_fallback": True,
            "candidate": default_dockerfile(state["language"], state["script"]),
            "base_image": LANGUAGES[state["language"]].base_image, **charged}


def route_model(state: State) -> str:
    """Where a reply goes, decided by which tool the model called.

    `tools_condition` cannot do this: it answers "tools" or "end", and there are two
    kinds of tool call here that must go to different places. Reading the name is also
    what keeps the submission away from `ToolNode`, so no tool the model calls can reach
    the gate.
    """
    if state["stopped"]:
        # The provider failed. Not repairable, not a finding about the script, and not a
        # reason to fall back: a verdict no judgment went into must not look like one.
        return END
    if state["used_fallback"] and state["candidate"]:
        # The model refused twice and we wrote the Dockerfile. It is checked like any
        # other: one path to the daemon, whoever wrote the file.
        return "gate"
    last = state["messages"][-1]
    if refusal_reason(last) is not None:
        # Declined, and the refusal counter is not spent yet, or the branch above would
        # have caught it. Asking again is not a repair, so this does not touch `retry`.
        return "model"
    calls = getattr(last, "tool_calls", None) or []
    if len(calls) > 1:
        # `parallel_tool_calls=False` asks the provider for one call per reply, and a
        # request made of the thing the prompt is defending against is not a rule. That
        # is the argument this file already makes about a withdrawn tool a few lines
        # below, and it was not made here: `ToolNode` answers every call in a message,
        # `seen` grew by one however many there were, and a reply carrying forty
        # `read_script` calls put 52,641 characters of a 60,000 character sample into one
        # prompt against a ceiling of 18,432.
        return "unusable"
    if not calls:
        # A reply that neither called a tool nor declined. Unusable rather than final:
        # ending here would report no verdict at all, so it spends an attempt and the
        # model is told what was wrong with the shape of its answer.
        return "unusable"
    if calls[0]["name"] == SUBMIT:
        return "gate"
    if calls[0]["name"] in INSPECTION:
        # The budget is checked here as well as when the tools are bound. Withdrawing a
        # tool from the request is what should make it uncallable, and that is a rule
        # enforced by the provider rather than by us: a model that names an inspection
        # tool anyway was served it, and a fake that ignores the withdrawal reassembled
        # the whole script and made 3,336 model calls. The graph does not take the
        # provider's word for it.
        return "inspect" if state["seen"] < MAX_LOOKS else "unusable"
    return "unusable"


def why_unusable(state: State) -> str:
    """What was wrong with the reply, in its own words.

    Four different things route here and they shared one hardcoded sentence, so a model
    that called a tool past the cap was told "your reply called no tool", lost an
    attempt, and was asked to repair a description of its own reply that was false.
    """
    last = state["messages"][-1]
    calls = getattr(last, "tool_calls", None) or []
    if len(calls) > 1:
        return (f"your reply made {len(calls)} tool calls. Make one at a time: call a "
                f"tool, read the answer, then decide what to do next.")
    if not calls:
        return ("your reply called no tool. Answer by calling one of the tools you were "
                "given, not with prose.")
    name = calls[0].get("name")
    if name in INSPECTION:
        return (f"you have used all {MAX_LOOKS} looks for this attempt, so {name} is no "
                f"longer available. Submit a Dockerfile with what you have.")
    return (f"you called {name!r}, which is not one of the tools you were given. Call "
            f"one of those.")


def unusable_node(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """A reply we cannot use. Repairable, but by rewriting the reply rather than the
    image, so it spends an attempt and the model is told what was actually wrong."""
    reason = why_unusable(state)
    _emit(runtime, Event("unusable_reply", reason))
    return {"retry_to": "model", "evidence": reason}


def _last_tool_call(messages: list[Any]) -> dict[str, Any] | None:
    """The most recent tool call the model made, searching backwards.

    Backwards from the end rather than at the end, because between the call and any node
    that wants to describe it there is a `ToolMessage` carrying the result.
    """
    for message in reversed(messages):
        calls = getattr(message, "tool_calls", None)
        if calls:
            return calls[0]
    return None


def counted_inspect(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Count the look and say what was read.

    Counting is a node rather than something inside a tool, because the cap is the
    graph's rule and not the tool's. A tool that counted its own uses would be the thing
    being limited keeping the tally.
    """
    # The last message here is the `ToolMessage` the `ToolNode` just appended, not the
    # `AIMessage` that asked for it, so the call has to be looked up rather than taken
    # from the end. Reading the end printed "the model called None with None" on every
    # real run, and no test saw it: they counted `looked` events and never read one.
    call = _last_tool_call(state["messages"]) or {}
    seen = state["seen"] + 1
    _emit(runtime, Event("looked",
                         f"attempt {state['attempt']}: the model called "
                         f"{call.get('name')} with {call.get('args')}",
                         {"tool": call.get("name"), "call": None, "result": "",
                          "run_id": state["run_id"]}))
    if seen == MAX_LOOKS:
        _emit(runtime, Event("tool_capped",
                             f"that was look {MAX_LOOKS} of {MAX_LOOKS} for this "
                             f"attempt. The looking tools are withdrawn and the model "
                             f"must submit now"))
    return {"seen": seen, "looks": state["looks"] + 1}


def _malformed(dockerfile: Any, base_image: Any) -> str | None:
    """Why a submission cannot be used, or None if it can.

    Shape only. Whether the Dockerfile is *allowed* is the gate's question and is asked
    next; this asks whether there is a Dockerfile at all.
    """
    for name, value in (("dockerfile", dockerfile), ("base_image", base_image)):
        if value is None:
            return f"your submission left out {name}. Send both fields."
        if not isinstance(value, str):
            return (f"{name} must be a string, and arrived as "
                    f"{type(value).__name__}. Send both fields as text.")
        if not value.strip():
            return f"your submission had an empty {name}. Send both fields."
    return None


def gate_node(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Check the submitted Dockerfile. Deterministic, and unreachable from a tool call.

    Reads the arguments off the submission the model made, answers that tool call with a
    `ToolMessage` so the conversation stays well formed, and records the verdict. What
    happens next is `route_gate`'s decision, not the model's.
    """
    last = state["messages"][-1]
    call = (getattr(last, "tool_calls", None) or [{}])[0]
    if state["used_fallback"]:
        # Ours, so there is no tool call to read it from and none to answer. It is
        # checked exactly like anything the model submitted.
        dockerfile, base_image = state["candidate"] or "", state["base_image"]
        answers: list[Any] = []
    else:
        args = call.get("args", {})
        dockerfile, base_image = args.get("dockerfile"), args.get("base_image")
        answers = [call]
        # Validated here because nothing else does. Every other tool's arguments are
        # checked by its own schema when `ToolNode` runs it, and this one is never run:
        # the graph routes on it. Anthropic and OpenAI grammar-constrain the arguments
        # anyway, but Groq documents its schema guarantee as not covering tool use, so
        # for Groq this is the only check there is. Without it a malformed submission
        # reached the gate as an empty string and was refused for the wrong reason.
        wrong = _malformed(dockerfile, base_image)
        if wrong is not None:
            _emit(runtime, Event("unusable_reply", wrong))
            return {"messages": [ToolMessage(content=wrong,
                                             tool_call_id=call.get("id", ""))],
                    "retry_to": "model", "evidence": wrong,
                    "rejection": wrong, "candidate": None}

    rejection = runtime.context.gate(dockerfile, base_image, frozenset(state["files"]))
    answer = ("the Dockerfile passed the gate" if rejection is None
              else f"the Dockerfile was rejected before it was built: {rejection}")
    # Every tool call is answered or the next request is refused for a dangling call.
    # The gate's own words go back to the model as the repair evidence.
    replies = [ToolMessage(content=answer, tool_call_id=c.get("id", ""))
               for c in answers]

    if rejection is not None:
        _emit(runtime, Event("gate_rejected", rejection, {"dockerfile": dockerfile}))
        if state["used_fallback"]:
            # Three things can go wrong with a Dockerfile we wrote ourselves, and in
            # all three the honest move is to stop and say which, never to ask a model
            # that has already declined twice.
            _emit(runtime, finished_event(
                state, f"our fallback Dockerfile was rejected: {rejection}",
                ok=False, kind="failed", dockerfile=dockerfile, used_fallback=True))
            return {"messages": replies, "candidate": dockerfile,
                    "base_image": base_image, "rejection": rejection,
                    "evidence": None, "retry_to": END}
        return {"messages": replies, "candidate": dockerfile,
                "base_image": base_image, "rejection": rejection, "evidence": answer}
    if not state["used_fallback"]:
        # `wrote` means the model wrote it. Our own fallback already announced itself
        # with `fell_back`, and saying the model wrote it would be a false sentence in
        # the one output that has to say who wrote what.
        _emit(runtime, Event("wrote", f"got {len(dockerfile)} characters",
                             {"base_image": base_image, "call": None,
                              "run_id": state["run_id"]}))
    return {"messages": replies, "candidate": dockerfile, "base_image": base_image,
            "rejection": None, "evidence": None}


def route_gate(state: State) -> Literal["build", "retry", "__end__"]:
    """A passing Dockerfile is built. A rejected one spends an attempt and goes back.

    Through `retry` rather than straight to the model, because a rejection is a failed
    attempt and the attempt cap has to see it. Routing it directly to the model would
    make a Dockerfile the gate always refuses loop until the graph's own recursion limit
    stopped it, which produces no verdict at all.
    """
    return "retry" if state["rejection"] is not None else "build"


def finished_event(state: State, reason: str, /, message: str | None = None,
             **fields: Any) -> Event:
    """The one event every ending produces, with the shared fields filled in.

    `message` differs from `reason` on the give-up path, which says "gave up after 3
    attempts" to a person and records "no Dockerfile worked in 3 attempts" as the
    outcome. Positional-only up to here, because `Outcome` has a field called `run`.
    """
    return Event("finished", message if message is not None else reason,
                 {"outcome": Outcome(
                     reason=reason, attempts=state["attempt"], run_id=state["run_id"],
                     usage=Usage(state["calls"], state["input_tokens"],
                                 state["output_tokens"], state["looks"]),
                     refusals=list(state["refusals"]),
                     build=BuildResult(**state["build"]) if state["build"] else None,
                     run=RunResult(**state["result"]) if state["result"] else None,
                     **fields)})


def build_node(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Build the image the gate just passed.

    Safe to replay. The tag is derived from the run and the attempt rather than from a
    clock or a counter, so a rebuild after a crash produces the same tag, and buildkit
    serves the layers it already has. Rebuilding costs time and changes nothing, which
    is what makes this the easy half of the replay question.
    """
    tag = f"envforge-{state['run_id']}:attempt{state['attempt']}"
    _emit(runtime, Event("building", f"building {tag}"))
    build = runtime.context.sandbox.build(state["candidate"], state["files"], tag,
                                          labels_for(state["run_id"]))
    return {"build": asdict(build)}


def route_build(state: State) -> Literal["run", "retry", "__end__"]:
    """Where a build goes, decided by what `after_build` already worked out.

    Routing on `retry_to` rather than re-reading the build, because the first version
    re-derived the timeout decision here and got it backwards: `after_build` sets
    `rebuilt_after_timeout` before this runs, so a route testing that flag saw the value
    from after the decision and ended the run instead of taking the free rebuild. One
    node decides, the route reads what it decided.
    """
    if state["build"] and BuildResult(**state["build"]).ok:
        return "run"
    return END if state["retry_to"] == END else "retry"


def after_build(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Say what the build did, and decide what the next attempt is for.

    Separate from `build_node` so the node that performs the side effect does nothing
    else, which is what keeps the replay question about one line.
    """
    build = BuildResult(**state["build"])
    if build.ok:
        return {}
    if build.timed_out:
        # A timeout is not a Dockerfile defect, so it must not spend a model call. The
        # same file is worth building once more for free: the incident this was written
        # for was a cold base image pulling past the ceiling, and buildkit keeps what it
        # pulled, so the retry starts warm. Once, not until it works.
        if not state["rebuilt_after_timeout"]:
            _emit(runtime, Event("build_failed",
                                       f"the build timed out after {build.seconds:.0f}s. "
                                       f"Trying the same Dockerfile once more, which "
                                       f"costs no tokens: a partly-pulled image is kept "
                                       f"and the retry starts warm"))
            return {"rebuilt_after_timeout": True, "retry_to": "build"}
        reason = (f"the build timed out after {build.seconds:.0f}s, twice. The Dockerfile "
                  f"asks for more work than the timeout allows, or the image cannot be "
                  f"pulled from here")
        _emit(runtime, Event("build_failed", reason))
        _emit(runtime, finished_event(state, reason, ok=False, kind="build_timeout",
                                      dockerfile=state["candidate"],
                                      used_fallback=state["used_fallback"]))
        return {"retry_to": END}
    _emit(runtime, Event("build_failed", f"build exited {build.exit_code}"))
    if state["used_fallback"]:
        _emit(runtime, finished_event(
            state, "our fallback Dockerfile did not build", ok=False, kind="failed",
            dockerfile=state["candidate"], used_fallback=True))
        return {"retry_to": END}
    return {"retry_to": "model", "evidence": bound(build.log, EVIDENCE_LIMIT)}


def container_name(state: State) -> str:
    """One name per attempt, derived rather than generated.

    Derived, because it is the durable evidence a resumed run reads. A random name would
    make the guard below unable to recognise its own container.
    """
    return f"envforge-{state['run_id']}-attempt{state['attempt']}"


def run_node(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Run the container, unless this attempt already ran one.

    The hard half of the replay question. Building twice wastes time; running twice
    executes an untrusted sample a second time, which is the one side effect in this
    program that must happen at most once.

    A checkpoint is written after a node returns, so a crash between the container
    exiting and the checkpoint committing means LangGraph replays this node. Nothing in
    the state can help, because the state is exactly what was not saved. The evidence
    has to be outside: the container is named from the run and the attempt, and `run`
    kills it and leaves it, and removal happens only once the result is durable, so a
    container still bearing this name was left behind by a process that died. Finding one means the sample already ran, and the honest
    answer is to stop and say so rather than to run it again or to invent a verdict.
    """
    name = container_name(state)
    if runtime.context.exists(name):
        # Stop it, and do not remove it. Two different things, and conflating them is
        # what this whole path exists to avoid.
        #
        # Stopping, because a process that died leaves its container behind and the
        # daemon does not stop it: the sample may still be executing right now, while we
        # are about to report the attempt as interrupted.
        #
        # Keeping, because the container is the only proof that this attempt already ran.
        # Removing it here would be the same mistake one layer along: the checkpoint for
        # this refusal can itself be lost in a crash, and the next resume would then find
        # no evidence and run the sample again.
        if runtime.context.running(name):
            runtime.context.stop_container(name)
        reason = ("this attempt already started a container and the run was interrupted "
                  "before its result was recorded. Refusing to run the sample a second "
                  "time")
        _emit(runtime, Event("exec_failed", reason))
        _emit(runtime, finished_event(state, reason, ok=False, kind="failed",
                                      dockerfile=state["candidate"]))
        return {"result": None, "interrupted": True}

    tag = f"envforge-{state['run_id']}:attempt{state['attempt']}"
    _emit(runtime, Event("running", f"running {tag}"))
    build = BuildResult(**state["build"])
    result = runtime.context.sandbox.run(build.image, state["args"], name=name,
                                         labels=labels_for(state["run_id"]))
    return {"result": asdict(result)}


def after_run(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Say what the container did, and only now remove it.

    This node runs after `run` returned, which means `run`'s writes are committed: the
    result is durable, so the container has stopped being the only record that the sample
    executed and can go. Removing it inside `run` is what opened the window where a crash
    left neither a checkpoint nor a container, and a resumed run executed the sample a
    second time.
    """
    if state["interrupted"]:
        # The one path that must not clean up. This node removes the container once the
        # result is durable, and on a normal run that is right. Here there is no result
        # and the container is the evidence, so removing it would throw away the only
        # thing standing between a later resume and a second execution of the sample.
        # An earlier version removed it here and a test asserted that, which was the
        # cleanup rule winning an argument it should have lost.
        return {}
    runtime.context.remove_container(container_name(state))
    if state["result"] is None:            # nothing ran, so there is nothing to report
        return {}
    result = RunResult(**state["result"])
    if result.start_error:
        # The daemon says the process never started, so the Dockerfile is wrong. This is
        # deliberately not a test on 126 or 127: a script that can produce anything has
        # already started.
        _emit(runtime, Event("exec_failed",
                                   f"the container never started: {result.start_error}"))
        if state["used_fallback"]:
            _emit(runtime, finished_event(
                state, "our fallback image could not run its command", ok=False,
                kind="failed", dockerfile=state["candidate"], used_fallback=True))
            return {"retry_to": END}
        return {"retry_to": "model",
                "evidence": ("the container never started its command. docker said:\n"
                             f"{bound(result.start_error, EVIDENCE_LIMIT)}")}
    # Whether the script succeeded is observable and the caller needs it: a nonzero exit
    # is a finding, not a malfunction of this tool.
    _emit(runtime, finished_event(
        state, f"the script ran and exited {result.exit_code}", ok=True,
        kind="ran" if result.exit_code == 0 else "script_failed",
        dockerfile=state["candidate"], used_fallback=state["used_fallback"]))
    return {}


def route_run(state: State) -> Literal["retry", "__end__"]:
    if state["result"] is None:
        return END
    return "retry" if RunResult(**state["result"]).start_error else END


def retry_node(state: State, runtime: Runtime[Context]) -> dict[str, Any]:
    """Spend an attempt, or give up because there are none left."""
    if state["attempt"] >= state["max_attempts"]:
        _emit(runtime, finished_event(
            state, f"no Dockerfile worked in {state['attempt']} attempts",
            message=f"gave up after {state['attempt']} attempts",
            ok=False, kind="no_image", dockerfile=state["candidate"],
            used_fallback=state["used_fallback"]))
        return {"retry_to": END}
    return new_attempt(state)


def route_retry(state: State) -> Literal["model", "build", "__end__"]:
    return state["retry_to"] if state["retry_to"] in ("model", "build") else END


def new_attempt(state: State) -> dict[str, Any]:
    """Start an attempt: bump the counter and clear the conversation.

    The conversation is cleared because the bound on how much of the untrusted script can
    reach one prompt is a per-prompt bound, and `add_messages` accumulates. Keeping the
    old messages would carry every earlier attempt's slices into the new context, so
    three attempts of four looks would put twelve slices in front of the model at once.

    The system message and the first human message survive, because they are ours: the
    instructions and the description of the script. Everything the model said and
    everything a tool returned goes.
    """
    # Exactly the first two: the system message and the opening one carrying the
    # script. Not "the leading run of system and human messages", which is what this
    # said and which was wrong in a way that grew: the repair message appended below is
    # itself a HumanMessage, so on the next attempt it had joined that leading run and
    # was never removed. Repair messages then accumulated one per attempt, and the bound
    # invariant 24 states became linear in `max_attempts` instead of constant. Measured
    # at twelve attempts: 24,876 characters of the sample in one prompt against a
    # ceiling of 22,528.
    removals = [RemoveMessage(id=m.id) for m in state["messages"][2:] if m.id]

    # The reset takes the reason with it, so the reason comes back as a message. This is
    # the repair prompt: without it a rejected Dockerfile is retried by a model that no
    # longer knows what was wrong with it, which the tests caught immediately.
    #
    # Both halves bounded. `evidence` already is, at the point it is produced. The
    # candidate is bounded here because it is the model's own text quoting a script it
    # has just read, and it survives across attempts: a Dockerfile whose comments hold
    # what the model read would otherwise carry those slices into the next attempt on
    # top of that attempt's own fresh look budget.
    told = []
    if state["evidence"]:
        previous = state["candidate"] or ""
        told = [HumanMessage(content=(
            f"Your last Dockerfile did not work.\n\n"
            f"--- what you submitted ---\n{bound(previous, DOCKERFILE_LIMIT)}\n"
            f"--- end ---\n\n{bound(state['evidence'], EVIDENCE_LIMIT)}\n\n"
            f"Submit a corrected Dockerfile."))]
    return {"messages": removals + told, "attempt": state["attempt"] + 1, "seen": 0}


def build_graph(script_text: str, checkpointer=None):
    """Three nodes and two routes.

    `inspect` is a `ToolNode` wrapping the read-only tools, followed by `counted`, which
    does the counting.

    The script is a parameter because the tools close over it, and they close over it so
    that no tool call can name a file. A graph compiled once for every run and handed the
    text at call time would need the tools to take a filename, which is the thing this
    design does not have. Compiling is cheap; one graph per run is the cost of that.
    """
    graph = StateGraph(State, context_schema=Context)
    graph.add_node("model", model_node)
    graph.add_node("inspect", ToolNode(inspection_tools(script_text)))
    graph.add_node("counted", counted_inspect)
    graph.add_node("gate", gate_node)

    graph.add_node("build", build_node)
    graph.add_node("after_build", after_build)
    graph.add_node("run", run_node)
    graph.add_node("after_run", after_run)
    graph.add_node("retry", retry_node)

    graph.add_edge(START, "model")
    graph.add_node("unusable", unusable_node)
    graph.add_conditional_edges("model", route_model,
                                {"inspect": "inspect", "gate": "gate",
                                 "model": "model", "unusable": "unusable", END: END})
    graph.add_edge("unusable", "retry")
    graph.add_edge("inspect", "counted")
    graph.add_edge("counted", "model")
    graph.add_conditional_edges("gate", route_gate,
                                {"build": "build", "retry": "retry", END: END})
    # The side effect and the decision are separate nodes throughout. `build` and `run`
    # do one thing each and record it; `after_build` and `after_run` read what happened
    # and say what it means. That is what keeps the replay question about a single line
    # rather than about a node that also emits, routes and reasons.
    graph.add_edge("build", "after_build")
    graph.add_conditional_edges("after_build", route_build,
                                {"run": "run", "retry": "retry", END: END})
    graph.add_edge("run", "after_run")
    graph.add_conditional_edges("after_run", route_run, {"retry": "retry", END: END})
    graph.add_conditional_edges("retry", route_retry,
                                {"model": "model", "build": "build", END: END})
    return graph.compile(checkpointer=checkpointer)


def start_state(run_id: str, language: str, script: str, full: str,
                files: dict[str, str], system: str, first: str,
                args: list[str] | None = None, max_attempts: int = 3,
                max_refusals: int = 1) -> State:
    """The state a run begins as."""
    return State(
        messages=[SystemMessage(content=system), HumanMessage(content=first)],
        run_id=run_id, language=language, script=script, full=full, files=files,
        args=list(args or []),
        attempt=1, calls=0, looks=0, seen=0,
        candidate=None, base_image="", evidence=None, rejection=None,
        stopped=False, interrupted=False,
        max_attempts=max_attempts, max_refusals=max_refusals, refusals=[],
        used_fallback=False, input_tokens=0, output_tokens=0,
        rebuilt_after_timeout=False, retry_to="model", build=None, result=None,
    )


def step_ceiling(max_attempts: int, max_refusals: int) -> int:
    """How many node visits a run can make, plus room.

    LangGraph stops a graph at `recursion_limit` and raises. Its default of 25 is close
    enough to what this machine legitimately uses that a longer run would hit it, and the
    failure matters more than the number: every ending here is a `finished` event
    carrying an `Outcome`, and a graph stopped by its own limit emits none, so the run
    would end with no verdict rather than a bad one.

    Derived from the caps that actually bound a run rather than guessed. Per attempt: one
    model call per look, the look's two nodes, the call that finally submits, and the
    gate, build and run nodes with their `after` pair. Refusals are free calls counted
    once for the run. If this ever fires it is a bug in the derivation, because
    `max_attempts` is the bound that is supposed to stop a run.
    """
    return max_attempts * (4 * MAX_LOOKS + 10) + max_refusals + 10


def first_prompt(language: str, script: str, full: str,
                 files: dict[str, str]) -> str:
    """The opening message: the script, bounded, and what is missing from it.

    Bounded because the script is untrusted text on its way into a prompt. The notice
    saying how long the whole file is and which offsets were removed is what makes the
    inspection tools usable: an offset means nothing without it, and a model that cannot
    tell it is looking at a truncated file has no reason to look.

    Built from the same templates the rest of the project uses rather than a second copy
    of them, so a change to how a script is presented reaches this too.
    """
    shown = bound(full, SCRIPT_LIMIT)
    context = CONTEXT.format(language=language, name=script, text=shown,
                             about=describe(full, shown),
                             files=manifests(files, script))
    # `.format` on the template, never on the result. The context holds the sample, and a
    # script full of f-strings and dict literals is a string full of braces: formatting
    # it a second time raises KeyError on the sample's own text.
    return FIRST.format(context=context, previous=None, evidence=None)


class Agent:
    """One script in, a stream of events out, and nothing of ours left on the machine.

    Owns the run, which is what makes it the right place for cleanup: the images an
    attempt built are this object's to remove, and removing them belongs in a `finally`
    so it happens whether the run ended with a verdict, an exception or a keyboard
    interrupt.

    Cleanup lives here rather than in the command line, where it used to. That was fine
    while the command line was the only caller, and it meant this engine leaked an image
    per attempt for every run driven by anything else, which was every run in this
    implementation until now.

    `gate` has no default on purpose. Every Dockerfile reaching the daemon was written by
    a model that had just read untrusted text, so an agent that can be built without a
    gate is an agent that can build one unchecked.
    """

    def __init__(self, llm: Any, sandbox: Any, gate: Gate, max_attempts: int = 3,
                 max_refusals: int = 1, strict: bool = False, exists=None, remove=None,
                 sweeper=None, running=None, stop=None) -> None:
        self.llm, self.sandbox, self.gate = llm, sandbox, gate
        self.max_attempts, self.max_refusals = max_attempts, max_refusals
        self.strict = strict
        # The three host lookups, injectable for the same reason the sandbox is. They
        # reach for the docker binary, and a test of the graph's decisions should not
        # need one: leaving them hard-wired made four unit tests require a daemon and
        # perform real removals on the developer's machine.
        # Resolved here rather than as default arguments, which bind at definition time:
        # a caller that replaces one of these on the module afterwards, which is what a
        # test without a daemon does, would otherwise be ignored entirely.
        self.exists = exists or container_exists
        self.remove = remove or remove_container
        self.sweeper = sweeper or sweep
        self.running = running or container_running
        self.stop = stop or force_stop

    def run(self, workspace: Workspace, language: str, args: Sequence[str] = (),
            checkpointer=None,
            config: dict[str, Any] | None = None) -> Iterator[Event]:
        """Stream the events the nodes produce, as they produce them.

        `stream_mode="custom"` is what a node writes while it works, so an event arrives
        here the moment it is made rather than when its node returns.
        """
        run_id = uuid.uuid4().hex
        # Collect the images earlier runs left behind, before making anything of our
        # own. Not a background chore: a crashed run leaves a tagged image that nothing
        # else removes, and the alternative is that they accumulate until somebody
        # notices the disk. Guarded by ownership and age, so a run in another terminal
        # right now is never touched.
        #
        # Images only. A crashed run's container is left where it is, because it is the
        # proof that its attempt already executed an untrusted sample; see invariant 32.
        #
        # Best effort about the daemon, deliberately. A sweep is housekeeping, and no
        # part of it is worth refusing to start a run over.
        try:
            for gone in self.sweeper(keep=run_id):
                yield Event("swept",
                            f"removed {gone}, left by a run that did not finish")
        except OSError:
            pass
        script = workspace.script
        files = {name: workspace.read(name) for name in workspace.names()}
        full = files[script]
        state = start_state(run_id, language, script, full, files,
                            SYSTEM, first_prompt(language, script, full, files),
                            args=list(args), max_attempts=self.max_attempts,
                            max_refusals=self.max_refusals)
        graph = build_graph(full, checkpointer=checkpointer)
        # No second sink here. The nodes write to the stream, and the stream is what this
        # generator yields; an extra list collected on the side is what hid the fact that
        # nothing was reaching a caller at all.
        context = Context(model=self.llm, strict=self.strict, gate=self.gate,
                          sandbox=self.sandbox,
                          exists=self.exists, remove_container=self.remove,
                          running=self.running, stop_container=self.stop)
        limit = step_ceiling(self.max_attempts, self.max_refusals)
        try:
            yield from graph.stream(state, {**(config or {}),
                                            "recursion_limit": limit},
                                    context=context, stream_mode="custom")
        except GraphRecursionError as exc:
            # Derived to be unreachable, so reaching it is a bug in the derivation rather
            # than a run that deserved to stop. Translated here because a run with no
            # verdict must never reach a shell as an unhandled traceback, which this
            # project's exit codes define as the script having run and failed.
            raise EngineFailure(
                f"the graph exceeded {limit} steps, which its own bound says is "
                f"impossible, so the run produced no verdict: {exc}") from exc
        finally:
            # Unconditional, and scoped to this run. `built_tags` may hold tags from
            # other runs if a sandbox is shared, so the prefix is the ownership check:
            # a run removes what it made and nothing else.
            for tag in list(getattr(self.sandbox, "built_tags", [])):
                if tag.startswith(f"envforge-{run_id}:"):
                    self.sandbox.remove_image(tag)
