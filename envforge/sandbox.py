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
from types import MappingProxyType
from typing import Mapping, Protocol, Sequence

# Filenames the build itself interprets, so a context file carrying one of these is not
# an input to the build but a change to it. Compared case-insensitively because a
# case-insensitive filesystem collides them anyway, which is most laptops.
RESERVED_NAMES = frozenset({"dockerfile", ".dockerignore"})

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
    """The seam. DockerSandbox now, a fake in tests, a Kubernetes Job later.

    `built_tags` and `remove_image` are on the seam because a caller has to be able to
    clean up what a run created. They were reached for through the protocol without
    being declared on it, so substituting a conforming sandbox made the program crash
    after the run had already finished and before it printed its answer.
    """

    built_tags: list[str]

    def build(self, dockerfile: str, files: Mapping[str, str], tag: str,
              labels: Mapping[str, str] = MappingProxyType({})) -> BuildResult: ...

    def run(self, image: str, args: Sequence[str] = (), name: str | None = None,
            labels: Mapping[str, str] = MappingProxyType({})) -> RunResult: ...

    def remove_image(self, tag: str) -> None: ...


# The labels every image and container we create carries.
#
# A label rather than a name prefix, because a sweep has to be able to say "this is ours"
# about somebody else's machine. A prefix match on `envforge-` would also match a
# container a user named that themselves, and deleting it would be our bug in their
# workspace.
#
# The start time is a label rather than read from the object's own creation timestamp so
# the sweep never parses a date. `docker` prints creation times in a human format that
# varies with locale and version, and an age rule built on parsing that is a rule that
# breaks on somebody else's machine.
RUN_LABEL = "envforge.run"
STARTED_LABEL = "envforge.started"


def labels_for(run_id: str) -> dict[str, str]:
    return {RUN_LABEL: run_id, STARTED_LABEL: str(int(time.time()))}


def _label_argv(labels: Mapping[str, str]) -> list[str]:
    return [part for key, value in labels.items()
            for part in ("--label", f"{key}={value}")]


def build_argv(tag: str, context: Path,
               labels: Mapping[str, str] = MappingProxyType({})) -> list[str]:
    """Build has network on purpose: apt and pip need it (invariant 4).

    The context is a temp dir holding exactly the Dockerfile and the script, so the
    daemon never receives the user's working directory.
    """
    return [
        "docker", "build",
        "--tag", tag,
        *_label_argv(labels),
        "--file", str(context / "Dockerfile"),
        str(context),
    ]


def run_argv(
    image: str,
    name: str,
    cidfile: Path,
    limits: Limits,
    args: Sequence[str] = (),
    labels: Mapping[str, str] = MappingProxyType({}),
) -> list[str]:
    """Every hardening flag from ARCHITECTURE.md invariant 5, in the argv itself."""
    argv = [
        "docker", "run",
        "--name", name,
        *_label_argv(labels),
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


def _docker(*argv: str) -> None:
    """Best effort. A wedged daemon is not a reason to lose the run's result."""
    try:
        subprocess.run(["docker", *argv], stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (subprocess.TimeoutExpired, OSError):
        pass


def force_stop(name: str) -> None:
    """Stop the container, and deliberately do not remove it.

    Stopping is the safety property: killing the docker client does not kill the
    container, which is measured behaviour rather than theory, so nothing may be left
    executing. Removing is a different thing, and doing it here is what made an untrusted
    sample runnable twice.

    The container is the only durable evidence that an attempt already executed. A
    checkpoint is written after a node returns, so a crash between removing the container
    and committing that checkpoint leaves a resumed run with no state and no container,
    and it runs the sample again. Keeping the exited container until the result is
    durable closes that window: `remove_container` is called by a later node, once the
    result has been written down.

    `kill` rather than `stop`, because `stop` sends SIGTERM and waits, and a sample that
    ignores SIGTERM would hold this up for the grace period. Nothing here needs the
    container to shut down tidily.
    """
    _docker("kill", name)


def remove_container(name: str) -> None:
    """Remove a container once nothing needs it as evidence any more."""
    _docker("rm", "--force", name)


def container_exists(name: str) -> bool:
    """Whether a container with this name is still on the host, in any state.

    The reconciliation primitive. `run` stops its container and leaves it, and removal
    happens only once the run's result is durable, so a container still here either
    belongs to a run in progress or was left by a process that died. Either way the
    sample already ran under that name.
    """
    try:
        finished = subprocess.run(["docker", "ps", "-a", "--filter", f"name=^{name}$",
                                   "--format", "{{.Names}}"],
                                  capture_output=True, text=True, timeout=20)
    except (subprocess.TimeoutExpired, OSError):
        # No answer is not the same as "no container". Reporting False here would let a
        # resumed run execute the sample again on the strength of a failed lookup, so
        # this fails closed: assume the evidence exists.
        return True
    return name in finished.stdout.split()


def _ours(kind: str, list_argv: list[str]) -> list[tuple[str, str, int]]:
    """Every object of this kind we made, as (id, run_id, started).

    Two calls rather than one, and no date parsing anywhere. `docker ps --format` can
    print a container's labels but `docker images --format` cannot, so a single listing
    that worked for both does not exist; `inspect` answers for both in the same shape.
    """
    # Guarded like every other docker call in this file, and this one was not. The sweep
    # runs at the start of every run, so an unguarded `subprocess.run` here made four
    # unit tests need the docker binary: the suite the README calls "needs no daemon"
    # died with FileNotFoundError on a machine without docker on PATH.
    try:
        listed = subprocess.run(list_argv, capture_output=True, text=True, timeout=30,
                                check=False)
    except (subprocess.TimeoutExpired, OSError):
        return []
    ids = listed.stdout.split()
    if not ids:
        return []
    try:
        shown = subprocess.run(
            ["docker", kind, "inspect", *ids, "--format",
             '{{.Id}}\t{{index .Config.Labels "' + RUN_LABEL + '"}}\t'
             '{{index .Config.Labels "' + STARTED_LABEL + '"}}'],
            capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, OSError):
        return []
    found: list[tuple[str, str, int]] = []
    for line in shown.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 3 or not parts[1]:
            continue
        try:
            found.append((parts[0], parts[1], int(parts[2])))
        except ValueError:
            # A label we wrote that is not a number is not ours to reason about, and
            # guessing an age for it is how a sweep deletes something it should not.
            continue
    return found


def sweep(keep: str = "", older_than: float = 3600.0) -> list[str]:
    """Remove images left behind by runs that did not finish.

    Images, not containers. See the comment below: a container is the proof that an
    attempt already executed an untrusted sample, and removing it is what would let a
    resumed run execute that sample again.

    Two guards, and both are about other people's work rather than tidiness.

    Ownership: anything labelled with `keep` belongs to the run asking for the sweep and
    is skipped, because a run must not delete the image it is about to run.

    Age: anything younger than `older_than` is skipped, because a second envforge may be
    running right now and its objects are labelled exactly like ours. There is no way to
    ask "is that process alive", so age stands in for it, and an hour is far longer than
    a run and far shorter than a machine fills up.

    Returns what it removed, so a caller can say so rather than sweeping silently.
    """
    cutoff = time.time() - older_than
    removed = []
    # Images only. Containers are deliberately left, and that is the resolution of a
    # contradiction rather than an oversight: a crashed attempt's container is the
    # durable evidence that its sample already ran, so sweeping it lets a run resumed an
    # hour later execute the sample a second time and report an ordinary verdict. A
    # confident wrong verdict is the worst thing this tool can produce, and an exited
    # container costs kilobytes. Images are what fill a disk and were never evidence.
    for kind, listing, drop in (
        ("image", ["docker", "image", "ls", "--quiet", "--filter",
                   f"label={RUN_LABEL}"], remove_image),
    ):
        for object_id, run_id, started in _ours(kind, listing):
            if run_id == keep or started > cutoff:
                continue
            drop(object_id)
            removed.append(f"{kind} {object_id[:12]} from run {run_id[:8]}")
    return removed


def remove_image(tag: str) -> None:
    """Remove a tag, and with it the image and any layers nothing else references.

    It does not touch the build cache, which is a separate store that only
    `docker builder prune` clears. Reading this as "cleanup" is what would let someone
    conclude a finished run leaves nothing behind on the machine.

    Best effort and bounded. This was the only docker call in the file with no timeout,
    and it runs once per built tag inside a `finally`, so a wedged daemon hung the
    program forever after the run had already produced its answer.
    """
    _docker("image", "rm", "--force", tag)


def daemon_error() -> str | None:
    """Why Docker cannot be used, or None if it can.

    One cheap call before the loop starts. Without it a stopped daemon looked exactly
    like three failed builds: the agent spent three paid model calls asking for repairs
    to a Dockerfile that was already correct, then reported the script as having run and
    failed. The daemon being down is neither the model's fault nor a finding about the
    script, and it is knowable in advance for the price of one subprocess.
    """
    try:
        finished = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, timeout=20, check=False)
    except FileNotFoundError:
        return "the docker command is not on PATH"
    except OSError as exc:
        return f"could not run docker: {exc}"
    except subprocess.TimeoutExpired:
        return "docker did not answer within 20 seconds"
    if finished.returncode != 0:
        detail = finished.stderr.decode("utf-8", "replace").strip().splitlines()
        return detail[0] if detail else "the docker daemon did not answer"
    return None


class DockerSandbox:
    """Docker CLI on the host. No socket is mounted anywhere (invariant 7)."""

    def __init__(self, limits: Limits | None = None) -> None:
        self.limits = limits or Limits()
        # Every tag this sandbox built, so whoever started the run can remove them.
        # Cleanup is the caller's call rather than automatic: layer reuse is what makes
        # a second run take seconds, and a loop that deleted its own base layers would
        # pay full price every attempt.
        self.built_tags: list[str] = []

    def build(self, dockerfile: str, files: Mapping[str, str], tag: str,
              labels: Mapping[str, str] = MappingProxyType({})) -> BuildResult:
        """Every file lands in the context root under its own name, which is the name a
        COPY must use.

        Contents rather than paths, so the bytes the model reviewed and the bytes the
        container runs are the same bytes. Copying from disk here would have been a
        second read, and a file can change between two reads.
        """
        started = time.monotonic()
        # Recorded before the attempt, not after: a build that fails part-way still
        # leaves layers and an image id behind, so the tag is what has to be cleaned up
        # whether or not the build succeeded.
        self.built_tags.append(tag)
        with tempfile.TemporaryDirectory(prefix="envforge-ctx-") as tmp:
            context = Path(tmp)
            (context / "Dockerfile").write_text(dockerfile, encoding="utf-8")
            for name, content in files.items():
                # A precondition, not a second copy of the rule. The workspace owns what
                # a legal filename is, decides it once at ingestion and says so to a
                # person. This states only what `build` requires in order to be correct,
                # because it writes these names into a directory and then hands that
                # directory to the daemon. If it fires, nobody typed anything wrong and
                # our own code broke a contract, which is what SandboxError means here.
                if not name or "/" in name or "\\" in name or name in (".", ".."):
                    raise SandboxError(f"caller passed {name!r}, which is not a bare "
                                       "filename. The workspace should have refused it")
                if name.lower() in RESERVED_NAMES:
                    # Found 2026-08-25, verified against the daemon. This loop runs after
                    # the gated Dockerfile is written, so a context file called Dockerfile
                    # overwrote it and the container ran instructions the gate never saw.
                    # Not a directory escape: a complete bypass of the only check there is.
                    raise SandboxError(f"caller passed {name!r}, which the build itself "
                                       "interprets. It would replace the gated Dockerfile")
                (context / name).write_text(content, encoding="utf-8")
            code, out, err, timed_out = _capture(
                build_argv(tag, context, labels), self.limits.build_timeout
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

    def run(self, image: str, args: Sequence[str] = (), name: str | None = None,
            labels: Mapping[str, str] = MappingProxyType({})) -> RunResult:
        # A caller may supply the name so it can find this container again after a
        # crash. This method stops the container and deliberately leaves it in place,
        # so the container is durable evidence that the sample already ran, and
        # `remove_container` is called later by whoever knows the result is safe.
        # Random by default, because a caller that does not care must not invent one.
        name = name or f"envforge-{uuid.uuid4().hex[:12]}"
        started = time.monotonic()
        with tempfile.TemporaryDirectory(prefix="envforge-cid-") as tmp:
            # docker refuses to overwrite a cidfile, so the path must not exist yet.
            cidfile = Path(tmp) / "cid"
            start_error = ""
            try:
                code, out, err, timed_out = _capture(
                    run_argv(image, name, cidfile, self.limits, args, labels),
                    self.limits.run_timeout,
                )
            finally:
                # Order matters and stopping must still be unconditional: read the
                # witness first, then stop whatever was running while we read it. The
                # container is left in place on purpose; see `force_stop`.
                try:
                    start_error = _start_error(name)
                finally:
                    force_stop(name)
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
        """See the module-level `remove_image`: a tag, not the build cache."""
        remove_image(tag)
