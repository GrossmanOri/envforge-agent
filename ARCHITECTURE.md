# Architecture

Invariants and decisions, not a mirror of the code. If a sentence here restates what a
file does, it belongs in the file instead.

## The pipeline

    script.py
       |
       v
    LLM writes a Dockerfile  (forced strict tool call, one string field)
       |
       v
    gate: allowlist of instructions, no line continuations  --reject--> repair
       |                                                                  ^
       v                                                                  |
    build  (network ON, apt and pip need it)  ------------- fails --------+
       |                                                                  |
       v                                                                  |
    run  (network OFF, all caps dropped, non-root, read-only) -- fails ---+
       |                                                          bounded evidence
       v
    verdict  (computed from observed behaviour; LLM opinion advisory)

The repair loop is the single back-edge, capped. Every repaired Dockerfile re-enters the
same gate before any build.

## Invariants

1. The gate runs before every build, first generation and every repair alike.
2. The gate is an allowlist of permitted instructions, never a blocklist.
3. No Dockerfile may contain a line continuation; every physical line is blank, a
   comment, or begins with an allowlisted instruction.
4. Build has network. Run has none.
5. The run has a memory cap, a pids cap, a cpu cap, read-only root with a tmpfs,
   cap-drop ALL, no-new-privileges, and a non-root user.
6. Every container is named and force-removed in a `finally`.
7. No Docker socket is mounted anywhere, and the agent is never containerised.
8. Test arguments are passed only after the image name, so an ENTRYPOINT image receives
   them as argv and never as docker flags.
9. All container output is attacker-controlled text, and evidence is bounded before it
   reaches a prompt.
10. The verdict is computed from observed behaviour. The model's opinion is labelled
    advisory and can never flip it.

Invariants 4 and 5 are asserted against the argv that `sandbox.py` actually builds, so
dropping a flag from the code fails a test rather than passing review.

## Decision log

### ADR-006: LLM provider layer is a hand-rolled Protocol, not a framework
Decided 2026-08-22. `envforge/llm.py`: an `LLM` Protocol, `AnthropicLLM` (native SDK,
strict tool use, GA 2026-01), `OpenAICompatLLM` (openai SDK plus `base_url`, covering
OpenAI and Groq), `make_llm("provider:model")`. Rejected: one OpenAI client for all
three, because Anthropic's compatibility layer ignores `strict`, so the only
grammar-guaranteed structured output for Claude is native-API-only, and Anthropic labels
that layer non-production. Rejected: LiteLLM and LangChain's model classes, because a
650-line project with one call site cannot justify a dependency it cannot explain, and
the trace module needs the unwrapped wire JSON. Provider wire differences live in two
classes of about thirty lines each; argument validation is uniform above them.

### ADR-007: Dockerfile generation shape
Decided 2026-08-22. Forced strict tool call, single field `{dockerfile: str}`,
`additionalProperties` false, with `base_image` declared alongside. The same call serves
generation and repair; repair is a full rewrite, never a diff. The gate bans line
continuations, so every physical line starts with an allowlisted instruction. Rejected:
raw text with fence stripping, because the extraction heuristic becomes a second parser
standing in front of the gate. Rejected: structured fields plus a renderer, because the
schema either stays too rigid for real scripts or grows into a shadow Dockerfile AST,
and the gate would then validate our own template rather than the untrusted artifact.
Note: the grammar guarantee holds on Anthropic and OpenAI; Groq is a forced call plus
validation.

## What crosses each boundary

| from | to | payload |
|---|---|---|
| CLI | agent | script path, model spec, caps |
| agent | LLM | script text, language, bounded failure evidence |
| LLM | gate | Dockerfile text, declared base image |
| gate | sandbox | approved Dockerfile, or a rejection reason |
| sandbox | agent | exit code, bounded stdout and stderr, timing |
| agent | verdict | what was attempted, what the cage stopped |
