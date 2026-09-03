"""The model layer: a chat model per provider, and what a framework does not do.

`make_llm("provider:model")` returns a LangChain chat model. `ChatAnthropic` for
Anthropic, `ChatOpenAI` for OpenAI and for Groq through its own base url. The graph binds
tools to whichever it gets, in one place, and nothing here assembles a request or parses
a response any more: that layer was hand-written until 2026-09-03 and ADR-006 records why
it went.

What is left is the part LangChain does not do. Which environment variable a provider
reads, so a missing key is its own failure rather than a malformed spec. Whether a
provider promises a grammar for tool arguments, since Groq documents its schema guarantee
as not covering tool use and asking for one would be claiming something it does not give.
And the classification of an HTTP status: an empty account, a dead key, a rate limit and
our own malformed request need different actions from whoever reads the exit code, and
that took several rounds and several real incidents to get right. LangChain passes the
SDK exception through untouched, so it applies unchanged.
"""

from __future__ import annotations

import os

MAX_TOKENS = 16000
PROVIDERS = ("anthropic", "openai", "groq")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

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


def kind_for_status(status: int, reported: str = "") -> str:
    """What an HTTP status from a provider means to us.

    Its own function because there were two copies, and they disagreed: one widened
    `rejected` to every 4xx while the other stayed on 400, so the same 422 was "our bug,
    do not retry" from a run and "provider unavailable, retry" from `--check`. Two entry
    points disagreeing about one event is what this exists to prevent.

    `reported` is the provider's own error type, needed only where a status is genuinely
    ambiguous: 403 is both an exhausted account and a key without model access.
    """
    if status == 402:
        # Payment Required is an empty account, the same event as a 403 billing error and
        # needing the same action. A blanket 4xx rule reported it as our own malformed
        # request, telling someone out of credit not to retry and that the bug was theirs.
        return "billing"
    if status == 403:
        return "billing" if "billing" in reported else "permission"
    if status == 404:
        return "no_such_model"
    if status == 401:
        return "auth"
    if status == 408:
        return "network"
    if status == 429:
        return "rate_limit"
    if status >= 500:
        return "server"
    if 400 <= status < 500:
        # Anything else the provider refuses. Enumerating statuses always misses the next
        # one, so an unmapped 4xx falls to a side rather than to the floor.
        return "rejected"
    return "unavailable"


def _connection_errors() -> tuple[type[BaseException], ...]:
    """The connection-failure base class of whichever SDKs are installed.

    Imported lazily and tolerantly so this module works with only one of them present.
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


def classify(exc: BaseException) -> ProviderUnavailable | None:
    """The provider failing, named, or None if this is not that.

    The one piece of the old hand-written layer worth keeping, and the reason it is kept
    rather than rewritten: it took several rounds to get right and each round was a real
    incident. 402 is an empty account, and so is a 403 whose error type says billing,
    while a 403 without it is a key that cannot use the model. 401 is a dead key. 429 is
    a rate limit. Any other 4xx is our own malformed request, which is a bug in us rather
    than the provider being down, and the two need different actions from whoever reads
    the exit code.

    LangChain passes the SDK's exception through untouched, so the status is where it
    always was and this rule applies unchanged to a chat model.

    Returns None for anything that is not a provider failure, so a caller re-raises
    rather than dressing a bug of ours up as a tidy verdict about the sample.
    """
    if isinstance(exc, ProviderUnavailable):
        return exc
    status = getattr(exc, "status_code", None)
    if status is None:
        if isinstance(exc, _connection_errors()):
            # No HTTP response at all: DNS, TLS, a dropped connection, a timeout.
            # Matched by inheritance rather than by class name, because the name test
            # that was here first missed `APITimeoutError`, which subclasses the
            # connection error in both SDKs and does not contain "connection".
            return ProviderUnavailable(f"could not reach the provider: {exc}",
                                       kind="network")
        return None
    kind = kind_for_status(status, getattr(exc, "type", "") or "")
    return ProviderUnavailable(f"{kind}: {exc}", kind=kind)


def make_llm(spec: str):
    """Build a LangChain chat model from "provider:model", failing at startup.

    The hand-written request building and response parsing this replaced is gone. What
    it was protecting is not: strict tool schemas still go to the two providers that
    honour them, the arguments are still validated locally, and the failure
    classification above is still ours.

    Groq is deliberately not given `strict`. It documents its schema guarantee as not
    applying to tool use, so asking for it would be claiming a guarantee the provider
    does not give; `graph.py` validates a submission's arguments itself, on every
    provider, which is the only check Groq actually has.
    """
    provider, separator, model = spec.partition(":")
    if not separator or not model:
        raise ValueError(f"spec must be provider:model, got {spec!r}")
    if provider not in PROVIDERS:
        raise ValueError(f"unknown provider {provider!r}, expected one of "
                         f"{', '.join(PROVIDERS)}")

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise MissingKey("ANTHROPIC_API_KEY")
        return ChatAnthropic(model=model, max_tokens=MAX_TOKENS)

    from langchain_openai import ChatOpenAI

    # Read the key ourselves rather than letting the client default it. Handing
    # `ChatOpenAI` no key makes it fall back to OPENAI_API_KEY, which would quietly send
    # an OpenAI secret to Groq's servers and fail as a 401 from the wrong provider
    # halfway through a run.
    variable = "OPENAI_API_KEY" if provider == "openai" else "GROQ_API_KEY"
    key = os.environ.get(variable)
    if not key:
        raise MissingKey(variable)
    base_url = None if provider == "openai" else GROQ_BASE_URL
    return ChatOpenAI(model=model, api_key=key, base_url=base_url,
                      max_completion_tokens=MAX_TOKENS)


def supports_strict(spec: str) -> bool:
    """Whether this provider grammar-constrains tool arguments.

    Anthropic and OpenAI do. Groq accepts the parameter and documents its schema
    guarantee as not covering tool use, so it is not asked for one: claiming it would be
    claiming something the provider does not promise, which is the sentence this project
    keeps a rule about.
    """
    return spec.partition(":")[0] in ("anthropic", "openai")
