"""Unit tests for chat_agent 429 resilience.

Covers the self-host-owned ``utils.retrieval.chat_resilience`` module: pure decision
helpers (retryable-error classification, Retry-After-aware backoff, fallback-chain
parsing, tool/message conversion), the persistent-rate-limit circuit breaker, and the
non-streaming provider fallback loop ``run_openai_fallback_agent`` driven by a fake
OpenAI-compatible LLM. Also exercises the full ``agentic._run_anthropic_agent_stream``
seam end-to-end for the post-tool-synthesis 429 case. No network or real provider calls.
"""

import asyncio
import os

import anthropic
import httpx
import pytest

os.environ.setdefault(
    "ENCRYPTION_SECRET",
    "omi_ZwB2ZNqB2HHpMK6wStk7sTpavJiPTFg7gXUHnc4tFABPU6pZ2c2DKgehtfgi4RZv",
)
# agentic -> conversation_tools -> utils.conversations.search constructs a Typesense
# client at import time; give it dummy config so import succeeds (client validates
# config only, never connects here). Mirrors conftest's fake OPENAI_API_KEY.
os.environ.setdefault("TYPESENSE_API_KEY", "test-typesense-key")
os.environ.setdefault("TYPESENSE_HOST", "localhost")
os.environ.setdefault("TYPESENSE_HOST_PORT", "8108")

# Imported for real — tests/conftest.py makes this hermetic (fake OPENAI_API_KEY,
# stubbed tiktoken, blocked outbound network). No module-scope sys.modules mutation.
# The resilience logic now lives in `chat_resilience`; `agentic` is still exercised for
# the full-loop seam test.
from utils.retrieval import agentic
from utils.retrieval import chat_resilience


@pytest.fixture(autouse=True)
def _reset_circuit_breaker():
    """Isolate the process-global breaker between tests (a success fully resets it)."""
    chat_resilience.record_anthropic_success()
    yield
    chat_resilience.record_anthropic_success()


# --------------------------------------------------------------------------- #
# Test doubles
# --------------------------------------------------------------------------- #


class _FakeResponse:
    def __init__(self, headers):
        self.headers = headers


class _FakeHeaderError(Exception):
    """Stand-in for an Anthropic error carrying a Retry-After response header."""

    def __init__(self, headers):
        super().__init__("rate limited")
        self.response = _FakeResponse(headers)


class _FakeAI:
    """Mimics a LangChain AIMessage: .content plus .tool_calls."""

    def __init__(self, content="", tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class _FakeLLM:
    """Returns a scripted sequence of AI messages from ainvoke()."""

    def __init__(self, responses):
        self._responses = list(responses)
        self._i = 0
        self.bound_tools = None

    def bind_tools(self, tools):
        self.bound_tools = tools
        return self

    async def ainvoke(self, messages):
        resp = self._responses[self._i]
        self._i += 1
        return resp


class _RaisingLLM:
    def bind_tools(self, tools):
        return self

    async def ainvoke(self, messages):
        raise RuntimeError("provider down")


class _StubToolSchema:
    @staticmethod
    def schema():
        return {
            "properties": {
                "query": {"type": "string", "title": "Query"},
                "config": {"type": "object", "title": "Config"},
            },
            "required": ["query", "config"],
        }


class _StubTool:
    name = "demo_tool"
    description = "A demo tool"
    args_schema = _StubToolSchema


def _display_name(name, tool_obj=None):
    """Injected get_tool_display_name stub (agentic owns the real one)."""
    return name


async def _noop_execute(name, args, registry, configurable):
    """Injected execute_tool stub for fallback tests that never invoke a tool."""
    return "UNUSED"


def _drain(callback):
    items = []
    while not callback.queue.empty():
        items.append(callback.queue.get_nowait())
    return items


def _anthropic_status_error(status_code, headers=None):
    """Build a real anthropic.APIStatusError (its constructor needs an httpx.Response).

    Constructing httpx Request/Response objects performs no network I/O, so this is
    safe under conftest's outbound-network block.
    """
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(status_code, request=request, headers=headers or {})
    return anthropic.APIStatusError("boom", response=response, body=None)


def _anthropic_rate_limit_error(headers=None):
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    response = httpx.Response(429, request=request, headers=headers or {})
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def _anthropic_connection_error():
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=request)


# --------------------------------------------------------------------------- #
# is_retryable / is_rate_limit classification
# --------------------------------------------------------------------------- #


def test_rate_limit_error_is_retryable():
    assert chat_resilience.is_retryable_anthropic_error(_anthropic_rate_limit_error()) is True


def test_connection_error_is_retryable():
    assert chat_resilience.is_retryable_anthropic_error(_anthropic_connection_error()) is True


def test_status_error_retryable_only_for_transient_codes():
    assert chat_resilience.is_retryable_anthropic_error(_anthropic_status_error(503)) is True
    assert chat_resilience.is_retryable_anthropic_error(_anthropic_status_error(400)) is False


def test_non_anthropic_error_is_not_retryable():
    assert chat_resilience.is_retryable_anthropic_error(ValueError("nope")) is False


def test_is_rate_limit_error_only_for_429_529():
    # Drives the circuit breaker: only genuine rate-limit/overload counts, not other 5xx.
    assert chat_resilience.is_rate_limit_error(_anthropic_rate_limit_error()) is True
    assert chat_resilience.is_rate_limit_error(_anthropic_status_error(529)) is True
    assert chat_resilience.is_rate_limit_error(_anthropic_status_error(503)) is False
    assert chat_resilience.is_rate_limit_error(_anthropic_connection_error()) is False
    assert chat_resilience.is_rate_limit_error(ValueError("nope")) is False


# --------------------------------------------------------------------------- #
# backoff / Retry-After
# --------------------------------------------------------------------------- #


def test_retry_after_seconds_header():
    assert chat_resilience.retry_after_seconds(_FakeHeaderError({"retry-after": "2"})) == 2.0


def test_retry_after_ms_header():
    assert chat_resilience.retry_after_seconds(_FakeHeaderError({"retry-after-ms": "1500"})) == 1.5


def test_retry_after_absent_returns_none():
    assert chat_resilience.retry_after_seconds(_FakeHeaderError({})) is None
    assert chat_resilience.retry_after_seconds(ValueError("no response attr")) is None


def test_backoff_prefers_retry_after_capped():
    # Honors the server value, capped at the module max.
    assert chat_resilience.backoff_delay(1, _FakeHeaderError({"retry-after": "2"})) == 2.0
    big = chat_resilience.backoff_delay(1, _FakeHeaderError({"retry-after": "9999"}))
    assert big == chat_resilience.MAX_BACKOFF_SECONDS


def test_backoff_exponential_with_jitter_bounds():
    # No Retry-After header -> exponential base 0.5 * 2**(attempt-1) plus [0, 0.5) jitter.
    d1 = chat_resilience.backoff_delay(1, _FakeHeaderError({}))
    assert 0.5 <= d1 < 1.0
    d3 = chat_resilience.backoff_delay(3, _FakeHeaderError({}))
    assert 2.0 <= d3 < 2.5


# --------------------------------------------------------------------------- #
# fallback chain parsing / config
# --------------------------------------------------------------------------- #


def test_fallback_chain_default(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_FALLBACK_CHAIN", raising=False)
    assert chat_resilience.fallback_chain() == [
        ("openrouter", "anthropic/claude-sonnet-4-6"),
        ("openai", "gpt-4.1"),
    ]


def test_fallback_chain_custom_and_filtering(monkeypatch):
    # Unknown providers and malformed entries are dropped; model strings preserved.
    monkeypatch.setenv(
        "CHAT_AGENT_FALLBACK_CHAIN",
        "openai:gpt-4.1 , bogusprovider:x, openrouter:anthropic/claude-sonnet-4-6, noseparator",
    )
    assert chat_resilience.fallback_chain() == [
        ("openai", "gpt-4.1"),
        ("openrouter", "anthropic/claude-sonnet-4-6"),
    ]


def test_fallback_enabled_toggle(monkeypatch):
    monkeypatch.delenv("CHAT_AGENT_FALLBACK_ENABLED", raising=False)
    assert chat_resilience.fallback_enabled() is True
    monkeypatch.setenv("CHAT_AGENT_FALLBACK_ENABLED", "false")
    assert chat_resilience.fallback_enabled() is False


def test_retry_attempts_floor(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_RETRY_ATTEMPTS", "0")
    assert chat_resilience.retry_attempts() == 1  # clamped to >= 1
    monkeypatch.setenv("CHAT_AGENT_RETRY_ATTEMPTS", "notint")
    assert chat_resilience.retry_attempts() == 3  # falls back to default


# --------------------------------------------------------------------------- #
# circuit breaker (persistent rate-limiting)
# --------------------------------------------------------------------------- #


def test_circuit_breaker_trips_after_threshold_and_resets(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_ANTHROPIC_TRIP_THRESHOLD", "2")
    monkeypatch.setenv("CHAT_AGENT_ANTHROPIC_COOLDOWN_SECONDS", "60")
    chat_resilience.record_anthropic_success()  # clean slate (autouse fixture also resets)

    assert chat_resilience.should_skip_anthropic() is False
    chat_resilience.record_anthropic_rate_limit()
    assert chat_resilience.should_skip_anthropic() is False  # one short of threshold
    chat_resilience.record_anthropic_rate_limit()
    assert chat_resilience.should_skip_anthropic() is True  # threshold reached -> tripped

    chat_resilience.record_anthropic_success()  # any success clears it
    assert chat_resilience.should_skip_anthropic() is False


def test_circuit_breaker_cooldown_expires(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_ANTHROPIC_TRIP_THRESHOLD", "1")
    monkeypatch.setenv("CHAT_AGENT_ANTHROPIC_COOLDOWN_SECONDS", "0")
    chat_resilience.record_anthropic_success()

    chat_resilience.record_anthropic_rate_limit()  # trips immediately (threshold 1)
    # cooldown 0 -> skip_until == now, so the window has already elapsed by the check.
    assert chat_resilience.should_skip_anthropic() is False


# --------------------------------------------------------------------------- #
# message + tool schema conversion
# --------------------------------------------------------------------------- #


def test_coerce_text_variants():
    assert chat_resilience.coerce_text("hi") == "hi"
    assert chat_resilience.coerce_text([{"type": "text", "text": "a"}, "b", {"type": "image"}]) == "ab"
    assert chat_resilience.coerce_text(None) == ""


def test_anthropic_msgs_to_langchain_roles():
    out = chat_resilience.anthropic_msgs_to_langchain(
        [{"role": "user", "content": "hello"}, {"role": "assistant", "content": "hi there"}]
    )
    assert [type(m).__name__ for m in out] == ["HumanMessage", "AIMessage"]
    assert out[0].content == "hello" and out[1].content == "hi there"


def test_langchain_tool_to_openai_strips_config_and_title():
    spec = chat_resilience.langchain_tool_to_openai(_StubTool)
    assert spec["type"] == "function"
    fn = spec["function"]
    assert fn["name"] == "demo_tool"
    assert fn["description"] == "A demo tool"
    params = fn["parameters"]
    assert "config" not in params["properties"]  # injected RunnableConfig dropped
    assert "config" not in params["required"]
    assert params["required"] == ["query"]
    assert "title" not in params["properties"]["query"]  # pydantic title dropped


# --------------------------------------------------------------------------- #
# provider fallback loop
# --------------------------------------------------------------------------- #


def test_fallback_no_tool_calls_streams_answer(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_FALLBACK_CHAIN", "openai:gpt-4.1")
    monkeypatch.setattr(
        chat_resilience,
        "get_or_create_openai_compatible_llm",
        lambda provider, model, streaming=False: _FakeLLM([_FakeAI(content="Hello from fallback")]),
    )

    callback = agentic.AsyncStreamingCallback()
    guard = agentic.AgentSafetyGuard(max_tool_calls=25, max_context_tokens=500000)
    full_response = []

    ok = asyncio.run(
        chat_resilience.run_openai_fallback_agent(
            "system",
            [{"role": "user", "content": "hi"}],
            {},
            callback,
            full_response,
            guard,
            {},
            core_tools=[],
            execute_tool=_noop_execute,
            get_tool_display_name=_display_name,
        )
    )

    assert ok is True
    assert "".join(full_response) == "Hello from fallback"
    items = _drain(callback)
    assert "data: Hello from fallback" in items
    assert items[-1] is None  # stream terminated


def test_fallback_executes_tool_then_answers(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_FALLBACK_CHAIN", "openai:gpt-4.1")
    monkeypatch.setattr(
        chat_resilience,
        "get_or_create_openai_compatible_llm",
        lambda provider, model, streaming=False: _FakeLLM(
            [
                _FakeAI(tool_calls=[{"name": "demo_tool", "args": {"query": "x"}, "id": "t1"}]),
                _FakeAI(content="final answer"),
            ]
        ),
    )

    executed = []

    async def _fake_execute(name, args, registry, configurable):
        executed.append((name, args))
        return "TOOL_RESULT"

    callback = agentic.AsyncStreamingCallback()
    guard = agentic.AgentSafetyGuard(max_tool_calls=25, max_context_tokens=500000)
    full_response = []

    ok = asyncio.run(
        chat_resilience.run_openai_fallback_agent(
            "system",
            [{"role": "user", "content": "do it"}],
            {},
            callback,
            full_response,
            guard,
            {},
            core_tools=[],
            execute_tool=_fake_execute,
            get_tool_display_name=_display_name,
        )
    )

    assert ok is True
    assert executed == [("demo_tool", {"query": "x"})]
    assert "".join(full_response) == "final answer"


def test_fallback_advances_to_next_provider_on_error(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_FALLBACK_CHAIN", "openrouter:anthropic/claude-sonnet-4-6,openai:gpt-4.1")

    def _factory(provider, model, streaming=False):
        if provider == "openrouter":
            return _RaisingLLM()
        return _FakeLLM([_FakeAI(content="second provider answer")])

    monkeypatch.setattr(chat_resilience, "get_or_create_openai_compatible_llm", _factory)

    callback = agentic.AsyncStreamingCallback()
    guard = agentic.AgentSafetyGuard(max_tool_calls=25, max_context_tokens=500000)
    full_response = []

    ok = asyncio.run(
        chat_resilience.run_openai_fallback_agent(
            "system",
            [{"role": "user", "content": "hi"}],
            {},
            callback,
            full_response,
            guard,
            {},
            core_tools=[],
            execute_tool=_noop_execute,
            get_tool_display_name=_display_name,
        )
    )

    assert ok is True
    assert "".join(full_response) == "second provider answer"


def test_fallback_returns_false_when_chain_empty(monkeypatch):
    monkeypatch.setenv("CHAT_AGENT_FALLBACK_CHAIN", "bogus:only,alsobad")

    callback = agentic.AsyncStreamingCallback()
    guard = agentic.AgentSafetyGuard(max_tool_calls=25, max_context_tokens=500000)

    ok = asyncio.run(
        chat_resilience.run_openai_fallback_agent(
            "system",
            [{"role": "user", "content": "hi"}],
            {},
            callback,
            [],
            guard,
            {},
            core_tools=[],
            execute_tool=_noop_execute,
            get_tool_display_name=_display_name,
        )
    )

    assert ok is False  # nothing usable in the chain -> caller emits generic error


# --------------------------------------------------------------------------- #
# post-tool synthesis 429 -> provider fallback (reproduces the live search failure)
# --------------------------------------------------------------------------- #


class _Block:
    """Minimal Anthropic content block (tool_use / text)."""

    def __init__(self, type, **kw):
        self.type = type
        for k, v in kw.items():
            setattr(self, k, v)


class _Msg:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _FakeStreamCM:
    """Async context manager standing in for anthropic_client.messages.stream(...).

    Yields no events (so no text streams) and returns a scripted final message, or
    raises on __aenter__ to simulate a request-time error (e.g. a 429).
    """

    def __init__(self, events=None, final_message=None, raise_exc=None):
        self._events = events or []
        self._final = final_message
        self._raise = raise_exc

    async def __aenter__(self):
        if self._raise is not None:
            raise self._raise
        return self

    async def __aexit__(self, *exc):
        return False

    def __aiter__(self):
        async def _gen():
            for e in self._events:
                yield e

        return _gen()

    async def get_final_message(self):
        return self._final


class _FakeAnthropicClient:
    """Serves a scripted stream context manager per turn via .messages.stream(...)."""

    def __init__(self, cms):
        self._cms = list(cms)
        self._i = 0
        outer = self

        class _Messages:
            def stream(self, **kwargs):
                cm = outer._cms[outer._i]
                outer._i += 1
                return cm

        self.messages = _Messages()


def test_post_tool_synthesis_429_falls_back_and_answers(monkeypatch):
    # Turn 1 requests a tool; the post-tool synthesis turn then 429s (a large tool result
    # pushing the request over the org's input-tokens/min limit). The provider fallback must
    # trigger on this POST-tool turn (not just the first turn) and still answer, restarting
    # from the pre-tool snapshot with the final text streamed exactly once.
    monkeypatch.setenv("CHAT_AGENT_RETRY_ATTEMPTS", "1")  # no backoff sleeps; straight to fallback
    monkeypatch.setenv("CHAT_AGENT_FALLBACK_CHAIN", "openai:gpt-4.1")

    tool_use_msg = _Msg("tool_use", [_Block("tool_use", id="t1", name="demo_tool", input={"query": "lunch"})])
    fake_client = _FakeAnthropicClient(
        [
            _FakeStreamCM(final_message=tool_use_msg),  # turn 1: request a tool (no text)
            _FakeStreamCM(raise_exc=_anthropic_rate_limit_error()),  # turn 2 (synthesis): 429
        ]
    )
    monkeypatch.setattr(agentic, "anthropic_client", fake_client)
    # The fallback provider lives in chat_resilience now; agentic injects CORE_TOOLS /
    # _execute_tool / get_tool_display_name into it, so patch the LLM factory there.
    monkeypatch.setattr(
        chat_resilience,
        "get_or_create_openai_compatible_llm",
        lambda provider, model, streaming=False: _FakeLLM([_FakeAI(content="FALLBACK_SYNTHESIS_OK")]),
    )

    async def _fake_execute(name, args, registry, configurable):
        return "TOOL_RESULT: found 2 conversations"

    # Injected via the seam by reference to agentic's module global -> patch it here.
    monkeypatch.setattr(agentic, "_execute_tool", _fake_execute)

    callback = agentic.AsyncStreamingCallback()
    guard = agentic.AgentSafetyGuard(max_tool_calls=25, max_context_tokens=500000)
    full_response = []
    messages = [{"role": "user", "content": "find my lunch plans"}]

    asyncio.run(
        agentic._run_anthropic_agent_stream(
            "SYSTEM", messages, [], {}, callback, full_response, guard, {"user_id": "u1"}
        )
    )

    assert "FALLBACK_SYNTHESIS_OK" in "".join(full_response)  # fallback synthesized the answer
    items = _drain(callback)
    data_items = [i for i in items if isinstance(i, str) and i.startswith("data: ")]
    assert data_items == ["data: FALLBACK_SYNTHESIS_OK"]  # streamed exactly once, no duplication
    assert items[-1] is None  # stream terminated exactly once
