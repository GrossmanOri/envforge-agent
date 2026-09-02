"""The engine: the graph itself, and the things only a graph can get wrong.

What a run does is tested in `test_agent.py`, and every one of those tests drives this
engine, because it is the only one. So nothing here re-checks a refusal or a repair.
This file is for the properties that belong to running the machine as a graph rather
than to the machine: that the nodes are the steps, that the routing is what walks a run,
that events reach the caller as they happen, and that the one way a graph can stop
without a verdict is caught and named.

This replaced a file of the same shape that ran every scenario through two engines and
asserted they agreed. The second engine is gone, so the agreement is not a property any
more, and a test comparing a thing to itself is worse than no test.
"""

import time
from unittest import mock

import pytest
from langgraph.errors import GraphRecursionError

from envforge.agent import STEPS, MAX_LOOKS, EngineFailure
from envforge.graph import Agent, build_graph, step_ceiling
from envforge.workspace import gather

from test_agent import ALLOW, FakeLLM, FakeSandbox, _build, _call, _look


@pytest.fixture
def script(tmp_path):
    path = tmp_path / "s.py"
    path.write_text("print('hello')\n")
    return gather(path)


@pytest.fixture
def long_script(tmp_path):
    path = tmp_path / "long.py"
    path.write_text("# head\n" + "# padding\n" * 500 + "\nimport tabulate\n"
                    + "# padding\n" * 500 + "\nprint(1)\n")
    return gather(path)


def drive(llm, sandbox, workspace, gate=ALLOW, **kwargs):
    events = list(Agent(llm, sandbox, gate, **kwargs).run(workspace, "python"))
    return events, [e.kind for e in events], events[-1].data["outcome"]


# --- the graph is the engine ----------------------------------------------------------

def test_the_graph_registers_exactly_the_steps_and_routes_uniformly():
    """The route map is uniform on purpose, and this says so rather than implying a shape.

    An earlier version asserted four particular edges were present and called that "two
    cycles". Every one of those assertions passes against a complete digraph, which is
    what this compiles to: every node to every node, plus an exit from each. A test that
    cannot distinguish the intended shape from every shape is not testing the shape.

    Uniform because each step returns one of the same four names, so encoding per-node
    which transitions are possible would be a second place that decision lives, free to
    disagree with the steps. Which transitions actually occur is asserted below, by
    watching a run.
    """
    compiled = build_graph().get_graph()
    nodes = {n for n in compiled.nodes} - {"__start__", "__end__"}
    assert nodes == set(STEPS) == {"author", "look", "execute"}
    routes = {(e.source, e.target) for e in compiled.edges}
    for source in nodes:
        assert {(source, target) for target in nodes} <= routes
        assert (source, "__end__") in routes


def test_the_routing_is_what_walks_a_run():
    """Replace the conditional edges with a straight edge to the exit and a run stops
    after one node. This is the check that the graph is doing the work rather than
    decorating a loop that is doing it somewhere else."""
    from langgraph.graph import END, StateGraph

    from envforge.graph import State, _node

    graph = StateGraph(State)
    for name in STEPS:
        graph.add_node(name, _node(name))
    graph.set_entry_point("author")
    for name in STEPS:
        graph.add_edge(name, END)          # no routing at all
    unrouted = graph.compile()

    from envforge.agent import start_run

    agent = Agent(FakeLLM(_call()), FakeSandbox(), ALLOW)
    run = start_run(agent, gather_one(), "python")
    produced = list(unrouted.stream({"run": run, "step": "author"},
                                    stream_mode="custom"))
    # One node ran and the run stopped, with no verdict at all.
    assert [event.kind for event in produced] == ["asking", "wrote"]
    assert not any(event.kind == "finished" for event in produced)


def gather_one(text: str = "print('hello')\n"):
    """A workspace without the fixture, for the one test that builds its own graph."""
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp())
    (directory / "s.py").write_text(text)
    return gather(directory / "s.py")


def test_the_transitions_a_run_actually_takes_are_the_two_cycles(long_script):
    """The real topology, observed. `look` only ever hands back to `author`, and the
    repair edge only ever leaves `execute`, so a run has exactly two cycles through the
    steps even though the route map permits more."""
    taken = []
    wrapped = {}
    for name, step in STEPS.items():
        def watched(run, name=name, step=step):
            went = yield from step(run)
            taken.append((name, went))
            return went
        wrapped[name] = watched

    replies = [_look("search_script", pattern="import"),
               _look("read_script", start=0, end=9),
               _call("FROM python:3.12-slim\nRUN nope\n"), _call()]
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom"), _build()])
    with mock.patch.dict(STEPS, wrapped):
        drive(FakeLLM(*replies), sandbox, long_script)

    assert {b for a, b in taken if a == "look"} == {"author"}
    assert ("execute", "author") in taken          # the repair cycle
    assert ("author", "look") in taken             # the tool cycle
    assert {b for a, b in taken} <= {"author", "look", "execute", "done"}


def test_the_graph_yields_each_event_once(long_script):
    """`stream_mode="custom"` delivers what a node writes as it writes it. "updates"
    would give one batch per node and "values" the whole accumulated state every step,
    which a consumer counting events reads as the run looping."""
    llm = FakeLLM(_look("search_script", pattern="import"), _call())
    events, kinds, _ = drive(llm, FakeSandbox(), long_script)
    assert kinds == ["asking", "looked", "asking", "wrote", "building", "running",
                     "finished"]
    assert len([e for e in events if e.kind == "finished"]) == 1


# --- the one way a graph stops without a verdict --------------------------------------

def test_the_step_ceiling_is_above_what_the_caps_allow(long_script):
    """LangGraph raises when a graph exceeds `recursion_limit`, and a run stopped that
    way produces no `finished` event, so it would end with no verdict rather than a bad
    one. The default of 25 is below what this machine legitimately uses."""
    assert step_ceiling(3, 1) > 25
    replies = []
    for _ in range(3):
        replies += [_look("search_script", pattern="x") for _ in range(MAX_LOOKS)]
        replies.append(_call("FROM python:3.12-slim\nRUN nope\n"))
    llm = FakeLLM(*replies)
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 3)
    events, kinds, outcome = drive(llm, sandbox, long_script)
    assert kinds.count("looked") == 3 * MAX_LOOKS
    assert kinds[-1] == "finished" and outcome.kind == "no_image"


def test_a_graph_that_runs_out_of_steps_is_not_silently_a_verdict(script):
    """The one failure mode this engine has that a loop does not.

    Translated into this project's own exception rather than surfacing as an unhandled
    traceback, which exits 1 and means "the script ran and failed" here. A run with no
    verdict must never be reported as a verdict about the sample.
    """
    import envforge.graph as graph_module

    llm = FakeLLM(*[_look("search_script", pattern="x") for _ in range(50)])
    agent = Agent(llm, FakeSandbox(), ALLOW)
    original, graph_module.step_ceiling = graph_module.step_ceiling, lambda *a: 4
    try:
        with pytest.raises(EngineFailure, match="no verdict"):
            list(agent.run(script, "python"))
    finally:
        graph_module.step_ceiling = original
    # And the raw library error is not what escapes, since nothing above should have to
    # import a graph library to catch it.
    assert not issubclass(EngineFailure, GraphRecursionError)


# --- streaming ------------------------------------------------------------------------
#
# These were written once, mutation tested, and then silently deleted by a slice edit
# that replaced the section above them, while the records went on claiming they existed.
# They are last in the file now, where a slice edit above cannot reach them, and the
# mutation that reintroduces the bug is written down in the build log.

SLOW = 0.4          # long enough to measure, short enough not to slow the suite


class Slow(FakeSandbox):
    """A sandbox whose build and run each take a measurable moment."""

    def build(self, dockerfile, files, tag):
        time.sleep(SLOW)
        return super().build(dockerfile, files, tag)

    def run(self, image, args=()):
        time.sleep(SLOW)
        return super().run(image, args)


class SlowLLM(FakeLLM):
    """A model that takes a moment, so `asking` can be timed too.

    Added because the first version only slowed the sandbox, which left the one step
    never measured: buffering `author` alone reddened nothing. `asking` moving from
    0.00s to 0.51s was the original regression's first symptom, so the test written for
    that regression could not see the half that started it.
    """

    def call(self, system, user, tools, history=()):
        time.sleep(SLOW)
        return super().call(system, user, tools, history)


def test_events_arrive_as_the_work_happens_and_not_in_one_batch(script):
    """`building` means a build is starting, not that one has finished.

    Timed rather than asserted on ordering, because ordering is identical either way and
    that is exactly how this broke with 352 tests watching. Every other test in this
    repository compares finished runs, and a finished run looks the same whether its
    events arrived as they happened or all at once at the end.

    What went wrong: the steps returned lists, so nothing left a step until the step was
    over, and `building` and `running` both arrived after the container had finished.
    The command line prints these live and passes `flush=True` to do it, so a cold-pull
    build taking five minutes showed the operator nothing and then dumped the whole run.
    """
    seen = []
    start = time.monotonic()
    for event in Agent(SlowLLM(_call()), Slow(), ALLOW).run(script, "python"):
        seen.append((event.kind, time.monotonic() - start))
    at = dict(seen)

    assert [kind for kind, _ in seen] == ["asking", "wrote", "building", "running",
                                          "finished"]
    assert at["finished"] >= 3 * SLOW          # three slow operations, one per step
    # Every one of them, including `asking`, which the sandbox-only version could not
    # see: buffering `author` alone reddened nothing.
    assert at["asking"] < SLOW / 2             # before the model was even called
    assert at["wrote"] - at["asking"] >= SLOW / 2
    assert at["running"] - at["building"] >= SLOW / 2
    assert at["finished"] - at["running"] >= SLOW / 2


def test_events_already_produced_survive_a_step_that_raises(script):
    """A step that dies mid-way keeps what it already said.

    `__main__` catches OSError from the daemon and prints its own message, and the
    `building envforge-...:attempt2` line is what says which attempt was in flight. While
    the steps buffered, that line was lost at precisely the moment it was worth having.
    """
    class Broken(FakeSandbox):
        def build(self, dockerfile, files, tag):
            raise OSError("the daemon went away")

    seen = []
    with pytest.raises(OSError):
        for event in Agent(FakeLLM(_call()), Broken(), ALLOW).run(script, "python"):
            seen.append(event.kind)
    assert seen == ["asking", "wrote", "building"]
