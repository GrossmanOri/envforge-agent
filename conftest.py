"""Skip the Docker-marked tests when there is no daemon to talk to.

README and CLAUDE.md both describe `python -m pytest` as the suite that needs no daemon,
and it was not: with no `docker` on PATH the thirteen marked tests errored rather than
skipping, so the documented command failed on a machine the documentation says it works
on. A review caught the sentence; making the sentence true is better than rewording it.

Skipped rather than deselected, so the count still says they exist and were not run. CI
keeps running them explicitly with `-m docker` against a real daemon, so nothing here
lets a broken sandbox through unnoticed.
"""

import shutil
import subprocess

import pytest


def _daemon_is_up() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        finished = subprocess.run(["docker", "version"], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return finished.returncode == 0


def pytest_collection_modifyitems(config, items):
    if _daemon_is_up():
        return
    skip = pytest.mark.skip(reason="no Docker daemon; run these with -m docker where one exists")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)
