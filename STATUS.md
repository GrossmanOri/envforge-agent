# Status

Updated 2026-08-22, end of sitting 1.

## Where we are
Sitting 1 of 11 complete: the cage by hand, five of seven flags verified by hand.
No project code yet, by design.

## What sitting 1 produced
Ori ran every mode of `cage.py` bare and caged. Five flags observed firing:
`--network none` (Errno 101, instant, no route rather than a dropped packet),
`--read-only` (Errno 30), `--pids-limit 64` (Errno 11, fork bomb dead in under a second),
`--user 65534:65534` (uid 0 becomes 65534), and after the fix `--memory 128m` (exit 137,
kernel kill, no traceback). `--cpus` and `no-new-privileges` remain untested.

Two findings, both written up in the private notes kept outside this repo:

1. The first OOM test passed without testing anything. `bytearray(512MB)` against
   `--memory 128m` exited 0, because a cgroup counts resident pages, not requested bytes,
   and untouched mmap pages never fault in. Touching one byte per 4096 gives exit 137 on
   identical code. `cage.py` fixed to touch every page.
2. A container outlives its client. Killing the `docker run` process left `cagetest` in
   `docker ps` as Up. The CLI is only a client; the container is a child of the daemon.

Exit codes now carry meaning: 137 is a kernel kill, 1 is the script raising, 125 is the
docker CLI rejecting our own command.

Ori's flag answer: keep `--network none`, `--memory`, `--user`. Dropping `--pids-limit`
is the contested one, and dropping `no-new-privileges` partly undoes the non-root user.

## Decisions this fixes for sitting 2
Every container gets `--name` and an explicit `docker rm -f` in a `finally`, because a
`subprocess` timeout kills the client only. The sandbox must distinguish 125 from 137.
The docker command is built as a Python list, never a string: `$CAGE` unquoted in zsh
arrived as one flag and produced exit 125.

## Decided after sitting 1, before any code
The full layout, the LLM provider layer, and the Dockerfile shape are settled and written
into CLAUDE.md and ARCHITECTURE.md (ADR-006 and ADR-007). Headlines: flat `envforge/`
package, no `src/`; one `llm.py` with a Protocol plus two provider classes, no LiteLLM
and no LangChain model classes; the Dockerfile arrives as a forced strict tool call with
one string field; the gate bans line continuations. The line target moved to about 650
and is a smell detector rather than a limit.

Open question for Ori, not blocking sitting 2: nothing. Sitting 3 will need him to
confirm which provider spec is the default on the CLI.

## Next
Sitting 2: `envforge/sandbox.py` and `tests/test_sandbox.py`. Sandbox protocol,
DockerSandbox with build_image and run_container returning dataclasses, bounded output,
named container force-removed in `finally`, real-Docker tests marked `docker`, and the
invariant test that asserts the hardening flags in ARCHITECTURE.md appear in the argv the
code actually builds.

## Sittings
1 cage (done) | 2 sandbox (next) | 3 llm | 4 plain loop | 5 LangGraph port | 6 gate |
7 verdict | 8 trace | 9 prompts | 10 failures and cost | 11 packaging
