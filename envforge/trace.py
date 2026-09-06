"""An opt-in, bounded JSONL record of emitted events, never a checkpoint.

Only event payloads are accepted: no environment, model clients or graph state.
Text can include script excerpts and container output and is not secret-redacted.
The parent directory must be chosen by the operator and trusted against replacement.
"""
from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import fields
from datetime import datetime, timezone
from pathlib import Path

from .agent import Outcome, Usage
from .events import Event
from .sandbox import BuildResult, RunResult

STRING_LIMIT = 16_384
FILE_LIMIT = 4 * 1024 * 1024


class TraceError(RuntimeError):
    """Recording failed. Stop execution; the destination may contain a partial line."""


def _bounded(value, path: str, truncated: list[str]):
    if isinstance(value, str):
        if len(value) > STRING_LIMIT:
            truncated.append(path)
        return value[:STRING_LIMIT]
    if value is None or type(value) in (bool, int, float):
        return value
    if type(value) in (Outcome, Usage, BuildResult, RunResult):
        return {f.name: _bounded(getattr(value, f.name), f"{path}.{f.name}", truncated)
                for f in fields(value)}
    if isinstance(value, (list, tuple)):
        if len(value) > 64:
            truncated.append(path)
        return [_bounded(v, f"{path}[{i}]", truncated)
                for i, v in enumerate(value[:64])]
    # No repr/default=str fallback: a new object could contain credentials.
    raise TraceError("unsupported event payload type")


class Trace:
    """Write and flush before the emitting node continues.

    Sequence and timestamps describe recording, not independent activity telemetry.
    A valid end record closes the CLI invocation; `complete` also requires an outcome.
    Missing end, broken JSON or a non-contiguous sequence means a partial record.
    No power-loss durability or resume guarantee follows from a flushed file.
    """

    def __init__(self, path: Path):
        self.run_id = uuid.uuid4().hex
        self.started = time.monotonic()
        self.sequence = 0
        self.size = 0
        self.has_outcome = False
        self.failed = False
        self.ended = False
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        try:
            fd = os.open(path, flags, 0o600)
            self.file = os.fdopen(fd, "wb", buffering=0)
        except OSError as exc:
            raise TraceError("cannot create trace destination") from exc
        try:
            self._write({"type": "header", "format": "envforge.events",
                         "provenance": "possible_sources_by_event_kind",
                         "string_limit": STRING_LIMIT, "file_limit_bytes": FILE_LIMIT,
                         "coverage": "emitted events only; not a full conversation",
                         "sensitive_content": "may include script excerpts and output"})
        except BaseException:
            self.close()
            raise

    def _write(self, record: dict) -> None:
        if self.failed or self.ended:
            raise TraceError("trace is no longer writable")
        envelope = {"schema_version": 1, "run_id": self.run_id,
                    "sequence": self.sequence,
                    "time": datetime.now(timezone.utc).isoformat(),
                    "elapsed_seconds": time.monotonic() - self.started, **record}
        try:
            payload = (json.dumps(envelope, ensure_ascii=True, allow_nan=False,
                                  separators=(",", ":")) + "\n").encode("ascii")
            if self.size + len(payload) > FILE_LIMIT:
                raise TraceError("trace size limit reached")
            pending = memoryview(payload)
            while pending:
                written = self.file.write(pending)
                if not written:
                    raise OSError("short trace write")
                pending = pending[written:]
            self.size += len(payload)
            self.sequence += 1
        except (OSError, ValueError, TypeError, TraceError) as exc:
            self.failed = True
            raise TraceError("trace size limit reached" if isinstance(exc, TraceError)
                             else "cannot write trace record") from exc

    def event(self, event: Event) -> None:
        truncated: list[str] = []
        record = {"type": "event", "kind": event.kind,
                  "message": _bounded(event.message, "message", truncated),
                  "data": {k: _bounded(v, f"data.{k}", truncated)
                           for k, v in event.data.items()},
                  "possible_authors": {k: sorted(a.value for a in event.authors(k))
                              for k in ("message", *event.data)},
                  "truncated_fields": truncated}
        self._write(record)
        if event.kind == "finished":
            self.has_outcome = True

    def finish(self, exit_code: int, status: str) -> None:
        self._write({"type": "end", "exit_code": exit_code, "status": status,
                     "complete": self.has_outcome and status == "finished"})
        self.ended = True

    def close(self) -> None:
        try:
            self.file.close()
        except OSError as exc:
            raise TraceError("cannot close trace destination") from exc
