# envforge-agent

Read STATUS.md first. It says where the build is and what is next.

## What this is
An agent that takes a script it does not trust, has an LLM write a Dockerfile for it,
builds and runs it in a hardened container, repairs on failure in a capped loop with
bounded evidence, and reports what the script tried to do. Python and Bash only, claimed
honestly.

Size target 400 to 600 lines including tests. Past 800, stop and ask what went wrong.
Any module that wants a subdirectory has outgrown this project.

## Security constraints, not negotiable
Host CLI. No Docker socket is mounted anywhere, and the agent itself is never
containerised, because that would reintroduce the socket.

Build has network, since apt and pip need it. Run has none, plus a memory cap, a pids
cap, a cpu cap, read-only root with a tmpfs, cap-drop ALL, no-new-privileges, and a
non-root user. Every container is named and force-removed in a `finally`: killing the
docker client does not kill the container, which is verified behaviour, not theory.

Every LLM-written Dockerfile passes a deterministic gate before build, repaired ones
included. The gate is an allowlist of permitted instructions (pinned FROM, COPY, a
restricted RUN, USER, CMD, ENTRYPOINT), never a blocklist of bad ones: a blocklist is
bypassed by the first form nobody thought of. This matters because build has network
and the Dockerfile is written by a model that just read attacker-controlled text. Test arguments only ever go after the image name, so an ENTRYPOINT image
receives them as argv and never as docker flags.

All container output is attacker-controlled text. Repair evidence is bounded before it
reaches a prompt.

The verdict is produced after the sandboxed run, from observed behaviour. The model's
opinion is one input, labelled advisory, never the gate.

## Shape
Plain loop first, LangGraph port immediately after, both engines behind one interface.
Everything after that is built as graph nodes. Seams kept deliberately: a `Sandbox`
protocol, the agent yields events rather than printing, prompts live as files
parameterised by language.

## Exit codes the sandbox must distinguish
137 is a kernel kill, so the script misbehaved. 1 is the script raising on its own.
125 is the docker CLI rejecting our command, so our own code is broken. Build the docker
command as a Python list, never a string.

## Commands
    python -m pytest              unit tests, no Docker needed
    python -m pytest -m docker    the tests that hit real Docker
    python -m envforge <script>   run the agent against a script
