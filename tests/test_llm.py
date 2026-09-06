"""The model layer: three providers, one factory, and the failure classification.

The hand-written request building and response parsing this file used to test is gone.
`make_llm` returns a LangChain chat model and the graph binds tools to it in one place.
What did not go is the part that took several rounds and several real incidents to get
right: which HTTP status means an empty account, which means a dead key, and which means
our own malformed request. That is still ours, and most of this file is about it.

Nothing here reaches a network and nothing needs an API key, which is not a convenience:
CI has neither.
"""

from __future__ import annotations

import pytest

from envforge.llm import (GROQ_BASE_URL, MAX_TOKENS, MissingKey, ProviderUnavailable,
                          classify, kind_for_status, make_llm, supports_strict)


class Sdk(Exception):
    """An SDK exception in the shape both providers raise and LangChain passes through."""

    def __init__(self, message="no", status_code=None, type_=""):
        super().__init__(message)
        if status_code is not None:
            self.status_code = status_code
        self.type = type_


# --- the factory ----------------------------------------------------------------------

@pytest.mark.parametrize("spec", ["anthropic", "anthropic:", ":claude", "gemini:pro", ""])
def test_a_bad_spec_is_refused_at_startup(spec):
    """Before a container is built, not in the middle of a run."""
    with pytest.raises(ValueError):
        make_llm(spec)


def test_anthropic_gets_the_anthropic_chat_model(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    model = make_llm("anthropic:claude-sonnet-5")
    assert type(model).__name__ == "ChatAnthropic"
    assert model.model == "claude-sonnet-5"
    assert model.max_tokens == MAX_TOKENS


def test_openai_gets_the_openai_chat_model_and_no_base_url(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    model = make_llm("openai:gpt-5")
    assert type(model).__name__ == "ChatOpenAI"
    assert model.model_name == "gpt-5"
    assert not model.openai_api_base


def test_groq_gets_the_openai_client_pointed_at_groq(monkeypatch):
    """The same wire format through a different base url, which is how Groq was reached
    before and is the whole reason it costs one branch rather than a third client."""
    monkeypatch.setenv("GROQ_API_KEY", "gsk-groq")
    model = make_llm("groq:llama-3.3-70b")
    assert type(model).__name__ == "ChatOpenAI"
    assert model.openai_api_base == GROQ_BASE_URL


def test_groq_will_not_borrow_the_openai_key(monkeypatch):
    """Two providers speak the same wire format but not with the same key. Letting the
    client default it would send an OpenAI secret to Groq's servers and fail as a 401
    from the wrong provider halfway through a run."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(MissingKey) as caught:
        make_llm("groq:llama-3.3-70b")
    assert caught.value.variable == "GROQ_API_KEY"


@pytest.mark.parametrize("spec, variable", [
    ("anthropic:claude-sonnet-5", "ANTHROPIC_API_KEY"),
    ("openai:gpt-5", "OPENAI_API_KEY"),
    ("groq:llama-3.3-70b", "GROQ_API_KEY"),
])
def test_a_missing_key_is_its_own_failure_not_a_bad_spec(monkeypatch, spec, variable):
    """Reporting a missing key as a malformed spec told the user to fix something they
    had typed correctly. It is a `ProviderUnavailable`, so the handler already covers it
    and the shell learns 3 rather than 2."""
    monkeypatch.delenv(variable, raising=False)
    with pytest.raises(MissingKey) as caught:
        make_llm(spec)
    assert caught.value.variable == variable
    assert isinstance(caught.value, ProviderUnavailable)


# --- strict schemas, and the one provider that does not promise them --------------------

def test_only_the_two_providers_that_promise_a_grammar_are_asked_for_one():
    """Groq documents its schema guarantee as not applying to tool use, so asking for
    `strict` there would be claiming something the provider does not give. It gets a
    forced call plus local validation instead, and the graph validates a submission on
    every provider because it is the only check Groq has."""
    assert supports_strict("anthropic:claude-sonnet-5")
    assert supports_strict("openai:gpt-5")
    assert not supports_strict("groq:llama-3.3-70b")


# --- the failure classification ---------------------------------------------------------

@pytest.mark.parametrize("status, reported, kind", [
    (401, "", "auth"),
    (402, "", "billing"),
    (403, "billing_error", "billing"),
    (403, "", "permission"),
    (404, "", "no_such_model"),
    (408, "", "network"),
    (429, "", "rate_limit"),
    (400, "", "rejected"),
    (422, "", "rejected"),
    (500, "", "server"),
    (503, "", "server"),
])
def test_a_status_means_one_thing_everywhere(status, reported, kind):
    """One function because there were two copies and they disagreed: one widened
    `rejected` to every 4xx while the other stayed on 400, so the same 422 was "our bug,
    do not retry" from a run and "provider unavailable, retry" from `--check`."""
    assert kind_for_status(status, reported) == kind


def test_payment_required_is_an_empty_account_and_not_our_bad_request():
    """A blanket 4xx rule reported it as our own malformed request, telling someone out
    of credit not to retry and that the bug was theirs."""
    assert kind_for_status(402) == "billing"
    assert kind_for_status(403, "billing_error") == kind_for_status(402)


def test_forbidden_is_read_further_because_the_status_is_ambiguous():
    """An exhausted account and a key without model access are both 403, and only the
    provider's own error type separates them."""
    assert kind_for_status(403, "billing_error") == "billing"
    assert kind_for_status(403, "permission_error") == "permission"


@pytest.mark.parametrize("status, kind", [(401, "auth"), (402, "billing"),
                                          (429, "rate_limit"), (422, "rejected")])
def test_classify_names_a_provider_failure(status, kind):
    failure = classify(Sdk("no", status))
    assert isinstance(failure, ProviderUnavailable) and failure.kind == kind


def test_classify_passes_a_provider_unavailable_through_unchanged():
    original = ProviderUnavailable("already named", kind="network")
    assert classify(original) is original


def test_a_bug_of_ours_is_not_a_provider_failure():
    """Anything with no status is not the provider failing. Returning a tidy
    `ProviderUnavailable` here would hide our own crash behind a report about the sample,
    which for a tool whose only product is a judgment about untrusted code is the worst
    failure available."""
    assert classify(ValueError("a bug in our own code")) is None
    assert classify(KeyError("state")) is None


def test_a_dropped_connection_is_a_provider_failure_even_with_no_status():
    """Matched by inheritance rather than by class name. The name test that was here
    first missed `APITimeoutError`, which subclasses the connection error in both SDKs
    and does not contain the word connection."""
    import anthropic
    import openai

    for exc in (anthropic.APIConnectionError(request=None),
                openai.APITimeoutError(request=None)):
        failure = classify(exc)
        assert failure is not None and failure.kind == "network"
