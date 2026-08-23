# Status

Updated 2026-08-23, end of sitting 4.

## Where we are
Sitting 4 of 11 complete: `envforge/agent.py` and `tests/test_agent.py`.
68 tests, 58 needing neither Docker nor an API key, 10 against the real daemon.

## What sitting 4 produced
The plain repair loop. It defines `write_dockerfile` against the schema `Tool` already
enforces, asks, gates, builds, runs, and decides one thing over and over: could a
rewritten Dockerfile fix this. It yields `Event`s rather than printing, and the final
event carries an `Outcome`, so sitting 5's graph engine and sitting 8's trace attach to
the same seam. There is still no `__main__`, so nothing runs from a shell.

Repairable, and each one spends an attempt: a gate rejection, a failed build, exit 126 or
127, and a reply that was refused-adjacent rather than refused, meaning `InvalidArguments`,
`Truncated`, or no tool call at all.

Terminal, because the script ran and what it did is sitting 7's problem: exit 0, exit 1,
exit 137, a 125 that came with a cidfile, and a timeout. A timeout is not a repair
candidate. The run happened.

Four decisions worth naming:

1. `gate` is a required constructor argument with no default. Every Dockerfile reaching
   the daemon was written by a model that just read untrusted text, so a loop that can be
   constructed without a gate is a loop that can build one unchecked. Sitting 6 fills in
   the real allowlist; the tests pass an explicit stub.
2. Repair is decided by whether the container started, not by its exit code. `RunResult`
   carries `start_error`, read from the daemon before the force-remove. This closes the
   gap sitting 2 left open, and closes it better than the 126/127 test it replaced: those
   codes can be produced by a script that wants to look like a broken image, and a script
   that can choose an exit code has already started, so the field is empty for it.
3. The fallback Dockerfile goes through the same gate. One path from a Dockerfile string
   to the daemon, whoever wrote it. If our own fallback fails our own gate, the loop stops
   and says so rather than asking the model again.
4. The script text is bounded before it enters the prompt, for the same reason container
   output is bounded in the sandbox. Both are attacker-controlled text on the way to a
   model.

Sitting 3 of 11 complete: `envforge/llm.py` and `tests/test_llm.py`.
48 tests, 39 without Docker or any API key, 9 against the real daemon.
The repository is public, and `main` rejects direct pushes: the ruleset requires a pull
request and a green `test` check, verified on 2026-08-23 by a push that was refused.

## What sitting 3 produced
`LLM` Protocol, `AnthropicLLM` on the native SDK, `OpenAICompatLLM` covering OpenAI and
Groq through `base_url`, `Tool`, `Call`, `validate`, and `make_llm("provider:model")`.
Four failure types, because the loop has to tell them apart: `InvalidArguments` is
repairable, `Refused` is not, `Truncated` means the ceiling was hit, and a bare `LLMError`
means the response had no tool call at all.

`build_request` is a pure function on each provider, the same move as sitting 2's
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

Sitting 2 of 11 complete: `envforge/sandbox.py` and `tests/test_sandbox.py`.
24 tests, 15 without Docker and 9 against the real daemon, all passing.
A `.venv` with pytest exists in the repo directory and is gitignored.
The sitting's judgment question is answered and recorded: why the memory test asserts
both exit 137 and the absence of `MemoryError` in stderr.

## What sitting 2 produced
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
code, trimming docstrings that carried the reasoning and opening three sittings in a row
with a note about length instead of the work.

What replaces it is not countable. A file whose responsibility cannot be stated in one
sentence is doing two things and wants splitting, and a module that wants a subdirectory
has outgrown the project.

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
argv and have tests asserting that, which is a weaker claim than sitting 1's five flags.

## CI, added 2026-08-22
`.github/workflows/tests.yml` runs both suites on push to `main` and on pull requests.
The docker half is the reason it exists: GitHub's Linux runners ship a real daemon, so
the hardening flags stay checked on a machine that is not Ori's laptop. Steps are named
separately so a red build says which half broke.

Two consequences for later sittings. The suite must never need an API key, so the fake
LLM planned for sitting 3 is a CI requirement and not a convenience. And this is signal,
not enforcement: turning it into an actual gate needs branch protection on `main`, a
repository setting nobody has flipped yet.

## Refusal policy, decided 2026-08-23
The input is a script nobody trusts and the model has cybersecurity safeguards, so a
refusal is an expected outcome rather than an edge case. Decided with Ori:

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

## Next
Sitting 5: the LangGraph port. The same loop as graph nodes behind one interface, with the
plain engine kept so the two can be compared rather than replaced.

Default provider spec decided 2026-08-22: `anthropic:claude-sonnet-5`. Native SDK with
strict tool use, which is the only path that gives a grammar guarantee for Claude, and
cheap enough that the capped repair loop can run its attempts without the cost being the
reason we stop. `ANTHROPIC_API_KEY` in the environment, never in a file.
Nothing blocks sitting 3 now.

## Sittings
1 cage (done) | 2 sandbox (done) | 3 llm (next) | 4 plain loop | 5 LangGraph port |
6 gate | 7 verdict | 8 trace | 9 prompts | 10 failures and cost | 11 packaging
