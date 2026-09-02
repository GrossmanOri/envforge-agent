"""Both engines, the same inputs, the same events.

This file is the seam. `ARCHITECTURE.md` says the interface between engines is a
labelled vocabulary rather than a topology, and that claim is only worth anything if
something checks that two engines actually honour it. Every test here runs the same
scenario through the plain loop and through the graph and asserts they agree, so a change
that makes one engine behave differently fails here rather than being discovered later by
whoever happened to switch engines.

Parametrised rather than written twice on purpose. A pair of test files would drift the
same way two implementations of the loop would. A handful of tests at the end are
graph-only, because they are about the one engine that can fail in ways the other cannot.
"""

import time
from dataclasses import fields
from unittest import mock

import pytest

from langgraph.errors import GraphRecursionError

from envforge.agent import STEPS, Agent, MAX_LOOKS
from envforge.graph import GraphAgent, step_ceiling
from envforge.llm import ProviderUnavailable, Refused
from envforge.sandbox import BuildResult
from envforge.workspace import gather

from test_agent import (ALLOW, FakeLLM, FakeSandbox, _build, _call, _look, _run)

ENGINES = [Agent, GraphAgent]
IDS = ["plain", "graph"]


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


def drive(engine, llm, sandbox, workspace, gate=ALLOW, **kwargs):
    events = list(engine(llm, sandbox, gate, **kwargs).run(workspace, "python"))
    return events, [e.kind for e in events], events[-1].data["outcome"]


def both(llm_replies, sandbox_args, workspace, gate=ALLOW, **kwargs):
    """Run one scenario through both engines and return what each produced.

    The fakes are rebuilt per engine rather than shared: a queue is consumed by the
    first run, so sharing one would feed the second engine an empty queue and the
    comparison would be between a run and a crash.
    """
    results = []
    for engine in ENGINES:
        llm = FakeLLM(*llm_replies())
        sandbox = FakeSandbox(**sandbox_args())
        results.append(drive(engine, llm, sandbox, workspace, gate, **kwargs))
    return results


def assert_agree(first, second):
    """The two engines produced the same run.

    Compared on the event kinds in order and on every field of the outcome that is not a
    fresh identifier. `run_id` is a uuid per run and the image tag contains it, so those
    are the two things that must differ and everything else must not.
    """
    (events_a, kinds_a, outcome_a), (events_b, kinds_b, outcome_b) = first, second
    assert kinds_a == kinds_b
    # Messages too, not only kinds. The give-up path had two engines agreeing on every
    # kind while the words the command line prints had silently changed.
    #
    # The run id is normalised out first: it is a fresh uuid per run and the image tag
    # is built from it, so `building envforge-<id>:attempt1` differs between any two
    # runs of anything. Blanking it is what leaves the rest of the sentence comparable.
    def worded(events, outcome):
        return [e.message.replace(outcome.run_id, "<run>") for e in events]

    assert worded(events_a, outcome_a) == worded(events_b, outcome_b)
    # Every field except `run_id`, which is a fresh uuid per run by design. `build` and
    # `run` are compared because they are what the verdict will read, and the records
    # said "every field" while this list quietly held eight of eleven.
    compared = [f.name for f in fields(outcome_a) if f.name != "run_id"]
    for field in compared:
        assert getattr(outcome_a, field) == getattr(outcome_b, field), field


# --- the scenarios --------------------------------------------------------------------

def test_the_happy_path_is_identical(script):
    a, b = both(lambda: [_call()], lambda: {}, script)
    assert_agree(a, b)
    assert a[1] == ["asking", "wrote", "building", "running", "finished"]
    assert a[2].ok and a[2].attempts == 1


def test_a_repair_is_identical(script):
    a, b = both(lambda: [_call("FROM python:3.12-slim\nRUN nope\n"), _call()],
                lambda: {"builds": [_build(ok=False, exit_code=1, log="boom"), _build()]},
                script)
    assert_agree(a, b)
    # The repair back-edge fired in both. In the graph that is an edge, not a `continue`.
    assert a[1].count("building") == 2 and a[2].attempts == 2


def test_the_tool_loop_is_identical(long_script):
    a, b = both(lambda: [_look("search_script", pattern="import"),
                         _look("read_script", start=4000, end=5000), _call()],
                lambda: {}, long_script)
    assert_agree(a, b)
    assert a[1].count("looked") == 2
    assert a[2].usage.looks == 2 and a[2].usage.calls == 3


def test_the_look_cap_is_identical(long_script):
    a, b = both(lambda: [_look("search_script", pattern="x") for _ in range(MAX_LOOKS)]
                        + [_call()],
                lambda: {}, long_script)
    assert_agree(a, b)
    assert a[1].count("tool_capped") == 1
    assert a[2].attempts == 1          # looking is not attempting, in either engine


def test_giving_up_is_identical(script):
    a, b = both(lambda: [_call("FROM python:3.12-slim\nRUN nope\n")] * 3,
                lambda: {"builds": [_build(ok=False, exit_code=1, log="boom")] * 3},
                script)
    assert_agree(a, b)
    assert a[2].kind == "no_image" and a[2].attempts == 3


def test_a_gate_rejection_is_identical(script):
    a, b = both(lambda: [_call("FROM python\n"), _call()], lambda: {}, script,
                gate=lambda d, base, files: None if "slim" in d else "FROM is not pinned")
    assert_agree(a, b)
    assert a[1][:3] == ["asking", "wrote", "gate_rejected"]


def test_a_refusal_and_the_fallback_are_identical(script):
    a, b = both(lambda: [Refused("no", reason="r"), Refused("no", reason="r")],
                lambda: {}, script)
    assert_agree(a, b)
    assert a[1].count("refused") == 2 and "fell_back" in a[1]
    assert a[2].used_fallback


def test_an_unreachable_provider_ends_both_the_same_way(script):
    a, b = both(lambda: [ProviderUnavailable("dead key", kind="auth")], lambda: {}, script)
    assert_agree(a, b)
    assert a[2].kind == "unavailable" and not a[2].ok
    # Never the fallback. A verdict no judgment went into must not look like a success.
    assert not a[2].used_fallback


def test_a_script_that_fails_is_identical(script):
    a, b = both(lambda: [_call()], lambda: {"runs": [_run(exit_code=1, stderr="boom")]},
                script)
    assert_agree(a, b)
    assert a[2].ok and a[2].kind == "script_failed"


def test_a_build_timeout_buys_the_same_free_rebuild_in_both(script):
    """The one transition that spends an attempt and makes no model call.

    Both engines have to agree on that, and it is the case most likely to differ,
    because in the graph it is the only edge that goes back to `execute` rather than to
    `author`. Written with a genuinely timed-out BuildResult: the first version used
    `_build(ok=False)`, which is an ordinary build failure, so it exercised the repair
    path and proved nothing about timeouts.
    """
    timed_out = BuildResult(ok=False, image="", exit_code=1, log="", truncated=False,
                            timed_out=True, seconds=300.0)
    a, b = both(lambda: [_call()],
                lambda: {"builds": [timed_out, _build()]}, script)
    assert_agree(a, b)
    # One model call, two builds, and the attempt was spent on the rebuild.
    assert a[2].usage.calls == 1
    assert a[1].count("building") == 2 and a[1].count("asking") == 1
    assert a[2].ok and a[2].attempts == 2


@pytest.mark.parametrize("engine", ENGINES, ids=IDS)
def test_an_unsupported_language_is_refused_at_the_door(engine, script):
    events = list(engine(FakeLLM(), FakeSandbox(), ALLOW).run(script, "ruby"))
    assert [e.kind for e in events] == ["finished"]
    assert events[0].data["outcome"].kind == "unsupported"


# --- the graph's own properties -------------------------------------------------------

def test_the_step_ceiling_is_above_what_the_caps_allow(long_script):
    """LangGraph raises when a graph exceeds `recursion_limit`, and a run stopped that
    way produces no `finished` event, so it would end with no verdict rather than a bad
    one. The default of 25 is below what this machine legitimately uses."""
    assert step_ceiling(3, 1) > 25
    # And a run that uses every look of every attempt still finishes normally.
    replies = []
    for _ in range(3):
        replies += [_look("search_script", pattern="x") for _ in range(MAX_LOOKS)]
        replies.append(_call("FROM python:3.12-slim\nRUN nope\n"))
    llm, sandbox = FakeLLM(*replies), FakeSandbox(
        builds=[_build(ok=False, exit_code=1, log="boom")] * 3)
    events, kinds, outcome = drive(GraphAgent, llm, sandbox, long_script)
    assert kinds.count("looked") == 3 * MAX_LOOKS
    assert kinds[-1] == "finished" and outcome.kind == "no_image"


def test_the_graph_yields_each_event_once(long_script):
    """`stream_mode="custom"` delivers what a node writes as it writes it. "updates" would
    give one batch per node and "values" the whole accumulated state every step, which a
    consumer counting events reads as the run looping."""
    llm = FakeLLM(_look("search_script", pattern="import"), _call())
    events, kinds, _ = drive(GraphAgent, llm, FakeSandbox(), long_script)
    assert kinds == ["asking", "looked", "asking", "wrote", "building", "running",
                     "finished"]
    assert len([e for e in events if e.kind == "finished"]) == 1


def test_the_graph_registers_exactly_the_steps_and_routes_uniformly():
    """The route map is uniform on purpose, and this says so rather than implying a shape.

    An earlier version of this test asserted that four particular edges were present and
    called that "two cycles". Every one of those assertions passes against a complete
    digraph, which is what this compiles to: thirteen edges, every node to every node,
    plus an exit from each. A test that cannot distinguish the intended shape from every
    shape is not testing the shape.

    Uniform because each step returns one of the same four names, so encoding per-node
    which transitions are possible would be a second place that decision lives, free to
    disagree with the steps. Which transitions actually occur is asserted below, by
    watching a run rather than by reading the edge list.
    """
    from envforge.graph import build_graph

    compiled = build_graph().get_graph()
    nodes = {n for n in compiled.nodes} - {"__start__", "__end__"}
    assert nodes == set(STEPS) == {"author", "look", "execute"}
    routes = {(e.source, e.target) for e in compiled.edges}
    for source in nodes:
        assert {(source, target) for target in nodes} <= routes
        assert (source, "__end__") in routes


def test_the_transitions_a_run_actually_takes_are_the_two_cycles(long_script):
    """The real topology, observed. `look` only ever hands back to `author`, and the
    repair edge only ever leaves `execute`, so the machine has exactly the two cycles the
    records describe even though the route map permits more."""
    taken = []
    wrapped = {}
    for name, step in STEPS.items():
        def watched(run, name=name, step=step):
            went = yield from step(run)
            taken.append((name, went))
            return went
        wrapped[name] = watched

    replies = [_look("search_script", pattern="import"), _look("read_script", start=0, end=9),
               _call("FROM python:3.12-slim\nRUN nope\n"), _call()]
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom"), _build()])
    with mock.patch.dict(STEPS, wrapped):
        drive(GraphAgent, FakeLLM(*replies), sandbox, long_script)

    assert {b for a, b in taken if a == "look"} == {"author"}
    assert ("execute", "author") in taken          # the repair cycle
    assert ("author", "look") in taken             # the tool cycle
    assert {b for a, b in taken} <= {"author", "look", "execute", "done"}


# --- what the refactor nearly changed without anyone noticing -------------------------

@pytest.mark.parametrize("engine", ENGINES, ids=IDS)
def test_giving_up_says_one_thing_to_a_person_and_another_to_the_outcome(engine, script):
    """Two strings, deliberately different, and the refactor collapsed them into one.

    The event is what the command line prints and the reason is what the report records.
    A single `_finished` helper filling both from one argument changed the printed line,
    and nothing caught it because no test in this repository asserted an event's message
    until the contract test started comparing them.
    """
    llm = FakeLLM(*[_call("FROM python:3.12-slim\nRUN nope\n")] * 3)
    sandbox = FakeSandbox(builds=[_build(ok=False, exit_code=1, log="boom")] * 3)
    events, _, outcome = drive(engine, llm, sandbox, script)
    assert events[-1].message == "gave up after 3 attempts"
    assert outcome.reason == "no Dockerfile worked in 3 attempts"


@pytest.mark.parametrize("engine", ENGINES, ids=IDS)
def test_no_attempts_configured_means_nothing_runs(engine, script):
    """`max_attempts=0` must build nothing and call nothing.

    The old loop tested its bound before running a step; the machine's `_next_attempt`
    tests it after one has already gone, so without a guard at the door a run configured
    to make no attempts authored, built, ran and reported success.
    """
    llm, sandbox = FakeLLM(_call()), FakeSandbox()
    events, kinds, outcome = drive(engine, llm, sandbox, script, max_attempts=0)
    assert kinds == ["finished"]
    assert outcome.kind == "no_image" and outcome.attempts == 0 and not outcome.ok
    assert sandbox.built == [] and llm.prompts == []


def test_a_graph_that_runs_out_of_steps_is_not_silently_a_verdict(script):
    """LangGraph raises when a graph exceeds `recursion_limit`, and the run emits no
    `finished` event at all, so there is no outcome to report.

    That is the one failure mode this engine has and the plain one does not, so it is
    translated into this project's own exception rather than surfacing as an unhandled
    traceback, which exits 1 and means "the script ran and failed" here. A run with no
    verdict must never be reported as a verdict about the sample.
    """
    import envforge.graph as graph_module
    from envforge.__main__ import EngineFailure

    llm = FakeLLM(*[_look("search_script", pattern="x") for _ in range(50)])
    agent = GraphAgent(llm, FakeSandbox(), ALLOW)
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
# These two were written once, mutation tested, and then silently deleted by a slice edit
# that replaced the section above them, while STATUS went on claiming they existed. They
# are last in the file now, and the mutation that reintroduces the buffering is recorded
# in the build log so the next person can re-run it rather than trust this comment.

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

    Added because the first version of this only slowed the sandbox, which left the one
    step that was never measured: buffering `author` alone reddened nothing. `asking`
    moving from 0.00s to 0.51s was the original regression's first symptom, so the test
    written for that regression could not see the half of it that started it.
    """

    def call(self, system, user, tools, history=()):
        time.sleep(SLOW)
        return super().call(system, user, tools, history)


@pytest.mark.parametrize("engine", ENGINES, ids=IDS)
def test_events_arrive_as_the_work_happens_and_not_in_one_batch(engine, script):
    """`building` means a build is starting, not that one has finished.

    Timed rather than asserted on ordering, because ordering is identical either way and
    that is exactly how this broke with 352 tests watching. Every other test here
    compares finished runs, and a finished run looks the same whether its events arrived
    as they happened or all at once at the end.

    What went wrong: the steps returned lists, so nothing left a step until the step was
    over, and `building` and `running` both arrived after the container had finished. The
    command line prints these live and passes `flush=True` to do it, so a cold-pull build
    taking five minutes showed the operator nothing and then dumped the whole run.
    """
    seen = []
    start = time.monotonic()
    for event in engine(SlowLLM(_call()), Slow(), ALLOW).run(script, "python"):
        seen.append((event.kind, time.monotonic() - start))
    at = dict(seen)

    assert [kind for kind, _ in seen] == ["asking", "wrote", "building", "running",
                                          "finished"]
    assert at["finished"] >= 3 * SLOW          # three slow operations, one per step
    # Every one of them, including `asking`, which the sandbox-only version of this
    # could not see: buffering `author` alone reddened nothing.
    assert at["asking"] < SLOW / 2             # before the model was even called
    assert at["wrote"] - at["asking"] >= SLOW / 2
    assert at["running"] - at["building"] >= SLOW / 2
    assert at["finished"] - at["running"] >= SLOW / 2


@pytest.mark.parametrize("engine", ENGINES, ids=IDS)
def test_events_already_produced_survive_a_step_that_raises(engine, script):
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
        for event in engine(FakeLLM(_call()), Broken(), ALLOW).run(script, "python"):
            seen.append(event.kind)
    assert seen == ["asking", "wrote", "building"]
