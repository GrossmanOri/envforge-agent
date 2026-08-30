# envforge-agent

Takes a script you do not trust, has an LLM write a Dockerfile for it, builds and runs it
in a hardened container, repairs the Dockerfile when the run fails, and reports what the
script tried to do while it ran. Python and Bash, one script at a time, plus the few
sibling files that language declares, such as a `requirements.txt` found beside it. A
language it does not handle is refused before the model is consulted.

Built in the open, one piece at a time. This README separates what runs today from what is
still design, so nothing here has to be taken on trust.

## What runs today

**The sandbox**, `envforge/sandbox.py`, which holds the untrusted script. It builds an
image and runs the container, returning the exit code with bounded output. The run has no
network, a memory cap with swap pinned to the same value, a pids cap, a cpu cap, a
read-only root with a tmpfs, every capability dropped, `no-new-privileges`, and a non-root
user. No Docker socket is mounted anywhere, and the agent is never itself containerised,
because that would put the socket back.

Every container is named before it spawns and force-removed in a `finally`. Killing the
docker client does not kill the container, which is measured behaviour rather than a
precaution. Two ambiguous outcomes are separated by evidence rather than by an exit code:
`--cidfile` tells our own malformed command apart from a script that exited 125 on purpose,
and the daemon's `State.Error` tells an image that could not start its command apart from a
script that chose 126 to look like one.

**The model layer**, `envforge/llm.py`. One forced, schema-constrained tool call to
Anthropic, OpenAI or Groq, so the Dockerfile arrives as validated arguments rather than
prose we have to extract.

**The gate**, `envforge/gate.py`. An allowlist of six instructions and nothing else, run
before every build including the fallback we write ourselves. `RUN` is exec form, so its
arguments are checked one at a time rather than matched against a string prefix. What it
does not do is stated plainly: `pip install <name>` runs that package's own code at build
time, and no instruction allowlist can prevent that, because installing packages is the
product.

**The repair loop**, `envforge/agent.py`. It asks, gates, builds, runs, and decides one
thing repeatedly: could a different Dockerfile have changed this. A failed build could. A
script exiting 1 could not, because the script ran, and watching it run is the point.

**The workspace**, `envforge/workspace.py`. The only code here that handles a path.
Symlinks are resolved and then checked for containment, in that order.

**The event vocabulary**, `envforge/events.py`. A closed set of the kinds a run may
report, and for each string in them, which of us wrote it: this program, the untrusted
input, the model, or the container. An engine that invents a kind fails when it builds the
event rather than when someone later tries to read it. Nothing renders these yet, so the
labels are recorded and not consumed.

**The token budget**, `envforge/budget.py`. What the model may be paid for one run,
counted in tokens rather than in turns, because every turn resends the ones before it. It
sits alongside the attempt cap rather than replacing it, since an attempt costs a build and
a container run, which tokens do not measure. Every reply is charged, including the
refusals and truncations we cannot use, and a truncated reply is the most expensive kind
there is.

**The command line**, `envforge/__main__.py`. `python -m envforge script.py` takes a script
and prints what it did, and `python -m envforge --check` verifies the API key without
spending a call. The exit code separates what a caller must not confuse. `0` is a clean run and `1` is
the script running and exiting nonzero, which is a finding about the sample. `3`, `4`, `5`
and `6` are this tool being unable to do its job: no credentials or the model unreachable,
the token budget spent, Docker unavailable, or no Dockerfile that would build. Those say nothing about the
script and a caller should fix its setup rather than read a verdict into them.

285 tests, 272 of which need neither Docker nor an API key. Both suites run on every push
and every pull request.

## What is designed and not built

The verdict and the trace. The command line reports what the script did and what it cost;
nothing yet decides what that behaviour *means*, which is the verdict's job.

The model has no tools. It reads the script once and writes a Dockerfile, so it cannot look
anything up before deciding. That makes this a workflow with a feedback loop rather than an
agent, which is stated here rather than glossed, and giving the model real tool choice is
the next substantial piece of work.

`ARCHITECTURE.md` holds the design. `STATUS.md` says where the build actually is, including
which hardening flags are asserted in the argv but not yet verified by observation.

## Tests

    python -m pytest              the suite that needs no daemon
    python -m pytest -m docker    the suite that builds real images
