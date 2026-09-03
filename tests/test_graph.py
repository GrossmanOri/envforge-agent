"""The graph: state flowing between nodes, the look cap, and the routing.

Driven with a fake chat model rather than a fake of our own model layer, because the
thing under test is the graph and the graph talks to a LangChain model. The fake returns
real `AIMessage` objects with real `tool_calls`, so the routing, the `ToolNode` and the
`ToolMessage` pairing are all exercised as they will be in production.
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver

from envforge.sandbox import BuildResult, RunResult

from envforge.context import Context
from envforge.graph import MAX_LOOKS, State, build_graph, new_attempt, start_state
from envforge.tools import SLICE_HEADER

SCRIPT = "# head\n" + "# padding\n" * 400 + "\nimport tabulate\n" + "# tail\n" * 400
GOOD = 'FROM python:3.12-slim\nCOPY s.py /app/s.py\nCMD ["python", "/app/s.py"]\n'
# What the opening prompt holds of the script. It was zero while the harness passed a
# sentence; `first_prompt` now carries the bounded script, so it is the bound itself.
from envforge.agent import SCRIPT_LIMIT as SCRIPT_IN_PROMPT


class FakeModel:
    """A chat model that replays a queue of replies.

    Records what it was bound with on every call, because the look cap is enforced by
    withdrawing tools from the request, so what was offered is the only place that rule
    is observable.
    """

    def __init__(self, *replies):
        self.queue = list(replies)
        self.offered, self.seen_messages = [], []

    def bind_tools(self, tools, **kwargs):
        self.offered.append([t.name for t in tools])
        self.bound_with = kwargs
        return self

    def invoke(self, messages, **kwargs):
        self.seen_messages.append(list(messages))
        reply = self.queue.pop(0)
        if isinstance(reply, Exception):
            # A queued exception is raised rather than returned, so a provider failure
            # can be scripted the same way a reply is.
            raise reply
        return reply


def looks_at(name="search_script", **args):
    return AIMessage(content="", tool_calls=[
        {"name": name, "args": args or {"pattern": "import"}, "id": f"call{name}"}])


def submits(dockerfile=GOOD, base_image="python:3.12-slim"):
    return AIMessage(content="", tool_calls=[
        {"name": "submit_dockerfile",
         "args": {"dockerfile": dockerfile, "base_image": base_image},
         "id": "callsubmit"}])


class FakeSandbox:
    """Replays queued build and run results, and records the names it was given.

    The names matter: the container name is how a resumed run discovers that its attempt
    already executed the sample, so a fake that ignored it could not exercise the guard.
    """

    def __init__(self, builds=(), runs=()):
        self.builds, self.runs = list(builds), list(runs)
        self.built_tags, self.ran_as = [], []
        # What the engine asked to be removed. Cleanup belongs to the object that owns
        # the run, so a fake that could not record it would leave that untestable.
        self.removed: list[str] = []

    def build(self, dockerfile, files, tag, labels=None):
        self.built_tags.append(tag)
        return self.builds.pop(0) if self.builds else _build()

    def run(self, image, args=(), name=None, labels=None):
        self.ran_as.append(name)
        return self.runs.pop(0) if self.runs else _ran()

    def remove_image(self, tag):
        self.removed.append(tag)


def _build(ok=True, exit_code=0, log="", timed_out=False, seconds=0.1):
    return BuildResult(ok=ok, image="img", exit_code=exit_code, log=log,
                       truncated=False, timed_out=timed_out, seconds=seconds)


def _ran(exit_code=0, stdout="", stderr="", start_error="", timed_out=False):
    return RunResult(exit_code=exit_code, stdout=stdout, stderr=stderr, truncated=False,
                     timed_out=timed_out, seconds=0.1, start_error=start_error)


def _offline():
    """The host lookups, answered without a daemon.

    A graph test should not need docker, and these three are the only calls in the
    engine that reach for it.
    """
    return {"exists": lambda name: False, "remove": lambda name: None,
            "sweeper": lambda keep="", older_than=3600.0: [],
            "running": lambda name: False, "stop": lambda name: None}


def _workspace(text=None):
    """A gathered workspace, without touching disk twice."""
    from envforge.workspace import Files

    return Files(script="s.py", contents={"s.py": text or SCRIPT})


def ALLOW(dockerfile, base_image, files):
    return None


def DENY(dockerfile, base_image, files):
    return "FROM is not pinned" if "slim" not in dockerfile else None


def run(model, gate=ALLOW, events=None, script=SCRIPT, checkpointer=None, config=None,
        sandbox=None, exists=lambda name: False, max_attempts=3,
        remove=lambda name: None, running=lambda name: False,
        stop=lambda name: None):
    graph = build_graph(script, checkpointer=checkpointer)
    state = start_state("r1", "python", "s.py", script, {"s.py": script},
                        "you write Dockerfiles", "here is the script",
                        max_attempts=max_attempts)
    context = Context(model=model, gate=gate, sandbox=sandbox or FakeSandbox(),
                      exists=exists, remove_container=remove,
                      running=running, stop_container=stop,
                      emit=events.append if events is not None else (lambda e: None))
    return graph.invoke(state, context=context, config=config or {})


# --- state flows between nodes --------------------------------------------------------

def test_state_flows_between_nodes_and_nodes_return_only_what_changed():
    """Every counter in the final state was put there by a node returning a dict.

    Nothing here mutates a shared object. `calls` is 2 because two nodes each returned
    `calls + 1`, and `seen` is 1 because the counting node returned it.
    """
    model = FakeModel(looks_at(), submits())
    final = run(model)
    assert final["calls"] == 2
    assert final["seen"] == 1 and final["looks"] == 1
    assert final["candidate"] == GOOD and final["base_image"] == "python:3.12-slim"
    assert final["rejection"] is None
    # The run's inputs travelled untouched alongside the counters.
    assert final["run_id"] == "r1" and final["script"] == "s.py"


def test_a_tool_call_is_answered_and_the_answer_is_the_script_labelled():
    """The `ToolNode` ran the real tool, so the conversation carries a real result."""
    model = FakeModel(looks_at(name="read_script", start=0, end=40), submits())
    final = run(model)
    answers = [m for m in final["messages"] if isinstance(m, ToolMessage)]
    assert len(answers) == 2                       # the look, and the gate's own reply
    assert SLICE_HEADER in answers[0].content
    assert "characters 0 to 40" in answers[0].content


# --- the four-look cap ----------------------------------------------------------------

def test_the_inspection_tools_are_withdrawn_after_the_cap():
    """The cap is what the request contains, not what the prompt asks for."""
    model = FakeModel(*[looks_at() for _ in range(MAX_LOOKS)], submits())
    final = run(model)

    assert final["seen"] == MAX_LOOKS
    for offered in model.offered[:MAX_LOOKS]:
        assert set(offered) == {"read_script", "search_script", "submit_dockerfile"}
    # The call after the budget ran out could only submit.
    assert model.offered[MAX_LOOKS] == ["submit_dockerfile"]


def test_parallel_tool_calls_are_disabled_on_every_request():
    """Two calls in one reply would leave one unanswered, which the next request
    rejects, and would let a single reply return several slices past the cap."""
    model = FakeModel(submits())
    run(model)
    assert model.bound_with["parallel_tool_calls"] is False


# --- submission routes to the gate, and the model cannot reach it ----------------------

def test_submitting_goes_to_the_gate_and_the_gate_is_never_a_tool_call():
    model = FakeModel(submits())
    events = []
    final = run(model, events=events)
    assert final["candidate"] == GOOD
    # The submission was answered by the gate node, not executed by the ToolNode.
    answers = [m for m in final["messages"] if isinstance(m, ToolMessage)]
    assert len(answers) == 1 and "passed the gate" in answers[0].content
    assert [e.kind for e in events] == ["asking", "wrote", "building", "running",
                                        "finished"]


def test_a_rejected_dockerfile_goes_back_to_the_model_with_the_reason():
    model = FakeModel(submits("FROM python\n"), submits(GOOD))
    events = []
    final = run(model, gate=DENY, events=events)
    assert final["rejection"] is None and final["candidate"] == GOOD
    assert [e.kind for e in events] == ["asking", "gate_rejected", "asking", "wrote",
                                        "building", "running", "finished"]
    # The retry cleared the conversation, so the reason came back as a new message.
    # Without it the model retries knowing only that something was wrong.
    told = [m for m in final["messages"]
            if isinstance(m, HumanMessage) and "did not work" in m.content]
    assert told and "FROM is not pinned" in told[0].content
    assert "FROM python\n" in told[0].content        # and what it had submitted


def test_the_submission_tool_is_never_executed():
    """Its body raises. Reaching it would mean a tool the model called was doing the
    work that a deterministic node is supposed to do."""
    from envforge.tools import submit_dockerfile

    with pytest.raises(AssertionError, match="never executed"):
        submit_dockerfile.invoke({"dockerfile": "FROM x", "base_image": "x"})


# --- the message reset between attempts -----------------------------------------------

def test_a_new_attempt_clears_the_conversation_but_keeps_our_own_messages():
    """The look cap bounds one prompt, and `add_messages` accumulates, so without this
    three attempts of four looks would put twelve slices in one context."""
    model = FakeModel(*[looks_at() for _ in range(MAX_LOOKS)], submits())
    final = run(model)
    assert len([m for m in final["messages"] if isinstance(m, ToolMessage)]) == 5

    after = new_attempt(final)
    kept = [m for m in final["messages"] if m.id not in
            {r.id for r in after["messages"]}]
    assert [type(m).__name__ for m in kept] == ["SystemMessage", "HumanMessage"]
    assert after["attempt"] == final["attempt"] + 1
    assert after["seen"] == 0


def test_the_reset_actually_shortens_the_next_prompt():
    """Asserted by applying the removals the way LangGraph would, rather than by reading
    the list of RemoveMessage objects and trusting them."""
    from langgraph.graph.message import add_messages

    model = FakeModel(*[looks_at() for _ in range(MAX_LOOKS)], submits())
    final = run(model)
    before = len(final["messages"])
    remaining = add_messages(final["messages"], new_attempt(final)["messages"])
    assert len(remaining) == 2 < before
    assert not [m for m in remaining if isinstance(m, ToolMessage)]


# --- checkpoint and resume ------------------------------------------------------------

def test_a_run_can_be_paused_and_resumed_from_an_in_memory_checkpoint():
    """Same process only. `InMemorySaver` proves the state is checkpointable and that a
    resumed run carries its counters, not that anything survives a restart.
    """
    saver = InMemorySaver()
    config = {"configurable": {"thread_id": "t1"}}
    script = SCRIPT

    # Stop after two looks by giving the model nothing more to say.
    first = FakeModel(looks_at(), looks_at())
    graph = build_graph(script, checkpointer=saver)
    state = start_state("r1", "python", "s.py", script, {"s.py": script}, "sys", "first")
    with pytest.raises(IndexError):           # the queue runs out mid-run
        graph.invoke(state, context=Context(model=first, gate=ALLOW,
                                           sandbox=FakeSandbox()),
                     config=config)

    saved = graph.get_state(config)
    assert saved.values["seen"] == 2 and saved.values["looks"] == 2

    # A different model object, the same thread: the run picks up where it stopped.
    second = FakeModel(submits())
    final = graph.invoke(None, context=Context(model=second, gate=ALLOW,
                                              sandbox=FakeSandbox()),
                         config=config)
    assert final["seen"] == 2                 # carried across the pause
    assert final["candidate"] == GOOD
    # And the cap still counts the looks taken before the pause.
    assert second.offered[0] == ["read_script", "search_script", "submit_dockerfile"]


def test_a_huge_search_pattern_is_bounded_before_it_reaches_the_conversation():
    """`read_region` clamps and `search_text` bounds its windows, so the bound in
    `labelled` looks redundant. It is not: `search_text` echoes the pattern back, and the
    pattern is chosen by the model and unbounded. This is the only thing between a
    300,000 character argument and the next request."""
    from envforge.tools import SLICE_LIMIT, inspection_tools

    search = [t for t in inspection_tools(SCRIPT) if t.name == "search_script"][0]
    result = search.invoke({"pattern": "z" * 300_000})
    assert len(result) < len(SLICE_HEADER) + SLICE_LIMIT + 200


def test_a_literal_search_never_becomes_a_regular_expression():
    """The pattern comes from a model that has just read attacker-controlled text, and
    `re` on a model-chosen pattern is catastrophic backtracking on the host doing the
    analysis, which is the one machine here that is not in a sandbox."""
    from envforge.tools import search_text

    assert "does not occur" in search_text("a" * 4000, "(a+)+$")
    assert "does not occur" in search_text("a" * 200 + "\nprint(1)\n", ".*")
    assert "occurs 1 time(s)" in search_text("print(1)\nx.y\n", "x.y")
    assert "does not occur" in search_text("print(1)\nxzy\n", "x.y")


# --- build, run, and what spends an attempt -------------------------------------------

def outcome_of(events):
    return [e for e in events if e.kind == "finished"][-1].data["outcome"]


def test_a_script_that_runs_ends_the_run_with_what_it_did():
    events = []
    sandbox = FakeSandbox(runs=[_ran(exit_code=0, stdout="hello")])
    final = run(FakeModel(submits()), events=events, sandbox=sandbox)
    result = outcome_of(events)
    assert result.ok and result.kind == "ran" and result.attempts == 1
    assert result.run.stdout == "hello"
    assert sandbox.built_tags == ["envforge-r1:attempt1"]


def test_a_script_that_exits_nonzero_is_a_finding_not_a_malfunction():
    """The tool did its job and the news is bad. Reporting it as an unqualified success
    made the documented meaning of exit 1 unreachable."""
    events = []
    run(FakeModel(submits()), events=events,
        sandbox=FakeSandbox(runs=[_ran(exit_code=3, stderr="boom")]))
    result = outcome_of(events)
    assert result.ok and result.kind == "script_failed" and result.run.exit_code == 3


def test_a_failed_build_spends_an_attempt_and_the_log_becomes_the_evidence():
    events = []
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="no such package"),
                                  _build()])
    final = run(FakeModel(submits(), submits()), events=events, sandbox=sandbox)
    assert [e.kind for e in events] == ["asking", "wrote", "building", "build_failed",
                                        "asking", "wrote", "building", "running",
                                        "finished"]
    assert outcome_of(events).attempts == 2
    told = [m for m in final["messages"] if isinstance(m, HumanMessage)
            and "did not work" in m.content]
    assert told and "no such package" in told[0].content


def test_a_build_timeout_buys_one_free_rebuild_and_no_model_call():
    """A timeout is not a Dockerfile defect, so the model must not be asked again. But
    buildkit keeps what it pulled, so the same file is worth building once more."""
    events = []
    sandbox = FakeSandbox(builds=[_build(ok=False, timed_out=True, seconds=300.0),
                                  _build()])
    run(FakeModel(submits()), events=events, sandbox=sandbox)
    assert [e.kind for e in events] == ["asking", "wrote", "building", "build_failed",
                                        "building", "running", "finished"]
    assert len(sandbox.built_tags) == 2
    result = outcome_of(events)
    assert result.ok and result.usage.calls == 1        # one model call, two builds
    assert result.attempts == 2                          # the rebuild spent an attempt


def test_a_second_timeout_ends_the_run_rather_than_rebuilding_forever():
    events = []
    sandbox = FakeSandbox(builds=[_build(ok=False, timed_out=True, seconds=300.0)] * 2)
    run(FakeModel(submits()), events=events, sandbox=sandbox)
    result = outcome_of(events)
    assert result.kind == "build_timeout" and not result.ok
    assert len(sandbox.built_tags) == 2


def test_a_container_that_never_started_is_repairable():
    """The daemon says the process never started, so the Dockerfile is wrong. This is
    deliberately not a test on exit 126 or 127, which a script can produce on purpose."""
    events = []
    sandbox = FakeSandbox(runs=[_ran(start_error="exec format error"), _ran()])
    run(FakeModel(submits(), submits()), events=events, sandbox=sandbox)
    assert "exec_failed" in [e.kind for e in events]
    assert outcome_of(events).attempts == 2


def test_the_attempt_cap_ends_the_run_rather_than_the_recursion_limit():
    events = []
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 3)
    run(FakeModel(*[submits()] * 3), events=events, sandbox=sandbox, max_attempts=3)
    result = outcome_of(events)
    assert result.kind == "no_image" and result.attempts == 3
    assert len(sandbox.built_tags) == 3


def test_a_gate_that_always_rejects_stops_at_the_attempt_cap():
    """A rejection is a failed attempt, so it goes through the same cap. Routing it
    straight back to the model would loop until the graph's own recursion limit, which
    produces no verdict at all."""
    events = []
    run(FakeModel(*[submits("FROM python\n")] * 3), gate=DENY, events=events,
        max_attempts=3)
    result = outcome_of(events)
    assert result.kind == "no_image" and result.attempts == 3
    assert [e.kind for e in events].count("gate_rejected") == 3


# --- replay safety --------------------------------------------------------------------

def test_the_container_is_named_from_the_run_and_the_attempt():
    """Deterministic, because the name is the only evidence a resumed run has that the
    sample already executed."""
    sandbox = FakeSandbox()
    run(FakeModel(submits()), sandbox=sandbox)
    assert sandbox.ran_as == ["envforge-r1-attempt1"]


def test_a_replayed_run_refuses_to_execute_the_sample_twice():
    """The one side effect that must happen at most once.

    A checkpoint is written after a node returns, so a crash between the container
    exiting and the checkpoint committing makes LangGraph replay the run node. The state
    cannot help, because the state is exactly what was not saved. The container is the
    evidence: `run` kills it and leaves it, and removal waits until the result is
    durable, so one still bearing this attempt's name was left by a process that died.
    """
    events = []
    sandbox = FakeSandbox()
    run(FakeModel(submits()), events=events, sandbox=sandbox,
        exists=lambda name: name == "envforge-r1-attempt1")

    assert sandbox.ran_as == []                        # nothing was executed
    result = outcome_of(events)
    assert not result.ok and result.kind == "failed"
    assert "second time" in result.reason
    # And it did not invent a verdict about a script it never watched.
    assert result.run is None


def test_a_build_is_replayed_without_complaint():
    """The easy half. The tag is derived from the run and the attempt, so a rebuild after
    a crash produces the same tag and buildkit serves what it already has."""
    sandbox = FakeSandbox()
    run(FakeModel(submits()), sandbox=sandbox)
    first = list(sandbox.built_tags)
    sandbox_again = FakeSandbox()
    run(FakeModel(submits()), sandbox=sandbox_again)
    assert first == sandbox_again.built_tags == ["envforge-r1:attempt1"]


def test_no_single_prompt_holds_too_much_of_the_script():
    """The bound the look cap exists for, measured on the prompts actually sent.

    A model that hides what it read in Dockerfile comments launders those slices into the
    next attempt, on top of that attempt's own fresh look budget. That broke this bound
    once before, because the previous Dockerfile was replayed whole into the repair. Both
    channels are bounded now, and this counts unique tokens in what the model was handed
    rather than trusting either bound.
    """
    from envforge.agent import DOCKERFILE_LIMIT, EVIDENCE_LIMIT
    from envforge.tools import SLICE_LIMIT

    tokens = [f"tok{i:06d}" for i in range(4000)]
    script = "\n".join(tokens)

    class Laundering(FakeModel):
        """Reads the script, then writes everything it read into comment lines, which
        the gate permits, and carries them forward every attempt."""

        def __init__(self):
            super().__init__()
            self.carried, self.reads = [], 0

        def invoke(self, messages):
            self.seen_messages.append(list(messages))
            looked = sum(1 for m in messages if isinstance(m, ToolMessage))
            if self.offered[-1] != ["submit_dockerfile"] and looked < MAX_LOOKS:
                start = self.reads * SLICE_LIMIT
                self.reads += 1
                return looks_at(name="read_script", start=start,
                                end=start + SLICE_LIMIT)
            self.carried += [m.content.replace("\n", " ") for m in messages
                             if isinstance(m, ToolMessage)]
            comments = "\n".join(f"# {c}" for c in self.carried)
            return submits(f"FROM python:3.12-slim\n{comments}\n"
                           f'COPY s.py /app/s.py\nCMD ["python", "/app/s.py"]\n')

    model = Laundering()
    run(model, script=script, max_attempts=3,
        sandbox=FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 9))

    worst = 0
    for messages in model.seen_messages:
        whole = "".join(str(m.content) for m in messages)
        worst = max(worst, sum(len(t) for t in tokens if t in whole))
    ceiling = (SCRIPT_IN_PROMPT + MAX_LOOKS * SLICE_LIMIT + DOCKERFILE_LIMIT
               + EVIDENCE_LIMIT)
    assert worst <= ceiling, f"{worst} characters of the sample in one prompt"
    assert worst < len(script) / 2          # and nowhere near the whole file


def test_the_container_is_removed_only_after_its_result_is_recorded():
    """The fail-closed ordering, asserted on the order things happened.

    A checkpoint commits when a node returns, so removing the container inside the node
    that ran it leaves a window where a crash loses both the state and the evidence. The
    removal belongs to the next node, by which time the result is durable.
    """
    order = []

    class Watching(FakeSandbox):
        def run(self, image, args=(), name=None, labels=None):
            order.append(("ran", name))
            return super().run(image, args, name=name, labels=labels)

    sandbox = Watching()
    run(FakeModel(submits()), sandbox=sandbox,
        remove=lambda name: order.append(("removed", name)))
    assert order == [("ran", "envforge-r1-attempt1"),
                     ("removed", "envforge-r1-attempt1")]


def test_an_interrupted_attempt_keeps_its_container_as_evidence():
    """The opposite of what this asserted, and the change is the point.

    It said refusing must not leave the evidence lying around, and cleaned it up. That
    is the cleanup rule winning an argument it should lose: the checkpoint for the
    refusal can itself be lost in a crash, and the next resume would then find nothing
    and execute the sample a second time. The container stays.
    """
    removed = []
    run(FakeModel(submits()), sandbox=FakeSandbox(), remove=removed.append,
        exists=lambda name: name == "envforge-r1-attempt1")
    assert removed == [], "the evidence that the sample already ran was deleted"


def test_a_container_found_still_running_is_stopped_but_not_removed():
    """A process that died leaves its container behind and the daemon does not stop it,
    so the sample may still be executing while we report the attempt as interrupted.
    Stopping ends that. Removing would destroy the proof it happened."""
    stopped, removed = [], []
    events = []
    run(FakeModel(submits()), sandbox=FakeSandbox(), events=events,
        remove=removed.append, exists=lambda name: True,
        running=lambda name: True, stop=stopped.append)
    assert stopped == ["envforge-r1-attempt1"]
    assert removed == []
    assert "second time" in outcome_of(events).reason


def test_a_container_already_stopped_is_not_stopped_again():
    stopped = []
    run(FakeModel(submits()), sandbox=FakeSandbox(), exists=lambda name: True,
        running=lambda name: False, stop=stopped.append)
    assert stopped == []


# --- refusals and the fallback --------------------------------------------------------

def declines(reason="looks like a credential stealer", provider="anthropic"):
    """A refusal, in the shape each provider actually reports one.

    A successful reply, not an exception. That is the whole distinction: a refusal is the
    model judging the sample, and an exception is our infrastructure failing.
    """
    if provider == "anthropic":
        return AIMessage(content="", response_metadata={
            "stop_reason": "refusal",
            "stop_details": {"type": "refusal", "explanation": reason}})
    return AIMessage(content="", additional_kwargs={"refusal": reason})


@pytest.mark.parametrize("provider", ["anthropic", "openai"], ids=["anthropic", "openai"])
def test_a_refusal_is_recognised_in_either_provider_shape(provider):
    from envforge.graph import refusal_reason

    assert refusal_reason(declines("no", provider=provider)) == "no"
    assert refusal_reason(submits()) is None
    assert refusal_reason(looks_at()) is None


def test_one_refusal_asks_again_and_never_spends_a_repair_attempt():
    """A refusal has its own counter. Asking a model that has just declined to try again
    is not a repair, and it must not consume one of the three attempts."""
    events = []
    final = run(FakeModel(declines(), submits()), events=events)
    assert [e.kind for e in events] == ["asking", "refused", "asking", "wrote",
                                        "building", "running", "finished"]
    result = outcome_of(events)
    assert result.attempts == 1 and result.ok
    assert not result.used_fallback and len(result.refusals) == 1


def test_refusing_twice_falls_back_to_a_Dockerfile_we_wrote():
    """What makes a refusal survivable without asking again. Ours goes through the same
    gate as anything the model wrote: one path to the daemon."""
    events = []
    final = run(FakeModel(declines(), declines()), events=events)
    assert [e.kind for e in events] == ["asking", "refused", "asking", "refused",
                                        "fell_back", "building", "running", "finished"]
    result = outcome_of(events)
    assert result.used_fallback and result.ok
    assert len(result.refusals) == 2
    # The fallback we wrote, not something the model produced.
    assert "COPY s.py /app/s.py" in final["candidate"]


def test_the_fallback_is_checked_by_the_gate_like_everything_else():
    gate_saw = []

    def watching(dockerfile, base_image, files):
        gate_saw.append(dockerfile)
        return None

    run(FakeModel(declines(), declines()), gate=watching)
    assert len(gate_saw) == 1 and "COPY s.py" in gate_saw[0]


@pytest.mark.parametrize("failure, sandbox_args, expected", [
    ("the gate refuses it", {}, "was rejected"),
    ("it does not build", {"builds": [_build(ok=False, exit_code=1, log="boom")]},
     "did not build"),
    ("it cannot start", {"runs": [_ran(start_error="exec format error")]},
     "could not run its command"),
], ids=["gate", "build", "exec"])
def test_a_fallback_that_fails_stops_instead_of_asking_again(failure, sandbox_args,
                                                             expected):
    """Three ways our own Dockerfile can fail, and in all three the honest move is to
    stop and say which. Retrying would be exactly the re-asking the refusal policy rules
    out, and the gate would then blame us for a Dockerfile the model wrote."""
    events = []
    # A gate that refuses everything, including ours. `DENY` allows anything
    # containing "slim", and the fallback is built on python:3.12-slim, so it
    # would have passed and the test would have proved nothing.
    gate = ((lambda d, b, f: "no Dockerfile is acceptable")
            if failure == "the gate refuses it" else ALLOW)
    model = FakeModel(declines(), declines())
    run(model, gate=gate, events=events, sandbox=FakeSandbox(**sandbox_args))

    result = outcome_of(events)
    assert not result.ok and result.kind == "failed"
    assert expected in result.reason
    assert result.used_fallback
    # One model call per refusal, and not one more.
    assert result.usage.calls == 2


def test_a_reply_that_calls_no_tool_spends_an_attempt_rather_than_ending_the_run():
    """Prose instead of a tool call. Repairable by rewriting the reply rather than the
    image, so it costs an attempt and the model is told what shape was expected.

    Ending the run here instead would report no verdict at all, which is the one thing a
    tool whose only product is a verdict must not do quietly.
    """
    events = []
    final = run(FakeModel(AIMessage(content="I think you should use Ubuntu."),
                          submits()), events=events)
    assert "unusable_reply" in [e.kind for e in events]
    result = outcome_of(events)
    assert result.ok and result.attempts == 2
    told = [m for m in final["messages"]
            if isinstance(m, HumanMessage) and "did not work" in m.content]
    assert told and "called no tool" in told[0].content


def test_a_model_that_never_calls_a_tool_stops_at_the_attempt_cap():
    """And it does not loop forever doing it."""
    events = []
    run(FakeModel(*[AIMessage(content="no thanks") for _ in range(3)]), events=events,
        max_attempts=3)
    result = outcome_of(events)
    assert result.kind == "no_image" and result.attempts == 3


# --- what a run leaves on the machine -------------------------------------------------

def test_a_run_removes_the_images_it_built_whatever_happened():
    """Cleanup belongs to whoever owns the run, in a `finally`.

    It used to live in the command line, which was fine while the command line was the
    only caller and meant this engine leaked an image per attempt for every other run.
    """
    from envforge.graph import Agent

    class Recording(FakeSandbox):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.removed = []

        def remove_image(self, tag):
            self.removed.append(tag)

    workspace = _workspace()
    sandbox = Recording(builds=[_build(ok=False, exit_code=1, log="boom"), _build()])
    agent = Agent(FakeModel(submits(), submits()), sandbox, ALLOW, **_offline())
    list(agent.run(workspace, "python"))
    assert sandbox.removed == sandbox.built_tags
    assert len(sandbox.removed) == 2                # one per attempt, both gone


def test_an_exception_mid_run_still_removes_the_images():
    from envforge.graph import Agent

    class Exploding(FakeSandbox):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.removed = []

        def run(self, image, args=(), name=None, labels=None):
            raise OSError("the daemon went away")

        def remove_image(self, tag):
            self.removed.append(tag)

    sandbox = Exploding()
    agent = Agent(FakeModel(submits()), sandbox, ALLOW, **_offline())
    with pytest.raises(OSError):
        list(agent.run(_workspace(), "python"))
    assert sandbox.removed == sandbox.built_tags != []


def test_a_run_removes_only_its_own_images():
    """`built_tags` may hold another run's work if a sandbox is shared, and a run that
    deleted an image another run was about to execute would be our bug in their run."""
    from envforge.graph import Agent

    class Recording(FakeSandbox):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.removed = []

        def remove_image(self, tag):
            self.removed.append(tag)

    sandbox = Recording()
    sandbox.built_tags.append("envforge-someoneelse:attempt1")
    list(Agent(FakeModel(submits()), sandbox, ALLOW,
               **_offline()).run(_workspace(), "python"))
    assert "envforge-someoneelse:attempt1" not in sandbox.removed
    assert len(sandbox.removed) == 1


def test_every_image_and_container_carries_the_run_label():
    """A label rather than a name prefix, so a sweep can say "this is ours" about
    somebody else's machine without matching a container they named themselves."""
    from envforge.graph import Agent
    from envforge.sandbox import RUN_LABEL, STARTED_LABEL

    class Watching(FakeSandbox):
        def __init__(self, **kw):
            super().__init__(**kw)
            self.labels = []

        def build(self, dockerfile, files, tag, labels=None):
            self.labels.append(("image", labels))
            return super().build(dockerfile, files, tag)

        def run(self, image, args=(), name=None, labels=None):
            self.labels.append(("container", labels))
            return super().run(image, args, name=name)

    sandbox = Watching()
    list(Agent(FakeModel(submits()), sandbox, ALLOW,
               **_offline()).run(_workspace(), "python"))
    assert [kind for kind, _ in sandbox.labels] == ["image", "container"]
    runs = {labels[RUN_LABEL] for _, labels in sandbox.labels}
    assert len(runs) == 1 and len(runs.pop()) == 32          # one run id, a uuid4 hex
    for _, labels in sandbox.labels:
        assert int(labels[STARTED_LABEL]) > 0


# --- the engine actually produces events ----------------------------------------------

def test_the_engine_yields_the_events_it_produces():
    """The whole point of the object, and it did none of it.

    `Agent.run` collected events into a list nothing read and streamed with
    `stream_mode="custom"`, which yields only what a node writes through LangGraph's
    writer. No node did, so the only engine in the project produced nothing at all: no
    verdict, no `finished`, nothing. Four tests drove that generator and every one of
    them asserted on the sandbox instead of on what came out.
    """
    from envforge.graph import Agent

    events = list(Agent(FakeModel(submits()), FakeSandbox(), ALLOW,
                        **_offline()).run(_workspace(), "python"))
    assert [e.kind for e in events] == ["asking", "wrote", "building", "running",
                                        "finished"]
    outcome = events[-1].data["outcome"]
    assert outcome.ok and outcome.kind == "ran"


def test_the_engine_yields_a_look_and_a_repair_too():
    from envforge.graph import Agent

    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom"), _build()])
    events = list(Agent(FakeModel(looks_at(), submits(), submits()), sandbox, ALLOW,
                        **_offline()).run(_workspace(), "python"))
    kinds = [e.kind for e in events]
    assert kinds.count("looked") == 1 and kinds.count("building") == 2
    assert kinds[-1] == "finished" and events[-1].data["outcome"].attempts == 2


def test_the_engine_says_what_it_swept():
    from envforge.graph import Agent

    offline = {**_offline(),
               "sweeper": lambda keep="", older_than=3600.0: ["image abc from run def"]}
    events = list(Agent(FakeModel(submits()), FakeSandbox(), ALLOW,
                        **offline).run(_workspace(), "python"))
    assert events[0].kind == "swept" and "abc" in events[0].message


# --- the message reset, measured rather than described ---------------------------------

def test_repair_messages_do_not_accumulate_across_attempts():
    """Each attempt carries one repair message, not one per attempt so far.

    `new_attempt` kept "the leading run of system and human messages", and the repair
    message it appends is itself a HumanMessage, so on the next attempt it had joined
    that run and was never removed again. The bound invariant 24 states became linear in
    `max_attempts` instead of constant: at twelve attempts a review measured 24,876
    characters of the sample in one prompt against a ceiling of 22,528.
    """
    from envforge.graph import Agent

    model = FakeModel(*[submits(f"FROM python:3.12-slim\nRUN nope{i}\n")
                        for i in range(6)])
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 6)
    list(Agent(model, sandbox, ALLOW, max_attempts=6,
               **_offline()).run(_workspace(), "python"))

    for sent in model.seen_messages:
        repairs = [m for m in sent
                   if isinstance(m, HumanMessage) and "did not work" in m.content]
        assert len(repairs) <= 1, f"{len(repairs)} repair messages in one prompt"
    # And the opening pair is never removed, or the model loses the script itself.
    for sent in model.seen_messages:
        assert isinstance(sent[0], SystemMessage) and isinstance(sent[1], HumanMessage)


def test_the_ceiling_holds_when_every_attempt_reads_somewhere_new():
    """The laundering attack with distinct slices per attempt.

    The version of this that shipped recycled the same slices every attempt, so the
    unique-token count could not grow and it passed against a design where it should
    not have.
    """
    from envforge.agent import DOCKERFILE_LIMIT, EVIDENCE_LIMIT, SCRIPT_LIMIT
    from envforge.graph import Agent
    from envforge.tools import SLICE_LIMIT

    tokens = [f"tok{i:06d}" for i in range(6000)]
    script = "\n".join(tokens)

    class Fresh(FakeModel):
        """Reads a region nobody has read yet, then hides it in comments."""

        def __init__(self):
            super().__init__()
            self.carried, self.reads = [], 0

        def invoke(self, messages):
            self.seen_messages.append(list(messages))
            looked = sum(1 for m in messages if isinstance(m, ToolMessage))
            if self.offered[-1] != ["submit_dockerfile"] and looked < MAX_LOOKS:
                start = self.reads * SLICE_LIMIT
                self.reads += 1
                return looks_at(name="read_script", start=start,
                                end=start + SLICE_LIMIT)
            self.carried += [m.content.replace("\n", " ") for m in messages
                             if isinstance(m, ToolMessage)]
            comments = "\n".join(f"# {c}" for c in self.carried)
            return submits(f"FROM python:3.12-slim\n{comments}\n"
                           f'COPY s.py /app/s.py\nCMD ["python", "/app/s.py"]\n')

    model = Fresh()
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 12)
    list(Agent(model, sandbox, ALLOW, max_attempts=12,
               **_offline()).run(_workspace(script), "python"))

    ceiling = SCRIPT_LIMIT + MAX_LOOKS * SLICE_LIMIT + DOCKERFILE_LIMIT + EVIDENCE_LIMIT
    worst = 0
    for sent in model.seen_messages:
        whole = "".join(str(m.content) for m in sent)
        worst = max(worst, sum(len(t) for t in tokens if t in whole))
    assert worst <= ceiling, f"{worst} characters of the sample in one prompt"


# --- the cap is the graph's rule, not the provider's -----------------------------------

def test_a_model_that_ignores_the_withdrawn_tool_is_not_served():
    """Withdrawing a tool from the request should make it uncallable, and that is a rule
    the provider enforces rather than us. A fake that names an inspection tool anyway was
    served it, took 3,332 looks past the cap and reassembled the whole script."""
    class Stubborn(FakeModel):
        def invoke(self, messages):
            self.seen_messages.append(list(messages))
            return looks_at(name="read_script", start=0, end=100)

    events = []
    final = run(Stubborn(), events=events, max_attempts=1)
    assert final["seen"] == MAX_LOOKS               # not one more, whatever it asked for
    assert [e.kind for e in events].count("looked") == MAX_LOOKS
    assert outcome_of(events).kind == "no_image"    # it ran out of attempts, not looks


def test_the_tool_node_holds_only_the_read_only_tools():
    """The wiring invariant 23 rests on. Adding the submission to this node would put the
    daemon one model call away, and nothing pinned it."""
    from envforge.graph import build_graph

    compiled = build_graph("print(1)")
    node = compiled.nodes["inspect"]
    names = {t.name for t in node.bound.tools_by_name.values()}
    assert names == {"read_script", "search_script"}
    assert "submit_dockerfile" not in names


# --- provider failures ------------------------------------------------------------------

class Dead(Exception):
    """An SDK exception, in the shape LangChain passes through."""

    def __init__(self, message, status_code, type_=""):
        super().__init__(message)
        self.status_code, self.type = status_code, type_


@pytest.mark.parametrize("status, reported, kind", [
    (401, "", "auth"),
    (402, "", "billing"),
    (403, "billing_error", "billing"),
    (429, "", "rate_limit"),
    (422, "", "rejected"),
], ids=["dead key", "no credit", "403 billing", "rate limit", "our bad request"])
def test_a_provider_failure_ends_the_run_and_says_which(status, reported, kind):
    """It escaped the graph entirely: a dead key came out as a raw SDK exception with no
    `finished` event, so a caller got no verdict and no sign one was missing.

    Never the fallback. A refusal is the model judging the sample; this is our
    infrastructure failing, and a verdict no judgment went into must not look like one.
    """
    class Failing(FakeModel):
        def invoke(self, messages):
            raise Dead("no", status, reported)

    events = []
    run(Failing(), events=events)
    result = outcome_of(events)
    assert not result.ok and not result.used_fallback
    assert result.kind == ("rejected" if kind == "rejected" else "unavailable")
    assert kind in [e.data.get("kind") for e in events if e.kind == "provider_unavailable"]


def test_a_bug_of_ours_is_not_dressed_up_as_a_provider_failure():
    """Anything without a status is not the provider failing, and turning it into a tidy
    verdict would hide our own crash behind a report about the sample."""
    class Broken(FakeModel):
        def invoke(self, messages):
            raise ValueError("a bug in our own code")

    with pytest.raises(ValueError, match="a bug in our own code"):
        run(Broken())


# --- tool binding, and the submission nobody executes -----------------------------------

def test_tools_are_bound_in_one_place_with_parallel_calls_off():
    """One binding site, so there is one place a tool can be added or a flag forgotten."""
    model = FakeModel(submits())
    run(model)
    assert model.bound_with["parallel_tool_calls"] is False
    assert set(model.offered[0]) == {"read_script", "search_script", "submit_dockerfile"}


@pytest.mark.parametrize("args, expected", [
    ({"base_image": "python:3.12-slim"}, "left out dockerfile"),
    ({"dockerfile": "FROM x"}, "left out base_image"),
    ({"dockerfile": 7, "base_image": "x"}, "must be a string"),
    ({"dockerfile": "", "base_image": "x"}, "empty dockerfile"),
    ({"dockerfile": "FROM x", "base_image": "   "}, "empty base_image"),
], ids=["no dockerfile", "no base_image", "not a string", "empty", "whitespace"])
def test_a_malformed_submission_is_refused_before_the_gate_sees_it(args, expected):
    """The one tool nobody executes is the one nobody validates.

    Every other tool's arguments are checked by its own schema when `ToolNode` runs it,
    and this one is never run: the graph routes on it. Anthropic and OpenAI
    grammar-constrain the arguments anyway, but Groq documents its schema guarantee as
    not covering tool use, so for Groq this is the only check there is. Without it a
    malformed submission reached the gate as an empty string and was refused for the
    wrong reason.
    """
    gated = []

    def watching(dockerfile, base_image, files):
        gated.append(dockerfile)
        return None

    broken = AIMessage(content="", tool_calls=[
        {"name": "submit_dockerfile", "args": args, "id": "callsubmit"}])
    events = []
    run(FakeModel(broken, submits()), gate=watching, events=events)

    assert "unusable_reply" in [e.kind for e in events]
    assert expected in [e.message for e in events if e.kind == "unusable_reply"][0]
    # The gate saw only the good one, so it never had to guess what an empty string meant.
    assert gated == [GOOD]


def test_a_well_formed_submission_reaches_the_gate_untouched():
    """The check is about shape, not about whether the Dockerfile is allowed. Deciding
    that is the gate's job and it must still get its turn."""
    gated = []
    run(FakeModel(submits("FROM python:3.12-slim\nCMD [\"x\"]\n")),
        gate=lambda d, b, f: gated.append(d) or None)
    assert gated == ["FROM python:3.12-slim\nCMD [\"x\"]\n"]


def test_a_look_says_which_tool_was_called_and_with_what():
    """The event's words, not just its kind.

    Every test here counted `looked` events and none read one, so a real run printed
    "the model called None with None" for months of fake-driven green. The node read
    `messages[-1]`, which by then is the `ToolMessage` the tool node appended rather
    than the `AIMessage` that asked for it.
    """
    events = []
    run(FakeModel(looks_at(name="read_script", start=10, end=99), submits()),
        events=events)
    looked = [e for e in events if e.kind == "looked"][0]
    assert "read_script" in looked.message
    assert "'start': 10" in looked.message and "'end': 99" in looked.message
    assert "None" not in looked.message
    assert looked.data["tool"] == "read_script"


def test_a_search_look_names_the_pattern_it_searched_for():
    events = []
    run(FakeModel(looks_at(name="search_script", pattern="tabulate"), submits()),
        events=events)
    looked = [e for e in events if e.kind == "looked"][0]
    assert "search_script" in looked.message and "tabulate" in looked.message


def test_the_refusal_shape_is_the_one_a_live_model_actually_returns():
    """Copied from a real reply, not invented.

    Asked Claude on 2026-09-03 for a Dockerfile that downloads and runs a credential
    stealer. It declined with `stop_reason` "refusal" and a `stop_details` carrying a
    category and an explanation, and `content` set to prose rather than empty. That last
    part matters: a refusal is a successful reply with no tool call, so anything reading
    "no tool call" as an error would report the model's judgment of the sample as our own
    malfunction.
    """
    from envforge.graph import refusal_reason

    real = AIMessage(
        content="I can't help with",
        response_metadata={
            "stop_reason": "refusal",
            "stop_details": {"category": "cyber", "type": "refusal",
                             "explanation": "This request triggered cyber-related "
                                            "safeguards."}},
        tool_calls=[])
    said = refusal_reason(real)
    assert said is not None and "cyber-related safeguards" in said
    # And it is not mistaken for a reply we could not use, which spends an attempt.
    assert refusal_reason(AIMessage(content="just chatting")) is None
