"""The workspace, which is the only code in the project that handles a path.

Most of these are about what happens at ingestion, because that is the only moment
a path exists. After gather() returns there is nothing left to attack.
"""

from pathlib import Path

import pytest

from envforge.agent import LANGUAGES
from envforge.workspace import FILE_LIMIT, Files, WorkspaceError, gather

PYTHON_SIBLINGS = LANGUAGES["python"].siblings


@pytest.fixture
def project(tmp_path):
    (tmp_path / "s.py").write_text("import requests\n")
    (tmp_path / "requirements.txt").write_text("requests==2.32.0\n")
    (tmp_path / "notes.md").write_text("not a manifest\n")
    return tmp_path


# --- what it gathers ------------------------------------------------------------------

def test_it_gathers_the_script_and_the_siblings_that_exist(project):
    workspace = gather(project / "s.py", PYTHON_SIBLINGS)
    assert workspace.script == "s.py"
    assert workspace.names() == {"s.py", "requirements.txt"}
    assert workspace.read("requirements.txt") == "requests==2.32.0\n"


def test_a_file_that_is_not_on_the_menu_is_not_gathered(project):
    """The menu is a fixed tuple, not a pattern and not a caller-supplied path, so
    there is nothing for a traversal to traverse."""
    assert "notes.md" not in gather(project / "s.py", PYTHON_SIBLINGS).names()


def test_a_language_with_no_manifest_gathers_only_the_script(tmp_path):
    (tmp_path / "s.sh").write_text("echo hi\n")
    (tmp_path / "requirements.txt").write_text("requests\n")
    workspace = gather(tmp_path / "s.sh", LANGUAGES["bash"].siblings)
    assert workspace.names() == {"s.sh"}


def test_reading_something_that_was_not_gathered_is_refused(project):
    workspace = gather(project / "s.py", PYTHON_SIBLINGS)
    with pytest.raises(WorkspaceError, match="not in this workspace"):
        workspace.read("notes.md")


# --- symlinks, which is why ingestion exists at all --------------------------------------

def test_a_sibling_symlinked_out_of_the_directory_is_refused(project, tmp_path):
    """The case the whole design is for. A prefix check on the joined path would
    pass this, because the joined path is inside the directory. Only the resolved
    target is outside it."""
    secret = tmp_path.parent / "id_ed25519"
    secret.write_text("PRIVATE KEY\n")
    (project / "pyproject.toml").symlink_to(secret)
    with pytest.raises(WorkspaceError, match="outside"):
        gather(project / "s.py", PYTHON_SIBLINGS)


def test_a_sibling_symlinked_inside_the_directory_is_fine(project):
    """Resolve and check where it landed, rather than refusing symlinks outright.
    A link that stays inside the root is not an escape."""
    (project / "real.txt").write_text("requests\n")
    (project / "pyproject.toml").symlink_to(project / "real.txt")
    assert gather(project / "s.py", PYTHON_SIBLINGS).read("pyproject.toml") == "requests\n"


def test_a_symlinked_script_is_followed_and_takes_its_target_directory_with_it(tmp_path):
    """The script is different from its siblings. The user named it, so following
    their link is doing what they asked. The siblings were discovered rather than
    named, which is why they may not leave the root, and the root is where the
    script actually lives rather than where the link sits."""
    real = tmp_path / "real"; real.mkdir()
    (real / "s.py").write_text("print(1)\n")
    (real / "requirements.txt").write_text("rich\n")
    elsewhere = tmp_path / "links"; elsewhere.mkdir()
    (elsewhere / "s.py").symlink_to(real / "s.py")

    workspace = gather(elsewhere / "s.py", PYTHON_SIBLINGS)
    assert workspace.names() == {"s.py", "requirements.txt"}
    assert workspace.read("requirements.txt") == "rich\n"


# --- files that are not files, or are too much of one ------------------------------------

def test_a_directory_wearing_a_manifest_name_is_refused(project):
    (project / "pyproject.toml").mkdir()
    with pytest.raises(WorkspaceError, match="not a regular file"):
        gather(project / "s.py", PYTHON_SIBLINGS)


def test_a_sibling_larger_than_the_limit_is_refused(project):
    """It is heading for a prompt. A dependency manifest is a few hundred bytes."""
    (project / "pyproject.toml").write_text("#" * (FILE_LIMIT + 1))
    with pytest.raises(WorkspaceError, match="larger than"):
        gather(project / "s.py", PYTHON_SIBLINGS)


def test_a_sibling_that_is_not_text_is_refused(project):
    (project / "pyproject.toml").write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(WorkspaceError, match="not valid UTF-8"):
        gather(project / "s.py", PYTHON_SIBLINGS)


def test_a_missing_script_is_refused(tmp_path):
    with pytest.raises(WorkspaceError, match="is not a file"):
        gather(tmp_path / "nope.py", PYTHON_SIBLINGS)


# --- the shape the rest of the project will depend on --------------------------------------

def test_files_carries_contents_rather_than_locations(project):
    """What makes an upload or a Kubernetes bundle a drop-in replacement later:
    there is no directory in here to point at."""
    workspace = gather(project / "s.py", PYTHON_SIBLINGS)
    assert isinstance(workspace, Files)
    assert all(isinstance(v, str) for v in workspace.contents.values())
    assert not any(isinstance(v, Path) for v in vars(workspace).values())
