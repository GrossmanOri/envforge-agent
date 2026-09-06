"""Run records through the real CLI and graph, with offline external adapters."""
import json
import stat

import pytest

import envforge.__main__ as cli
import envforge.graph as graph
from envforge.agent import EngineFailure
from envforge.llm import ProviderUnavailable
from envforge.tools import SLICE_HEADER
from tests.test_graph import FakeModel, FakeSandbox, _ran, looks_at, submits


@pytest.fixture
def offline(tmp_path, monkeypatch):
    script = tmp_path / "s.py"
    script.write_text("print('ALPHA')\n")
    sandbox = FakeSandbox(runs=[_ran(stdout="hello\n")])
    model = FakeModel(looks_at("read_script", start=0, end=5), submits())
    monkeypatch.setattr(cli, "load_env", lambda: [])
    monkeypatch.setenv("ENVFORGE_TRACE_TEST", "not-for-the-record")
    monkeypatch.setattr(cli, "make_llm", lambda spec: model)
    monkeypatch.setattr(cli, "daemon_error", lambda: None)
    monkeypatch.setattr(cli, "DockerSandbox", lambda: sandbox)
    monkeypatch.setattr(graph, "sweep", lambda **kw: [])
    monkeypatch.setattr(graph, "container_exists", lambda name: False)
    monkeypatch.setattr(graph, "remove_container", lambda name: None)
    return script, tmp_path / "run.jsonl", sandbox, model


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_cli_trace_preserves_real_events_and_metadata(offline):
    script, path, sandbox, model = offline
    assert cli.main([str(script), "--trace", str(path)]) == 0
    rows = records(path)
    events = [r for r in rows if r["type"] == "event"]
    assert [r.get("kind") for r in events] == [
        "asking", "looked", "asking", "wrote", "building", "running", "finished"]
    look = next(r for r in events if r["kind"] == "looked")
    assert look["data"]["result"] == SLICE_HEADER + "\n\ncharacters 0 to 5 of 15:\nprint"
    assert look["possible_authors"]["result"] == ["input", "tool"]
    result = events[-1]["data"]["outcome"]
    assert (result["run"]["stdout"], result["run"]["exit_code"],
            result["usage"]["calls"], result["usage"]["unreported_calls"]) == (
                "hello\n", 0, 2, 2)
    assert (rows[0]["type"], rows[-1]["type"], rows[-1]["status"],
            rows[-1]["complete"], rows[-1]["exit_code"]) == (
                "header", "end", "finished", True, 0)
    assert [r["sequence"] for r in rows] == list(range(len(rows)))
    assert {r["run_id"] for r in rows} == {result["run_id"]}
    assert all(r["schema_version"] == 1 and r["time"].endswith("+00:00")
               and r["elapsed_seconds"] >= 0 for r in rows)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert "ALPHA" not in path.read_text() and "not-for-the-record" not in path.read_text()


def test_no_flag_creates_no_record(offline):
    script, path, sandbox, model = offline
    cli.main([str(script)])
    assert sorted(p.name for p in path.parent.iterdir()) == ["s.py"]


@pytest.mark.parametrize("symlink", [False, True])
def test_existing_destination_refuses_before_provider_or_build(offline, symlink):
    script, path, sandbox, model = offline
    target = path.parent / "existing"
    target.write_text("keep")
    if symlink:
        path.symlink_to(target)
    else:
        path.write_text("keep")
    assert cli.main([str(script), "--trace", str(path)]) == 8
    assert (path.read_text(), target.read_text(), model.seen_messages,
            sandbox.built_tags) == ("keep", "keep", [], [])


def test_provider_failure_is_counted_with_unknown_usage(offline):
    script, path, sandbox, model = offline
    model.queue = [ProviderUnavailable("offline", kind="connection")]
    assert cli.main([str(script), "--trace", str(path)]) == 3
    outcome = next(r["data"]["outcome"] for r in records(path)
                   if r.get("kind") == "finished")
    assert (outcome["usage"]["calls"], outcome["usage"]["unreported_calls"],
            outcome["run"]) == (1, 1, None)
    row = next(r for r in records(path) if r.get("kind") == "finished")
    assert (records(path)[0]["provenance"], row["possible_authors"]["outcome"]) == (
        "possible_sources_by_event_kind", ["container", "input", "model", "provider", "us"])


@pytest.mark.parametrize("failure,code,status", [
    (EngineFailure("broken"), 4, "engine_error"),
    (KeyboardInterrupt(), 130, "interrupted"),
])
def test_interruption_and_engine_failure_leave_honest_record(
        offline, monkeypatch, failure, code, status):
    script, path, sandbox, model = offline
    def broken(*a, **kw):
        raise failure
    monkeypatch.setattr(sandbox, "run", broken)
    assert cli.main([str(script), "--trace", str(path)]) == code
    end = records(path)[-1]
    assert (end["status"], end["complete"], end["exit_code"], sandbox.removed) == (
        status, False, code, sandbox.built_tags)


def test_write_failure_stops_before_run_and_cleans_images(offline, monkeypatch):
    from envforge.trace import Trace, TraceError
    script, path, sandbox, model = offline
    original = Trace._write
    def fail(self, record):
        if record.get("kind") == "running":
            raise TraceError("disk full")
        return original(self, record)
    monkeypatch.setattr(Trace, "_write", fail)
    assert cli.main([str(script), "--trace", str(path)]) == 8
    assert (sandbox.ran_as, sandbox.removed, records(path)[-1]["type"]) == (
        [], sandbox.built_tags, "event")


def test_record_bounds_escape_newlines_and_identify_truncation(tmp_path):
    from envforge.trace import Trace, STRING_LIMIT
    from envforge.events import Event
    path = tmp_path / "bounded.jsonl"
    trace = Trace(path)
    try:
        trace.event(Event("asking", "x" * (STRING_LIMIT + 3) + "\n\x1b"))
    finally:
        trace.close()
    row = records(path)[1]
    assert (len(row["message"]), row["truncated_fields"], row["message"][-1]) == (
        STRING_LIMIT, ["message"], "x")
    assert len(path.read_text().splitlines()) == 2


def test_trace_size_limit_fails_without_completion(tmp_path, monkeypatch):
    import envforge.trace as module
    from envforge.events import Event
    path = tmp_path / "limited.jsonl"
    trace = module.Trace(path)
    monkeypatch.setattr(module, "FILE_LIMIT", path.stat().st_size)
    try:
        with pytest.raises(module.TraceError, match="size limit"):
            trace.event(Event("asking", "hello"))
    finally:
        trace.close()
    assert [r["type"] for r in records(path)] == ["header"]


def test_finished_script_failure_is_complete_but_not_success(offline):
    script, path, sandbox, model = offline
    sandbox.runs = [_ran(exit_code=17)]
    assert cli.main([str(script), "--trace", str(path)]) == 1
    end = records(path)[-1]
    assert (end["complete"], end["exit_code"], end["status"]) == (True, 1, "finished")


def test_reported_usage_survives_a_later_provider_failure(offline, capsys):
    script, path, sandbox, model = offline
    reply = looks_at("read_script", start=0, end=5)
    reply.usage_metadata = {"input_tokens": 11, "output_tokens": 7, "total_tokens": 18}
    model.queue = [reply, ProviderUnavailable("offline", kind="connection")]
    cli.main([str(script), "--trace", str(path)])
    outcome = next(r["data"]["outcome"] for r in records(path)
                   if r.get("kind") == "finished")
    assert outcome["usage"] == {"calls": 2, "input_tokens": 11, "output_tokens": 7,
                                "looks": 1, "unreported_calls": 1}
    assert "token totals are incomplete" in capsys.readouterr().out


def test_unknown_lookup_is_not_claimed_as_observed_execution(offline, monkeypatch):
    script, path, sandbox, model = offline
    monkeypatch.setattr(graph, "container_exists", lambda name: True)
    monkeypatch.setattr(graph, "container_running", lambda name: False)
    cli.main([str(script), "--trace", str(path)])
    outcome = next(r["data"]["outcome"] for r in records(path)
                   if r.get("kind") == "finished")
    assert (outcome["reason"], outcome["run"], sandbox.ran_as) == (
        "a container exists or its absence could not be verified. "
        "Refusing execution because this attempt may already have run", None, [])


def test_closing_a_trace_failure_is_reported(offline, monkeypatch, capsys):
    from envforge.trace import Trace, TraceError
    script, path, sandbox, model = offline
    original = Trace.close
    def broken(self):
        original(self)
        raise TraceError("close failed")
    monkeypatch.setattr(Trace, "close", broken)
    assert cli.main([str(script), "--trace", str(path)]) == 8
    assert "trace failed" in capsys.readouterr().err


def test_partial_os_write_is_completed_and_io_errors_poison_writer(tmp_path):
    from envforge.trace import Trace, TraceError
    from envforge.events import Event
    path = tmp_path / "short.jsonl"
    trace = Trace(path)
    raw = trace.file
    class Short:
        fail = False
        def write(self, value):
            if self.fail:
                raise OSError("disk full")
            return raw.write(value[:7])
        def close(self):
            raw.close()
    trace.file = Short()
    try:
        trace.event(Event("asking", "שלום\n\x1b"))
        assert path.read_bytes().endswith(b"\n")
        assert records(path)[-1]["message"] == "שלום\n\x1b"
        trace.file.fail = True
        with pytest.raises(TraceError, match="cannot write"):
            trace.event(Event("asking", "lost"))
        trace.file.fail = False
        with pytest.raises(TraceError, match="no longer writable"):
            trace.finish(0, "finished")
    finally:
        trace.close()


def test_unknown_objects_are_never_stringified(tmp_path):
    from envforge.trace import Trace, TraceError
    from envforge.events import Event
    trace = Trace(tmp_path / "unknown.jsonl")
    try:
        with pytest.raises(TraceError, match="unsupported"):
            trace.event(Event("wrote", "candidate", {
                "base_image": "python", "call": object(), "run_id": trace.run_id}))
    finally:
        trace.close()


@pytest.mark.parametrize("extra", [["--check"], []])
def test_trace_requires_a_run(offline, extra):
    script, path, sandbox, model = offline
    assert cli.main(["--trace", str(path), *extra]) == 2
    assert not path.exists()
