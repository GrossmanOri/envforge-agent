# envforge-agent

Takes a script you do not trust, has an LLM write a Dockerfile for it, builds and runs it
in a hardened container, repairs the Dockerfile when the run fails, and reports what the
script tried to do while it ran. Python and Bash, one file at a time.

Built in the open, one piece at a time. This README separates what runs today from what is
still design, so nothing here has to be taken on trust.

## What runs today

`envforge/sandbox.py`, the part that holds the untrusted script.

It builds an image from a Dockerfile and a script, runs the container, and returns the exit
code with bounded output. The run has no network, a memory cap with swap pinned to the same
value, a pids cap, a cpu cap, a read-only root with a tmpfs, every capability dropped,
`no-new-privileges`, and a non-root user. No Docker socket is mounted anywhere, and the
agent is never itself containerised, because that would put the socket back.

Every container is named before it spawns and force-removed in a `finally`. Killing the
docker client does not kill the container, which is measured behaviour rather than a
precaution.

Exit code 125 is separated from a script that exits 125 on purpose, using `--cidfile`:
docker writes that file only once the container exists, so its absence means our own
command was malformed and its presence means the script behaved that way. Without the
split, a hostile script can spend the agent's repair budget on a Dockerfile that was never
broken.

24 tests, 9 of which build real images and run real containers. Both suites run on every
push and every pull request.

## What is designed and not built

The LLM layer, the Dockerfile gate, the repair loop, the verdict, and the command line
entry point. There is no `__main__.py`, so there is nothing to run from a shell yet.

`ARCHITECTURE.md` holds the design. `STATUS.md` says where the build actually is, including
which hardening flags are asserted in the argv but not yet verified by observation.

## Tests

    python -m pytest              the suite that needs no daemon
    python -m pytest -m docker    the suite that builds real images
