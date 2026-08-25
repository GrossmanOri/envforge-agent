"""Build an image from an untrusted Dockerfile and run it in a hardened container.

Nothing here decides anything. It builds argv, spawns the docker client, bounds what
comes back, and always removes the container. Judgement lives in gate.py and verdict.py.
"""

from __future__ import annotations

import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence

# Bytes kept per captured stream. Container output is attacker-controlled text and it
# reaches an LLM prompt later, so it is bounded at the source, not at the prompt.
OUTPUT_LIMIT = 16_384


class SandboxError(RuntimeError):
    """Our own docker command was wrong. Never raised for anything the script did."""


@dataclass(frozen=True)
class Limits:
    memory: str = "256m"
    pids: int = 128
    cpus: str = "1.0"
    run_timeout: float = 30.0
    build_timeout: float = 300.0


@dataclass(frozen=True)
class BuildResult:
    ok: bool
    image: str
    exit_code: int | None  # None means the build hit our timeout
    log: str
    truncated: bool
    timed_out: bool
    seconds: float


@dataclass(frozen=True)
class RunResult:
    exit_code: int | None  # None means the run hit our timeout
    stdout: str
    stderr: str
    truncated: bool
    timed_out: bool
    seconds: float
    # Why the container never started its process, straight from the daemon. Empty
    # when the process did start, whatever it then did. A script cannot fake this,
    # because a script that can write anything has already started.
    start_error: str = ""


class Sandbox(Protocol):
    """The seam. DockerSandbox now, a fake in tests, a Kubernetes Job later."""

    def build(self, dockerfile: str, files: Mapping[str, str], tag: str) -> BuildResult: ...

    def run(self, image: str, args: Sequence[str] = ()) -> RunResult: ...


def build_argv(tag: str, context: Path) -> list[str]:
    """Build has network on purpose: apt and pip need it (invariant 4).

    The context is a temp dir holding exactly the Dockerfile and the script, so the
    daemon never receives the user's working directory.
    """
    return [
        "docker", "build",
        "--tag", tag,
        "--file", str(context / "Dockerfile"),
        str(context),
    ]


def run_argv(
    image: str,
    name: str,
    cidfile: Path,
    limits: Limits,
    args: Sequence[str] = (),
) -> list[str]:
    """Every hardening flag from ARCHITECTURE.md invariant 5, in the argv itself."""
    argv = [
        "docker", "run",
        "--name", name,
        "--cidfile", str(cidfile),
        "--network", "none",
        "--memory", limits.memory,
        # Without this the container gets the same amount again as swap and the memory
        # cap stops meaning what it says.
        "--memory-swap", limits.memory,
        "--pids-limit", str(limits.pids),
        "--cpus", limits.cpus,
        "--read-only",
        "--tmpfs", "/tmp:rw,nosuid,nodev,noexec,size=64m",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges:true",
        # Enforced here as well as in the Dockerfile, because the Dockerfile is written
        # by the model and this line is not.
        "--user", "65534:65534",
        # Read-only root plus a real HOME, or half of pip and friends fail on a write
        # to ~ and the repair loop burns an attempt on our own cage.
        "--env", "HOME=/tmp",
        image,
    ]
    # Invariant 8: arguments go after the image and nowhere else, so an ENTRYPOINT image
    # receives "--privileged" as argv rather than docker receiving it as a flag.
    argv.extend(args)
    return argv


def _bound(raw: bytes, limit: int = OUTPUT_LIMIT) -> tuple[str, bool]:
    """Keep the head and the tail: the head says what ran, the tail says how it died."""
    if len(raw) <= limit:
        return raw.decode("utf-8", "replace"), False
    half = limit // 2
    head = raw[:half].decode("utf-8", "replace")
    tail = raw[-half:].decode("utf-8", "replace")
    return f"{head}\n... {len(raw) - limit} bytes cut ...\n{tail}", True


def _capture(argv: list[str], timeout: float) -> tuple[int | None, bytes, bytes, bool]:
    """Run a docker client to completion or to our timeout, always returning output."""
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = proc.communicate(timeout=timeout)
        return proc.returncode, out, err, False
    except subprocess.TimeoutExpired:
        # This kills the client only. The container is a child of the daemon and keeps
        # running, which is why every caller removes it by name in a finally.
        proc.kill()
        out, err = proc.communicate()
        return None, out, err, True


def _start_error(name: str) -> str:
    """The daemon's own account of why the process never started.

    This is the witness that separates an image whose command cannot be executed
    from a script that exited 126 or 127 to look like one. It has to be read
    before the container is removed, which is why the caller does both in order
    inside one finally.
    """
    try:
        done = subprocess.run(
            ["docker", "inspect", name, "--format", "{{.State.Error}}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""  # No witness is not the same as a witness saying nothing.
    return done.stdout.strip() if done.returncode == 0 else ""


def _force_remove(name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "--force", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass  # A wedged daemon is not a reason to lose the run's result.


class DockerSandbox:
    """Docker CLI on the host. No socket is mounted anywhere (invariant 7)."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()

    def build(self, dockerfile: str, files: Mapping[str, str], tag: str) -> BuildResult:
        """Every file lands in the context root under its own name, which is the name a
        COPY must use.

        Contents rather than paths, so the bytes the model reviewed and the bytes the
        container runs are the same bytes. Copying from disk here would have been a
        second read, and a file can change between two reads.
        """
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="envforge-ctx-") as tmp:
            context = Path(tmp)
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            for name, content in files.items():
                if "/" in name or "\\" in name or name in (".", ".."):
                    # A precondition, not a second copy of the rule. The workspace owns
                    # what a legal filename is, decides it once at ingestion and says so
                    # to a person. This only states what `build` requires in order to be
                    # correct, because it writes these names into a directory. If it
                    # ever fires, nobody typed anything wrong and our own code broke a
                    # contract, which is exactly what SandboxError means here.
                    raise SandboxError(f"caller passed {name!r}, which is not a bare "
                                       "filename. The workspace should have refused it")
                (context / name).write_text(content, encoding="utf-8")
            code, out, err, timed_out = _capture(
                build_argv(tag, context), self.limits.build_timeout
            )
        log, truncated = _bound(out + err)
        return BuildResult(
            ok=code == 0,
            image=tag,
            exit_code=code,
            log=log,
            truncated=truncated,
            timed_out=timed_out,
            seconds=time.monotonic() - started,
        )

    def run(self, image: str, args: Sequence[str] = ()) -> RunResult:
        name = f"envforge-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="envforge-cid-") as tmp:
            # docker refuses to overwrite a cidfile, so the path must not exist yet.
            cidfile = Path(tmp) / "cid"
            start_error = ""
            try:
                code, out, err, timed_out = _capture(
                    run_argv(image, name, cidfile, self.limits, args),
                    self.limits.run_timeout,
                )
            finally:
                # Order matters and removal must still be unconditional: read the
                # witness first, then remove whatever happened while reading it.
                try:
                    start_error = _start_error(name)
                finally:
                    _force_remove(name)
            created = cidfile.exists()

        stdout, cut_out = _bound(out)
        stderr, cut_err = _bound(err)
        # 125 is ambiguous: docker returns it when it rejects our command, and a script
        # is free to exit 125 on its own. The cidfile settles it, because docker writes
        # it only once the container exists. Our bug raises, the script's exit is data.
        if code == 125 and not created:
            raise SandboxError(f"docker rejected our run command: {stderr.strip()}")
        return RunResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            truncated=cut_out or cut_err,
            timed_out=timed_out,
            seconds=time.monotonic() - started,
            start_error=start_error,
        )

    def remove_image(self, tag: str) -> None:
        subprocess.run(
            ["docker", "image", "rm", "--force", tag],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
