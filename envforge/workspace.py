"""The files one run is allowed to see, and the only place a path is ever handled.

Tools and the sandbox ask for names and contents. Neither is given a path, so
there is no path for anything to manipulate. Everything that could go wrong with
a filesystem happens once, here, at ingestion, rather than on every read.

That single choke point is the point. A `requirements.txt` sitting next to an
untrusted script was not chosen by anyone: it was discovered. Resolving it once
and checking where it actually landed is a rule that holds forever. Checking a
path at each use is a rule that holds until somebody adds a use.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

# Per file. A dependency manifest is a few hundred bytes; anything at this size is
# not one, and it is heading for a prompt.
FILE_LIMIT = 64 * 1024


class WorkspaceError(Exception):
    """The files could not be gathered. Never raised for anything inside them."""


class Workspace(Protocol):
    """Names and contents. Deliberately no path, and deliberately no listing of
    anything that was not gathered up front."""

    @property
    def script(self) -> str:
        """The filename of the script this run is about."""

    def names(self) -> frozenset[str]: ...

    def read(self, name: str) -> str: ...


@dataclass(frozen=True)
class Files:
    """A workspace already gathered, holding contents rather than locations.

    Contents rather than locations is what makes the later versions of this drop
    in without touching a caller: an upload has no directory, and a build running
    as a Kubernetes Job has no host filesystem to point at.
    """

    script: str
    contents: dict[str, str]

    def names(self) -> frozenset[str]:
        return frozenset(self.contents)

    def read(self, name: str) -> str:
        if name not in self.contents:
            raise WorkspaceError(f"{name!r} is not in this workspace")
        return self.contents[name]


def _read(path: Path, root: Path, name: str) -> str:
    """One file, or the reason it is not usable.

    `resolve()` before the containment check, never after: the check has to be
    made against where the file actually is. A sibling that is a symlink to
    ~/.ssh/id_ed25519 resolves outside the root and is refused here, which is a
    rule that cannot be forgotten later because there is no later.
    """
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise WorkspaceError(f"{name!r} resolves to {resolved}, outside {root}")
    if not resolved.is_file():
        raise WorkspaceError(f"{name!r} is not a regular file")
    if resolved.stat().st_size > FILE_LIMIT:
        raise WorkspaceError(f"{name!r} is larger than {FILE_LIMIT} bytes")
    try:
        return resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise WorkspaceError(f"{name!r} is not valid UTF-8") from exc


def gather(script: Path, siblings: tuple[str, ...] = ()) -> Files:
    """Collect the script and whichever of `siblings` exist beside it.

    The script is resolved first and its own directory becomes the root, so a
    symlinked script is followed. The user named that file, so following it is
    doing what they asked. The siblings were not named by anyone, they were found,
    which is why they may not resolve anywhere except inside that root.
    """
    resolved = script.resolve()
    if not resolved.is_file():
        raise WorkspaceError(f"{script} is not a file")
    if any(sep in name for name in siblings for sep in "/\\"):
        # The menu is ours, so this is a mistake in our own table rather than an attack,
        # but the sandbox writes these names into a directory and a separator would
        # escape it. Fail where the mistake is, not where it lands.
        raise WorkspaceError("sibling names must be bare filenames")
    root = resolved.parent
    contents = {resolved.name: _read(resolved, root, resolved.name)}
    for name in siblings:
        candidate = root / name
        if candidate.exists():
            contents[name] = _read(candidate, root, name)
    return Files(script=resolved.name, contents=contents)
