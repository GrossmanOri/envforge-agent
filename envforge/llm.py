"""The model layer: one forced tool call, validated arguments, nothing hidden.

Every provider is asked for exactly one named tool call and nothing else. The
Dockerfile arrives as a schema-constrained argument rather than as prose we have
to extract, because an extraction heuristic would sit between a model that just
read attacker-controlled text and the gate that has to check it.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Protocol

MAX_TOKENS = 16000
PROVIDERS = ("anthropic", "openai", "groq")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

_JSON_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "object": dict, "array": list,
}


class LLMError(Exception):
    """The call did not produce usable arguments.

    It still cost tokens, and the tokens are carried here so the run's ledger can
    charge for them. A truncated reply burned the whole output ceiling, which is what
    truncation means, so a budget that only counted successes could be walked past by
    a loop that never succeeds. Zero is the honest default: it means no reply reached
    us to read a usage off, not that the call was free.
    """

    def __init__(self, message: str, input_tokens: int = 0, output_tokens: int = 0):
        super().__init__(message)
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class ProviderUnavailable(Exception):
    """We could not reach the model at all: a dead key, an empty account, a rate limit,
    a network failure.

    Deliberately not an `LLMError`. The loop catches the three `LLMError` types as
    repairable, meaning the reply was unusable and a rewritten prompt might do better.
    Nothing about this is repairable, and asking again spends money to fail identically.

    The distinction that matters is not the retry policy though, it is what the run is
    allowed to conclude. A refusal is a successful HTTP 200 with `stop_reason` set to
    `refusal`, and it is the model judging the script, which is a finding. Everything
    here is an exception with no response body at all, and it is our infrastructure
    failing, which is not a finding about anything. If these ever reached the refusal
    path we would build our own Dockerfile, run it, and report an ordinary-looking
    verdict on a run the model never saw. For a tool whose only product is a judgment
    about untrusted code, saying "fine" when the judge never arrived is the worst
    failure available.

    `kind` names which one, because the HTTP status does not: an exhausted account and a
    key without model access are both 403, separated only by the error type the provider
    reports.
    """

    def __init__(self, message: str, kind: str = "unavailable"):
        super().__init__(message)
        self.kind = kind


class MissingKey(ProviderUnavailable):
    """No usable credentials for this provider.

    A subclass of `ProviderUnavailable` so the loop's handler already covers it, and its
    own type so callers can stop reporting it as a malformed spec. It was reported as a
    usage error, which told a user to fix something they had typed correctly.
    """

    def __init__(self, variable: str):
        super().__init__(f"{variable} is not set", kind="no_key")
        self.variable = variable


class InvalidArguments(LLMError):
    """Arguments came back but do not satisfy the schema. Repairable."""


class Refused(LLMError):
    """The model declined. Rewriting the Dockerfile will not help.

    `reason` is whatever the provider said, kept as a structure rather than
    flattened into the message. It is worth recording and worth showing the
    user next to the observed behaviour, and it is never the verdict: the
    script being judged also wrote part of the text the judge read.
    """

    def __init__(self, message: str, reason: Any = None,
                 input_tokens: int = 0, output_tokens: int = 0):
        super().__init__(message, input_tokens, output_tokens)
        self.reason = reason


class Truncated(LLMError):
    """The response hit the token ceiling, so the arguments are incomplete."""


@dataclass(frozen=True)
class Tool:
    """One tool, in our shape. Each provider renders it into its own."""

    name: str
    description: str
    schema: dict[str, Any]

    def __post_init__(self) -> None:
        # Strict mode is a contract with the schema: both providers reject a
        # schema without these, and they reject it at request time, which would
        # make a caller's mistake look like a model failure inside the loop.
        if self.schema.get("additionalProperties") is not False:
            raise ValueError("a strict schema must set additionalProperties to false")
        if not self.schema.get("required"):
            raise ValueError("a strict schema must list its required fields")


@dataclass(frozen=True)
class Call:
    """What every provider returns. `model` comes from the response, not the
    request, because a provider is free to serve something other than what was
    asked for, and the trace has to record what actually answered."""

    arguments: dict[str, Any]
    model: str
    input_tokens: int
    output_tokens: int
    request: dict[str, Any]
    response: dict[str, Any]


class LLM(Protocol):
    def call(self, system: str, user: str, tool: Tool) -> Call: ...


def validate(arguments: Any, tool: Tool) -> dict[str, Any]:
    """Check arguments against the tool's own schema.

    Runs on every provider, including the two whose grammar constraint should
    make it unnecessary. Groq documents its schema guarantee as incompatible
    with tool use, so for Groq this is the only check there is.
    """
    if not isinstance(arguments, dict):
        raise InvalidArguments(f"expected an object, got {type(arguments).__name__}")
    properties = tool.schema.get("properties", {})
    for key in tool.schema.get("required", []):
        if key not in arguments:
            raise InvalidArguments(f"missing required field {key!r}")
    for key, value in arguments.items():
        if key not in properties:
            raise InvalidArguments(f"unexpected field {key!r}")
        declared = properties[key].get("type")
        expected = _JSON_TYPES.get(declared)
        if expected and not isinstance(value, expected):
            raise InvalidArguments(f"field {key!r} should be {declared}")
    return arguments


def _connection_errors() -> tuple[type[BaseException], ...]:
    """The connection-failure base class of whichever SDKs are installed.

    Imported lazily and tolerantly so this module still works with only one of them
    present, which is the reason the original check matched on a class name instead.
    Matching the base class is what makes a timeout count, since both SDKs derive their
    timeout error from their connection error.
    """
    found: list[type[BaseException]] = []
    for module in ("anthropic", "openai"):
        try:
            found.append(__import__(module).APIConnectionError)
        except (ImportError, AttributeError):
            continue
    return tuple(found) or (OSError,)


def kind_for_status(status: int, reported: str = "") -> str:
    """What an HTTP status from a provider means to us.

    Its own function because there were two copies. `reachable` widened `rejected` to
    every 4xx while the `--check` command stayed on 400 alone, so the same 422 was "our
    bug, do not retry" from a run and "provider unavailable, retry" from `--check`. Two
    entry points disagreeing about one event is what this exists to prevent.

    `reported` is the provider's own error type, needed only where a status is genuinely
    ambiguous: 403 is both an exhausted account and a key without model access.
    """
    if status == 402:
        # Payment Required is an empty account, which is the same event as a 403 billing
        # error and needs the same action. A blanket 4xx rule had it reported as our own
        # malformed request, telling someone out of credit not to retry and that the bug
        # was theirs.
        return "billing"
    if status == 403:
        return "billing" if "billing" in reported else "permission"
    if status == 404:
        return "no_such_model"
    if status in (401,):
        return "auth"
    if status in (408,):
        return "network"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    if 400 <= status < 500:
        # Anything else the provider refuses. Enumerating statuses always misses the
        # next one, so unmapped 4xx falls to a side rather than to the floor.
        return "rejected"
    return "unavailable"


def reachable(send):
    """Call the provider, and turn "we could not reach it" into one typed failure.

    Both SDKs raise their own exception classes, and neither is an `LLMError`, so
    without this they escape the loop's handlers entirely: the run dies mid-generator
    with a traceback, no outcome, and whatever was already spent unrecorded.

    Matched by HTTP status rather than by class name, because the two SDKs do not share
    a hierarchy and the statuses are the part both agree on. 403 is read further, since
    an exhausted account and a key that cannot use the model are the same status and
    only the provider's own error type separates them.
    """
    try:
        return send()
    except Exception as exc:               # noqa: BLE001 - narrowed immediately below
        status = getattr(exc, "status_code", None)
        if isinstance(exc, TypeError) and "resolve authentication" in str(exc):
            # A backstop, not the mechanism. Credentials are checked when the client is
            # built, so this only fires if that check is bypassed or the SDK changes
            # where it resolves them from. Matched on the SDK's specific phrase rather
            # than on the word "auth", which would have swallowed any TypeError from our
            # own code that happened to mention authentication.
            raise MissingKey("ANTHROPIC_API_KEY") from exc
        if status is None:
            # No HTTP response at all: DNS, TLS, a dropped connection, a timeout.
            # Matched by inheritance rather than by class name. The name test that was
            # here first missed `APITimeoutError`, which subclasses the connection error
            # in both SDKs but does not contain "connection", so a timed-out call went
            # back to escaping the loop entirely.
            if isinstance(exc, _connection_errors()):
                raise ProviderUnavailable(f"could not reach the provider: {exc}",
                                          kind="network") from exc
            raise
        # 400 is the provider rejecting the request we sent, which is not the provider
        # being unavailable and must not be reported as one: the model was reached and
        # answered. It is the same event as docker exit 125 one layer up, and ADR-008
        # already settled what that means, which is that our own code is broken.
        #
        # Two causes share the status. A prompt over the context window, which this tool
        # can produce by feeding a long log into a repair, and a malformed request, which
        # is a bug in us. Neither is fixed by retrying and neither is the provider's
        # fault, so they share an ending; the message carries which one the provider
        # said it was, since that is the only thing that separates them.
        kind = kind_for_status(status, getattr(exc, "type", "") or "")
        raise ProviderUnavailable(f"{kind}: {exc}", kind=kind) from exc


def charge(check, arguments: Any, tool: Tool, spent: tuple[int, int]) -> dict[str, Any]:
    """Run `validate` and make sure a failure carries what the reply cost.

    `validate` is called from both providers and knows nothing about tokens, and a
    reply that fails its schema cost exactly as much as one that passes.
    """
    try:
        return check(arguments, tool)
    except InvalidArguments as exc:
        exc.input_tokens, exc.output_tokens = spent
        raise


class AnthropicLLM:
    """The native SDK. Anthropic's OpenAI-compatibility layer ignores `strict`,
    so this is the only path that grammar-constrains Claude's arguments."""

    def __init__(self, model: str, client: Any = None):
        import anthropic

        self.model = model
        if client is not None:
            self._client = client
            return
        try:
            self._client = anthropic.Anthropic()
        except anthropic.AnthropicError as exc:
            # A named profile that is missing or corrupt makes the constructor itself
            # raise. Left uncaught this was the same traceback the credential check was
            # added to remove, just moved one line earlier.
            raise ProviderUnavailable(f"anthropic credentials: {exc}",
                                      kind="no_key") from exc
        # Asked of the constructed client rather than of the environment, and asked here
        # rather than left to request time. This SDK resolves credentials from several
        # places and raises nothing when it finds none, deferring to the first request
        # and raising `TypeError` there, which is not an exception any handler in this
        # program was looking for: a missing key crashed the run with a traceback.
        #
        # All three slots, not two. The first version read `api_key` and `auth_token`
        # only, which told anyone authenticated through `ant auth login` that their key
        # was not set: `credentials` is where the SDK puts a profile's bearer token and
        # a workload identity, and it is as valid as the other two. The test enshrined
        # the bug, since deleting two environment variables cannot reveal a third slot.
        if not any(getattr(self._client, name, None)
                   for name in ("api_key", "auth_token", "credentials")):
            raise MissingKey("ANTHROPIC_API_KEY")

    def build_request(self, system: str, user: str, tool: Tool) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "tools": [{
                "name": tool.name,
                "description": tool.description,
                "strict": True,
                "input_schema": tool.schema,
            }],
            "tool_choice": {"type": "tool", "name": tool.name},
        }

    @staticmethod
    def tokens(response: Any) -> tuple[int, int]:
        """What the reply cost, read off the response rather than the request, and
        read on the failing paths too. Defaults to zero only when there is no usage
        to read, which is a missing measurement rather than a free call."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        return getattr(usage, "input_tokens", 0), getattr(usage, "output_tokens", 0)

    def call(self, system: str, user: str, tool: Tool) -> Call:
        request = self.build_request(system, user, tool)
        response = reachable(lambda: self._client.messages.create(**request))
        spent = self.tokens(response)
        if response.stop_reason == "refusal":
            details = response.stop_details
            raise Refused(f"{self.model} declined: {details}",
                          reason=details.model_dump(mode="json") if details else None,
                          input_tokens=spent[0], output_tokens=spent[1])
        if response.stop_reason == "max_tokens":
            raise Truncated(f"{self.model} hit {MAX_TOKENS} tokens", *spent)
        # Thinking is adaptive by default, so the tool call is rarely content[0].
        block = next((b for b in response.content if b.type == "tool_use"), None)
        if block is None:
            raise LLMError(f"no tool_use block, stop_reason={response.stop_reason}", *spent)
        return Call(
            arguments=charge(validate, block.input, tool, spent),
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            request=request,
            response=response.model_dump(mode="json"),
        )


class OpenAICompatLLM:
    """OpenAI and Groq, which speak the same wire format through `base_url`.

    OpenAI grammar-constrains the arguments. Groq accepts the forced named call
    but documents its schema guarantee as not applying to tool use, so `strict`
    is left off there and `validate` is the whole guarantee.
    """

    def __init__(self, model: str, base_url: str | None = None, strict: bool = True,
                 api_key_env: str = "OPENAI_API_KEY", client: Any = None):
        import openai

        self.model = model
        self.strict = strict
        if client is not None:
            self._client = client
            return
        # Read the key ourselves. Handing openai a None key makes it fall back to
        # OPENAI_API_KEY, which would quietly send an OpenAI key to Groq and fail
        # as a 401 from the wrong provider halfway through a run.
        key = os.environ.get(api_key_env)
        if not key:
            raise MissingKey(api_key_env)
        self._client = openai.OpenAI(base_url=base_url, api_key=key)

    def build_request(self, system: str, user: str, tool: Tool) -> dict[str, Any]:
        function: dict[str, Any] = {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.schema,
        }
        if self.strict:
            function["strict"] = True
        return {
            "model": self.model,
            # The same ceiling the Anthropic path sends. Without it the budget's
            # estimate is a guess about a limit that does not exist: `estimate` adds
            # MAX_TOKENS on the assumption a reply cannot exceed it, so one reply here
            # could overshoot the whole budget before the next call is refused. It also
            # gives `Truncated` something to fire on, which is otherwise unreachable
            # on this path.
            "max_completion_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "tools": [{"type": "function", "function": function}],
            "tool_choice": {"type": "function", "function": {"name": tool.name}},
        }

    @staticmethod
    def tokens(response: Any) -> tuple[int, int]:
        """The same measurement under this wire format's own names."""
        usage = getattr(response, "usage", None)
        if usage is None:
            return 0, 0
        return getattr(usage, "prompt_tokens", 0), getattr(usage, "completion_tokens", 0)

    def call(self, system: str, user: str, tool: Tool) -> Call:
        request = self.build_request(system, user, tool)
        response = reachable(lambda: self._client.chat.completions.create(**request))
        spent = self.tokens(response)
        choice = response.choices[0]
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise Refused(f"{self.model} declined: {refusal}", reason=refusal,
                          input_tokens=spent[0], output_tokens=spent[1])
        if choice.finish_reason == "length":
            raise Truncated(f"{self.model} ran out of output tokens", *spent)
        calls = choice.message.tool_calls or []
        if not calls:
            raise LLMError(f"no tool call, finish_reason={choice.finish_reason}", *spent)
        try:
            arguments = json.loads(calls[0].function.arguments)
        except json.JSONDecodeError as exc:
            raise InvalidArguments(f"arguments were not JSON: {exc}", *spent) from exc
        return Call(
            arguments=charge(validate, arguments, tool, spent),
            model=response.model,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            request=request,
            response=response.model_dump(mode="json"),
        )


def make_llm(spec: str) -> LLM:
    """Build a provider from "provider:model", failing at startup rather than
    mid-run. The client is constructed here for the same reason: a missing key
    should stop the process before a container has been built."""
    provider, separator, model = spec.partition(":")
    if not separator or not model:
        raise ValueError(f"spec must be provider:model, got {spec!r}")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}, expected one of {', '.join(PROVIDERS)}")
    if provider == "anthropic":
        return AnthropicLLM(model)
    if provider == "openai":
        return OpenAICompatLLM(model)
    return OpenAICompatLLM(model, base_url=GROQ_BASE_URL, strict=False,
                           api_key_env="GROQ_API_KEY")
