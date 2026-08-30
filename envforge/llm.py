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
        self._client = client if client is not None else anthropic.Anthropic()

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
        response = self._client.messages.create(**request)
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
            raise ValueError(f"{api_key_env} is not set")
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
        response = self._client.chat.completions.create(**request)
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
