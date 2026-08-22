# Status

Updated 2026-08-22, end of sitting 2.

## Where we are
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

## Known gaps
126 and 127 are not distinguished from other script exit codes. Both mean the image's
command cannot be executed, which is the model's fault and repairable, but nothing in the
code treats them specially yet.

Timeout returns `exit_code=None` with `timed_out=True`. Nothing yet decides what a
timeout means for the verdict.

`--cpus` and `no-new-privileges` are still unverified by observation. Both appear in the
argv and have tests asserting that, which is a weaker claim than sitting 1's five flags.

## Next
Sitting 3: `envforge/llm.py` and its tests. One call, forced strict tool use, wire JSON
kept for the trace module, a fake client in tests. Load the claude-api skill before
writing. Needs an API key.

Default provider spec decided 2026-08-22: `anthropic:claude-sonnet-5`. Native SDK with
strict tool use, which is the only path that gives a grammar guarantee for Claude, and
cheap enough that the capped repair loop can run its attempts without the cost being the
reason we stop. `ANTHROPIC_API_KEY` in the environment, never in a file.
Nothing blocks sitting 3 now.

## Sittings
1 cage (done) | 2 sandbox (done) | 3 llm (next) | 4 plain loop | 5 LangGraph port |
6 gate | 7 verdict | 8 trace | 9 prompts | 10 failures and cost | 11 packaging
