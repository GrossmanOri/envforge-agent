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

**The command line**, `envforge/__main__.py`. `python -m envforge script.py` takes a script
and prints what it did, and `python -m envforge --check` verifies the API key without
spending a call. The exit code separates what a caller must not confuse. `0` is a clean run and `1` is
the script running and exiting nonzero, which is a finding about the sample. `3` through `7` are this
tool being unable to do its job: no credentials or the model unreachable, Docker
unavailable, no Dockerfile that would build, or the provider refusing the request we sent,
which is our bug rather than theirs. Those say nothing about the
script and a caller should fix its setup rather than read a verdict into them.

**The looking tools**, in `envforge/agent.py` and `envforge/llm.py`. A script is bounded
to 8,192 characters before it reaches a prompt, because it is untrusted text and it is
resent on every repair. Most real scripts are longer than that, so the model was deciding
what to install from the first and last quarter of a file with the middle replaced by a
marker, and a dependency can be anywhere in that middle: an import inside a function, a
subprocess call to a command line tool.

So the model can now ask. `search_script` finds a literal string and returns the offset of
every occurrence, `read_script` returns a region by character offset, and between them the
model chooses whether to look, where, and when it has seen enough to write. Which region
matters differs per script, which is what makes this a decision no deterministic rule
could have made for it.

Search returns every offset because the first version returned the first five matches with
their surrounding text, and that is useless for the query a model actually asks. Searching
a Python file for `import` matches the import block at the top, and the top is the half the
model was already shown. The first live run spent a look on a search that told it nothing,
then read the middle in slices and found the answer on the last of its four looks. Offsets
are cheap to return in full and are exactly what the other tool takes.

Four looks per attempt, 2,048 characters each. That cap is a security control and not a
budget: a model that asks for region after region has reassembled the whole file one slice
at a time. Every slice is labelled as the sample's own words on the way back, because a
tool result arrives in the one position a model is trained to trust.

The bound that follows from those numbers is written out in full as invariant 24, and it
is written in full because the short version of it was wrong. Bounding the direct path is
not the same as bounding the sample: the model's previous Dockerfile is replayed into every
repair and survives across attempts, so a model that copied what it read into comment lines
carried its slices forward and collected a fresh look budget on top. A review found it by
building the attack rather than by reading the rule, and it put 25,326 characters of a
40,000 character sample into a single prompt before the previous Dockerfile was bounded too.

What the model does not get is any influence over the loop. It chooses what to read. It
does not choose whether an attempt is spent, whether the gate runs, or whether anything is
built.

**Two engines**, `envforge/agent.py` and `envforge/graph.py`, chosen with `--engine`. The
plain one is a `while` loop and the LangGraph one is three nodes. Neither implements the
machine. Both drive the same three steps, and a step yields its events as they happen and
returns the name of the next step, so the only thing either engine decides is what to call
next.

A run takes two cycles through those steps: `author` to `look` and back is the model
reading the script, and `execute` to `author` is the repair loop. That is a property of
what the steps return rather than of the graph's edges, which are uniform, and it is
asserted by watching a run rather than by reading the edge list.

The seam is checked rather than claimed. A contract test runs every scenario through both
engines and compares the event kinds in order, the message of every event, and every field
of the verdict except the run's uuid. Replacing the graph's routing with a straight edge to
the exit reddens most of that file. A port that wrapped the existing loop inside one node
would have passed everything else in this repository.

No count here on purpose. It was written as twelve, and one review later it was seventeen,
which is the fourth number in this work that went stale because nothing checked it.

366 tests, 353 of which need neither Docker nor an API key. The rest skip
automatically when no daemon is present. Both suites run on every push
and every pull request.

## What is designed and not built

The verdict and the trace. The command line reports what the script did and what it cost;
nothing yet decides what that behaviour *means*, which is the verdict's job.

`ARCHITECTURE.md` holds the design. `STATUS.md` says where the build actually is, including
which hardening flags are asserted in the argv but not yet verified by observation.

## Tests

    python -m pytest              the suite that needs no daemon
    python -m pytest -m docker    the suite that builds real images
