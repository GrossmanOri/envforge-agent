"""The agent, as a graph. This file is the whole engine.

Three nodes and one state.

    START -> author -+-> look -> author         the model reading the script
                     |
                     +-> execute -+-> author    the repair loop
                                  |
                                  +-> END

`author` asks the model. `look` answers the model's question about the script. `execute`
gates the Dockerfile, builds it and runs it. A node reads the state, does one thing, and
returns only the fields it changed. LangGraph merges those into the state and reads
`step` to decide which node runs next.

Nothing mutates the state in place. A node that spends an attempt returns
`{"attempt": state["attempt"] + 1}`, and that is the only way the number ever changes.
That matters beyond tidiness: the state is what a checkpointer would save, what a resumed
run would restore, and what anyone debugging a run reads between two nodes. An earlier
version carried one mutable object through the graph and mutated it inside the nodes,
which looks identical from outside and is a loop wearing a graph costume.

Events do not travel in the state. Each node writes them to LangGraph's stream as it
produces them, so a caller sees `building` when the build starts rather than when the
node returns.
"""

from __future__ import annotations

import uuid
from typing import Any, Iterator, Sequence, TypedDict

from langgraph.config import get_stream_writer
from langgraph.errors import GraphRecursionError
from langgraph.graph import END, StateGraph

from .agent import (EVIDENCE_LIMIT, LANGUAGES, MAX_LOOKS, READ_SCRIPT, SEARCH_SCRIPT,
                    SYSTEM, WRITE_DOCKERFILE, EngineFailure, Gate, Outcome, Usage,
                    bound, context_for, default_dockerfile, look, prompt)
from .events import Event
from .llm import (LLM, Answered, Call, InvalidArguments, LLMError, ProviderUnavailable,
                  Refused, Truncated)
from .sandbox import BuildResult, Sandbox
from .workspace import Workspace

# The node a run goes to next. Plain strings, because they are the node names too.
AUTHOR, LOOK, EXECUTE, DONE = "author", "look", "execute", "done"


class State(TypedDict):
    """Everything one run knows, and the only thing that travels between nodes.

    Flat and plain on purpose. These were the local variables of a `while` loop, then the
    fields of a mutable object passed by reference; both hid the run's state from the
    graph. Here every field is visible to LangGraph, so a node's entire effect on a run
    is the dict it returns.

    What is not here: the model, the sandbox and the gate. Those are objects rather than
    data, and they never change during a run, so the `Agent` holds them and the nodes
    close over them. Keeping them out is what leaves this a plain data structure, which
    is what a checkpointer would need to write down.
    """

    # Set once, never changed.
    run_id: str
    language: str
    script: str
    files: dict[str, str]
    full: str                    # the whole script; the looking tools read from this
    context: str                 # the prompt block describing the script and manifests
    args: tuple[str, ...]
    max_attempts: int
    max_refusals: int

    # What the run has spent.
    attempt: int
    calls: int
    input_tokens: int
    output_tokens: int
    looks: int
    refusals: list[Any]

    # The conversation with the model. Both reset at the start of every attempt.
    history: list[Answered]
    seen: int                    # looks used this attempt, against MAX_LOOKS
    pending: Call | None         # a tool call `author` made and `look` will answer

    # Carried between attempts.
    dockerfile: str | None
    base_image: str
    previous: str | None         # the last Dockerfile, for the repair prompt
    evidence: str | None         # why the last one did not work
    used_fallback: bool
    rebuilt_after_timeout: bool
    build: BuildResult | None

    step: str                    # which node runs next


def usage(state: State) -> Usage:
    return Usage(state["calls"], state["input_tokens"], state["output_tokens"],
                 state["looks"])


def charged(state: State, reply: Call | LLMError) -> dict[str, int]:
    """What a reply cost, as a state update.

    Charged whether the reply was usable or not: a truncated one burned the whole output
    ceiling, so a ledger counting only successes under-reports what a run cost.
    """
    return {"calls": state["calls"] + 1,
            "input_tokens": state["input_tokens"] + reply.input_tokens,
            "output_tokens": state["output_tokens"] + reply.output_tokens}


def finished(state: State, reason: str, /, message: str | None = None,
             **fields: Any) -> Event:
    """The event every ending produces, with the fields they all share filled in.

    `message` differs from `reason` on one path: giving up says "gave up after 3
    attempts" to a person and records "no Dockerfile worked in 3 attempts" as the
    outcome's reason. Collapsing them once changed what the command line printed.

    Positional-only up to here, because `Outcome` has a field called `run` holding the
    container result, which would otherwise collide with a parameter of the same name.
    """
    return Event("finished", message if message is not None else reason,
                 {"outcome": Outcome(reason=reason, attempts=state["attempt"],
                                     usage=usage(state), refusals=state["refusals"],
                                     run_id=state["run_id"], **fields)})


def dead_end(state: State, reason: str, /, **fields: Any) -> Event:
    """The end of the road, always after the fallback. Three things can go wrong with a
    Dockerfile we wrote ourselves, and in all three the honest move is to stop and say
    which, never to ask the model again."""
    return finished(state, reason, ok=False, kind="failed", used_fallback=True,
                    dockerfile=state["dockerfile"], **fields)


def next_attempt(state: State, emit, then: str) -> dict[str, Any]:
    """Spend an attempt and carry on, or give up because there are none left.

    `then` is AUTHOR everywhere except the free rebuild after a build timeout, which
    spends an attempt and returns to EXECUTE without a model call. A timeout is not a
    Dockerfile defect, so asking the model again is asking the wrong question at full
    price, but the build is worth repeating once because buildkit keeps what it pulled.
    """
    if state["attempt"] >= state["max_attempts"]:
        emit(finished(state, f"no Dockerfile worked in {state['attempt']} attempts",
                      message=f"gave up after {state['attempt']} attempts",
                      ok=False, kind="no_image", dockerfile=state["previous"],
                      used_fallback=state["used_fallback"]))
        return {"step": DONE}
    # A new attempt writes a new prompt, so the conversation and the look budget both
    # start again: a repair prompt is written to stand alone, and the look cap bounds how
    # much of the sample any single prompt can hold.
    return {"step": then, "attempt": state["attempt"] + 1, "history": [], "seen": 0}


# --- the three nodes ------------------------------------------------------------------

def author(state: State, llm: LLM) -> dict[str, Any]:
    """Ask the model once. It answers with a tool call, and which tool it called is what
    decides where the run goes next."""
    emit = get_stream_writer()
    emit(Event("asking", f"attempt {state['attempt']}: asking the model"))

    # The look cap is enforced by withdrawing the tools, never by asking the model to
    # stop using them. A rule in a prompt is a request made of the thing the prompt is
    # defending against; a tool that is not in the request cannot be called at all.
    tools = ([SEARCH_SCRIPT, READ_SCRIPT, WRITE_DOCKERFILE]
             if state["seen"] < MAX_LOOKS else [WRITE_DOCKERFILE])
    user = prompt(state["context"], state["previous"], state["evidence"])

    try:
        call = llm.call(SYSTEM, user, tools, state["history"])
    except Refused as exc:
        refusals = state["refusals"] + [exc.reason]
        emit(Event("refused", str(exc), {"reason": exc.reason}))
        update = {**charged(state, exc), "refusals": refusals}
        if len(refusals) <= state["max_refusals"]:
            return {**update, "step": AUTHOR}   # the refusal counter, not the repair one
        emit(Event("fell_back", "refused twice, using our own Dockerfile"))
        return {**update, "step": EXECUTE, "used_fallback": True,
                "dockerfile": default_dockerfile(state["language"], state["script"]),
                "base_image": LANGUAGES[state["language"]].base_image}
    except ProviderUnavailable as exc:
        # Not repairable, and not a finding about the script. Asking again spends money
        # to fail identically, and falling back would print a verdict on a run the model
        # never saw. A rejected request is not an unreachable provider: the model was
        # reached and it answered, and one of those is worth retrying while the other is
        # a bug in us.
        if exc.kind == "rejected":
            reason = (f"the provider rejected our request, which is our bug rather than "
                      f"theirs: {exc}")
        else:
            reason = f"the model could not be reached ({exc.kind}): {exc}"
        emit(Event("provider_unavailable", reason, {"kind": exc.kind}))
        emit(finished(state, reason, ok=False,
                      kind="rejected" if exc.kind == "rejected" else "unavailable"))
        return {"step": DONE, "calls": state["calls"] + 1}
    except (InvalidArguments, Truncated, LLMError) as exc:
        # Repairable, but by rewriting the reply rather than the image.
        emit(Event("unusable_reply", str(exc)))
        # Bounded like every other evidence path. This one was missed once, and a
        # provider message carrying model-chosen text put 200,000 characters into the
        # next prompt.
        update = {**charged(state, exc), "evidence": bound(str(exc), EVIDENCE_LIMIT)}
        return {**update, **next_attempt({**state, **update}, emit, AUTHOR)}

    update = charged(state, call)
    if call.name != WRITE_DOCKERFILE.name:
        # A look, not an answer. `look` will run the tool and put the result on the
        # transcript. Nothing here lets the model touch the run: it chose what to read,
        # not whether an attempt is spent or whether anything is built.
        return {**update, "step": LOOK, "pending": call}

    emit(Event("wrote", f"got {len(call.arguments['dockerfile'])} characters",
               {"base_image": call.arguments["base_image"], "call": call,
                "run_id": state["run_id"]}))
    return {**update, "step": EXECUTE,
            "dockerfile": call.arguments["dockerfile"],
            "base_image": call.arguments["base_image"]}


def look_node(state: State) -> dict[str, Any]:
    """Answer the model's question with a bounded, labelled slice of the script.

    This is the tool loop. The model asked for a region or a search in `author`, this
    node runs it, and the answer goes onto `history` so the next `author` call sees it as
    the reply to its own question.
    """
    call = state["pending"]
    if call is None:                        # only reachable by driving the graph wrongly
        raise EngineFailure("the look node was entered with no tool call waiting")

    emit = get_stream_writer()
    result = look(state["full"], call)      # bounded and labelled where it is produced
    seen = state["seen"] + 1
    emit(Event("looked",
               f"attempt {state['attempt']}: the model called {call.name} "
               f"with {call.arguments}",
               {"tool": call.name, "call": call, "result": result,
                "run_id": state["run_id"]}))
    if seen == MAX_LOOKS:
        emit(Event("tool_capped",
                   f"that was look {MAX_LOOKS} of {MAX_LOOKS} for this attempt. The "
                   f"looking tools are withdrawn and the model must write now"))
    return {"step": AUTHOR, "pending": None, "seen": seen,
            "looks": state["looks"] + 1,
            "history": state["history"] + [Answered(call, result)]}


def execute(state: State, sandbox: Sandbox, gate: Gate) -> dict[str, Any]:
    """Gate the Dockerfile, build it, run it, and decide whether another attempt could
    help. A failure a rewrite cannot fix must not spend one."""
    emit = get_stream_writer()
    dockerfile = state["dockerfile"]
    if dockerfile is None:                  # only reachable by driving the graph wrongly
        raise EngineFailure("the execute node was entered with no Dockerfile")

    # A COPY may name any file the workspace gathered, and nothing else.
    rejection = gate(dockerfile, state["base_image"], frozenset(state["files"]))
    if rejection is not None:
        emit(Event("gate_rejected", rejection, {"dockerfile": dockerfile}))
        if state["used_fallback"]:
            emit(dead_end(state, f"our fallback Dockerfile was rejected: {rejection}"))
            return {"step": DONE}
        # Bounded like the other evidence paths. The gate caps both the file and what a
        # reason may quote, so this is belt as well as braces, and it is here because
        # this was the one evidence site without it.
        update = {"previous": dockerfile, "dockerfile": None,
                  "evidence": bound(f"the Dockerfile was rejected before it was built: "
                                    f"{rejection}", EVIDENCE_LIMIT)}
        return {**update, **next_attempt(state, emit, AUTHOR)}

    tag = f"envforge-{state['run_id']}:attempt{state['attempt']}"
    emit(Event("building", f"building {tag}"))
    build = sandbox.build(dockerfile, state["files"], tag)

    if not build.ok and build.timed_out:
        # A timeout is not a Dockerfile defect, so it must not spend a model call. But
        # the same file is worth building once more for free: the incident this was
        # written for was a cold base image pulling past the ceiling, and buildkit keeps
        # what it pulled, so the retry starts warm. Once, not until it works, or a
        # Dockerfile that genuinely asks for more work than the timeout allows would
        # retry forever at full build cost.
        if not state["rebuilt_after_timeout"]:
            emit(Event("build_failed",
                       f"the build timed out after {build.seconds:.0f}s. Trying the same "
                       f"Dockerfile once more, which costs no tokens: a partly-pulled "
                       f"image is kept and the retry starts warm"))
            return {"build": build, "rebuilt_after_timeout": True,
                    **next_attempt(state, emit, EXECUTE)}
        reason = (f"the build timed out after {build.seconds:.0f}s, twice. The Dockerfile "
                  f"asks for more work than the timeout allows, or the image cannot be "
                  f"pulled from here")
        emit(Event("build_failed", reason))
        emit(finished(state, reason, ok=False, kind="build_timeout",
                      dockerfile=dockerfile, build=build,
                      used_fallback=state["used_fallback"]))
        return {"step": DONE, "build": build}

    if not build.ok:
        emit(Event("build_failed", f"build exited {build.exit_code}"))
        if state["used_fallback"]:
            # This branch used to ask the model again, which is exactly the re-asking the
            # refusal policy rules out, and the gate would then have blamed us for a
            # Dockerfile the model wrote.
            emit(dead_end(state, "our fallback Dockerfile did not build", build=build))
            return {"step": DONE, "build": build}
        update = {"build": build, "previous": dockerfile, "dockerfile": None,
                  "evidence": bound(build.log, EVIDENCE_LIMIT)}
        return {**update, **next_attempt(state, emit, AUTHOR)}

    emit(Event("running", f"running {tag}"))
    result = sandbox.run(build.image, state["args"])
    if result.start_error:
        # The daemon says the process never started, so the Dockerfile is wrong. This is
        # deliberately not a test on 126 or 127: those can be produced by a script that
        # wants to look like a broken image, and a script that can produce anything has
        # already started.
        emit(Event("exec_failed",
                   f"the container never started: {result.start_error}"))
        if state["used_fallback"]:
            emit(dead_end(state, "our fallback image could not run its command",
                          build=build, run=result))
            return {"step": DONE, "build": build}
        update = {"build": build, "previous": dockerfile, "dockerfile": None,
                  "evidence": ("the container never started its command. docker said:\n"
                               f"{bound(result.start_error, EVIDENCE_LIMIT)}")}
        return {**update, **next_attempt(state, emit, AUTHOR)}

    # The script ran. What it means is the verdict's problem, but whether it succeeded is
    # observable here and the caller needs it: a run ending in a nonzero exit is a
    # finding, and reporting it as an unqualified success made the documented meaning of
    # exit 1 unreachable.
    emit(finished(state, f"the script ran and exited {result.exit_code}", ok=True,
                  kind="ran" if result.exit_code == 0 else "script_failed",
                  dockerfile=dockerfile, build=build, run=result,
                  used_fallback=state["used_fallback"]))
    return {"step": DONE, "build": build}


# --- the graph ------------------------------------------------------------------------

def build_graph(llm: LLM, sandbox: Sandbox, gate: Gate):
    """Three nodes, and one conditional edge out of each.

    The nodes close over the model, the sandbox and the gate, which is why those are not
    in the state. Every node routes through the same map, because every node returns one
    of the same four names: writing a per-node map would encode which transitions are
    possible in a second place, free to disagree with the nodes themselves.
    """
    graph = StateGraph(State)
    graph.add_node(AUTHOR, lambda state: author(state, llm))
    graph.add_node(LOOK, look_node)
    graph.add_node(EXECUTE, lambda state: execute(state, sandbox, gate))
    graph.set_entry_point(AUTHOR)
    routes = {AUTHOR: AUTHOR, LOOK: LOOK, EXECUTE: EXECUTE, DONE: END}
    for name in (AUTHOR, LOOK, EXECUTE):
        graph.add_conditional_edges(name, lambda state: state["step"], routes)
    return graph.compile()


def step_ceiling(max_attempts: int, max_refusals: int) -> int:
    """How many node visits a run can possibly make, plus room.

    LangGraph stops a graph at `recursion_limit` and raises, and its default of 25 is
    below what this machine legitimately uses: three attempts of four looks each is about
    thirty visits. That matters more than a tuning number because of how it fails. Every
    ending here is a `finished` event carrying an `Outcome`, and a graph stopped by its
    own limit emits none, so the run would end with no verdict rather than a bad one.

    Derived from the caps that actually bound a run rather than guessed. Per attempt: one
    author call per look, plus the look, plus the call that finally writes, plus the
    execute. Refusals are free calls and are counted once for the run. If this limit ever
    fires it is a bug in the derivation, because `max_attempts` is the bound that is
    supposed to stop a run.
    """
    return max_attempts * (2 * MAX_LOOKS + 3) + max_refusals + 10


def start_state(agent: "Agent", workspace: Workspace, language: str,
                args: Sequence[str]) -> "State | Event":
    """The state a run begins as, or the single event saying it will not begin.

    A language we cannot handle is refused before the model is consulted rather than
    half-attempted: there is no fallback Dockerfile for it, so a refusal used to raise
    `ValueError` out of the middle of a run.
    """
    run_id = uuid.uuid4().hex
    if language not in LANGUAGES:
        supported = ", ".join(sorted(LANGUAGES))
        return Event("finished", f"{language} is not supported", {"outcome": Outcome(
            ok=False, kind="unsupported",
            reason=f"this agent handles {supported}, not {language!r}", run_id=run_id)})
    if agent.max_attempts < 1:
        # Checked here because the nodes check the bound after an attempt has already
        # gone. Without this, a run configured to make no attempts would author, build,
        # run and report success.
        return Event("finished", "gave up after 0 attempts", {"outcome": Outcome(
            ok=False, kind="no_image", reason="no Dockerfile worked in 0 attempts",
            attempts=0, run_id=run_id)})

    script = workspace.script
    files = {name: workspace.read(name) for name in workspace.names()}
    full = files[script]
    return State(
        run_id=run_id, language=language, script=script, files=files, full=full,
        context=context_for(language, script, full, files), args=tuple(args),
        max_attempts=agent.max_attempts, max_refusals=agent.max_refusals,
        attempt=1, calls=0, input_tokens=0, output_tokens=0, looks=0, refusals=[],
        history=[], seen=0, pending=None,
        dockerfile=None, base_image="", previous=None, evidence=None,
        used_fallback=False, rebuilt_after_timeout=False, build=None,
        step=AUTHOR)


class Agent:
    """The agent. One script in, a stream of events out.

    It holds the three things a run needs and cannot serialise (the model, the sandbox,
    the gate), compiles the graph once, and streams what the nodes write.

    `gate` has no default on purpose. Every Dockerfile reaching the daemon was written by
    a model that had just read untrusted text, so an agent that can be built without a
    gate is an agent that can build one unchecked.
    """

    def __init__(self, llm: LLM, sandbox: Sandbox, gate: Gate, max_attempts: int = 3,
                 max_refusals: int = 1) -> None:
        self.llm, self.sandbox, self.gate = llm, sandbox, gate
        self.max_attempts, self.max_refusals = max_attempts, max_refusals
        # Compiled once. The graph holds no run state, so one compiled graph serves every
        # run this agent makes.
        self._graph = build_graph(llm, sandbox, gate)

    def run(self, workspace: Workspace, language: str,
            args: Sequence[str] = ()) -> Iterator[Event]:
        """Stream the events the nodes produce, as they produce them.

        `stream_mode="custom"` is what a node writes while it works, so an event arrives
        here the moment it is made. "updates" would deliver one batch per node and
        "values" the whole accumulated state every step: the first means a caller learns
        a build started only once it finished, and the second re-yields the entire
        history every step, which a consumer counting events reads as the run looping.
        """
        state = start_state(self, workspace, language, args)
        if isinstance(state, Event):    # a language we do not handle, refused at the door
            yield state
            return
        limit = step_ceiling(self.max_attempts, self.max_refusals)
        try:
            yield from self._graph.stream(state, {"recursion_limit": limit},
                                          stream_mode="custom")
        except GraphRecursionError as exc:
            # Derived to be unreachable, so reaching it is a bug in the derivation rather
            # than a run that deserved to stop. Translated here so nothing above has to
            # import a graph library to catch it, and because a run with no verdict must
            # never reach the shell as exit 1, which means "the script failed".
            raise EngineFailure(
                f"the graph exceeded {limit} steps, which its own bound says is "
                f"impossible, so the run produced no verdict: {exc}") from exc
