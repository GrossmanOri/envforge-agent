# Status

Updated 2026-08-21, end of setup, sitting 1 in progress.

## Where we are
Sitting 1 of 11: the cage by hand. Repo created and pushed. No project code yet.

## What the last sitting produced
Setup: README, .gitignore, CLAUDE.md, STATUS.md. Private notes started in
`~/Projects/envforge/private/agent/` (interview-qa.md with three entries, linkedin.md
empty). Sitting 1 files written to `~/Projects/envforge/private/agent/sitting-01-cage/`:
`cage.py` (misbehaves on command: net, mem, fork, write, sleep) and a three-line
Dockerfile.

## Waiting on Ori
Run each mode bare and caged with the seven flags, run the client-timeout experiment
(`timeout 5 docker run --name cagetest cage sleep; docker ps`), and answer: keep only
three of the seven flags, which three and what does each dropped one no longer stop.

## Next
Sitting 2: `envforge/sandbox.py` and `tests/test_sandbox.py`. Sandbox protocol,
DockerSandbox with build_image and run_container returning dataclasses, bounded output,
named container killed on timeout (shape decided by the sitting 1 timeout result),
real-Docker tests marked `docker`.

## Sittings
1 cage by hand (now) | 2 sandbox | 3 llm | 4 plain loop | 5 LangGraph port | 6 gate |
7 verdict | 8 trace | 9 prompts | 10 failures and cost | 11 packaging
