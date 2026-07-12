"""Chat-agent resilience: Anthropic retry/backoff, a persistent-rate-limit circuit
breaker, and an OpenAI-compatible provider fallback.

This module is intentionally self-host-owned and decoupled from ``agentic.py`` (the
upstream-tracked native tool-use loop). Upstream owns *how to stream one agent turn*;
this module owns *how to make it resilient*. Anthropic/tool primitives that live in
``agentic.py`` (``CORE_TOOLS``, the tool executor, the display-name helper) are passed
in as arguments so this module never imports from ``agentic.py`` — no import cycle, and
no in-function imports (per backend rules). Keeping this out of ``agentic.py`` means
upstream merges of that file never collide with our fallback logic.
"""

import asyncio
import logging
import os
import random
import time
from typing import Any, Callable, List, Optional, Tuple

import anthropic
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from utils.llm.providers import OPENAI_COMPATIBLE_PROVIDERS, get_or_create_openai_compatible_llm
from utils.retrieval.safety import AgentSafetyGuard, SafetyGuardError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config / error classification
# ---------------------------------------------------------------------------

RETRYABLE_ANTHROPIC_STATUS = {429, 500, 502, 503, 504, 529}
RATE_LIMIT_ANTHROPIC_STATUS = {429, 529}
MAX_BACKOFF_SECONDS = 20.0
SUPPORTED_FALLBACK_PROVIDERS = {'openai', 'openrouter'}


def retry_attempts() -> int:
    """Max Anthropic attempts per turn before falling back (>= 1)."""
    try:
        return max(1, int(os.getenv('CHAT_AGENT_RETRY_ATTEMPTS', '3')))
    except (TypeError, ValueError):
        return 3


def fallback_enabled() -> bool:
    """Provider fallback is on unless explicitly disabled."""
    return os.getenv('CHAT_AGENT_FALLBACK_ENABLED', 'true').strip().lower() != 'false'


def force_fallback() -> bool:
    """Testing hook: force the provider fallback path on the first turn."""
    return os.getenv('CHAT_AGENT_FORCE_FALLBACK', '').strip().lower() == 'true'


def fallback_chain() -> List[Tuple[str, str]]:
    """Ordered (provider, model) fallbacks parsed from CHAT_AGENT_FALLBACK_CHAIN.

    Default keeps quality parity: the same Claude model via OpenRouter, then
    OpenAI gpt-4.1. Only providers we can build (openai/openrouter) are kept.
    """
    raw = os.getenv('CHAT_AGENT_FALLBACK_CHAIN', 'openrouter:anthropic/claude-sonnet-4-6,openai:gpt-4.1')
    chain: List[Tuple[str, str]] = []
    for part in raw.split(','):
        part = part.strip()
        if not part or ':' not in part:
            continue
        provider, _, model = part.partition(':')
        provider = provider.strip().lower()
        model = model.strip()
        if provider in SUPPORTED_FALLBACK_PROVIDERS and model:
            chain.append((provider, model))
    return chain


def provider_has_credentials(provider: str) -> bool:
    """True when the provider's configured API-key env var is set (non-empty).

    Reads the authoritative env-var name from ``OPENAI_COMPATIBLE_PROVIDERS`` so this
    check never drifts from how the provider client is actually built.
    """
    config = OPENAI_COMPATIBLE_PROVIDERS.get(provider)
    if config is None:
        return False
    return bool(os.getenv(config.api_key_env, '').strip())


def is_retryable_anthropic_error(e: Exception) -> bool:
    """True for transient Anthropic failures worth retrying (429/5xx/connection)."""
    if isinstance(e, (anthropic.RateLimitError, anthropic.APIConnectionError, anthropic.APITimeoutError)):
        return True
    if isinstance(e, anthropic.APIStatusError):
        return getattr(e, 'status_code', None) in RETRYABLE_ANTHROPIC_STATUS
    return False


def is_rate_limit_error(e: Exception) -> bool:
    """True only for Anthropic rate-limit/overload (429/529) — drives the circuit breaker."""
    if isinstance(e, anthropic.RateLimitError):
        return True
    if isinstance(e, anthropic.APIStatusError):
        return getattr(e, 'status_code', None) in RATE_LIMIT_ANTHROPIC_STATUS
    return False


def retry_after_seconds(e: Exception) -> Optional[float]:
    """Server-provided Retry-After (seconds) or retry-after-ms, if present on the response."""
    response = getattr(e, 'response', None)
    headers = getattr(response, 'headers', None) or {}
    try:
        ra = headers.get('retry-after')
        if ra is not None:
            return float(ra)
        ra_ms = headers.get('retry-after-ms')
        if ra_ms is not None:
            return float(ra_ms) / 1000.0
    except (TypeError, ValueError):
        return None
    return None


def backoff_delay(attempt: int, e: Exception) -> float:
    """Exponential backoff with jitter, capped; prefers the server's Retry-After."""
    server = retry_after_seconds(e)
    if server is not None and server >= 0:
        return min(server, MAX_BACKOFF_SECONDS)
    base = min(MAX_BACKOFF_SECONDS, 0.5 * (2 ** (attempt - 1)))
    return base + random.uniform(0, 0.5)


# ---------------------------------------------------------------------------
# Persistent-rate-limit circuit breaker
#
# On this self-host the Anthropic org cap is hit continuously, so retrying every
# message just adds latency before the inevitable fallback. After N consecutive
# rate-limit failures the breaker trips: new turns skip Anthropic and go straight
# to the fallback provider for a cooldown window, then re-probe. Per-process and in
# memory (single event loop, so a plain dict is race-free); any success resets it.
# ---------------------------------------------------------------------------

_breaker = {'consecutive_rate_limits': 0, 'skip_until': 0.0}


def _breaker_trip_threshold() -> int:
    try:
        return max(1, int(os.getenv('CHAT_AGENT_ANTHROPIC_TRIP_THRESHOLD', '2')))
    except (TypeError, ValueError):
        return 2


def _breaker_cooldown_seconds() -> float:
    try:
        return max(0.0, float(os.getenv('CHAT_AGENT_ANTHROPIC_COOLDOWN_SECONDS', '60')))
    except (TypeError, ValueError):
        return 60.0


def record_anthropic_rate_limit() -> None:
    """Count a rate-limit failure; trip the breaker once the threshold is reached."""
    _breaker['consecutive_rate_limits'] += 1
    if _breaker['consecutive_rate_limits'] >= _breaker_trip_threshold():
        _breaker['skip_until'] = time.monotonic() + _breaker_cooldown_seconds()


def record_anthropic_success() -> None:
    """Any Anthropic success clears the breaker (also used by tests to reset state)."""
    _breaker['consecutive_rate_limits'] = 0
    _breaker['skip_until'] = 0.0


def should_skip_anthropic() -> bool:
    """True while the breaker is tripped (skip Anthropic, use the fallback directly)."""
    return time.monotonic() < _breaker['skip_until']


# ---------------------------------------------------------------------------
# Retry orchestration around a single streaming turn
# ---------------------------------------------------------------------------


async def run_anthropic_turn_with_retry(
    stream_turn: Callable,
    *,
    callback,
    emitted_flag: list,
    already_emitted: bool,
    max_attempts: int,
):
    """Run one Anthropic streaming turn with retry/backoff.

    ``stream_turn`` is a zero-arg callable returning the turn coroutine; it must set
    ``emitted_flag[0]=True`` as soon as any assistant text streams. Retries only while
    nothing has streamed (this attempt or a prior turn via ``already_emitted``), so
    visible tokens are never duplicated. Records circuit-breaker success/rate-limit as a
    side effect. Returns the final message on success; re-raises the last error for the
    caller to handle (typically provider fallback).
    """
    attempt = 0
    while True:
        attempt += 1
        emitted_flag[0] = False
        try:
            result = await stream_turn()
            record_anthropic_success()
            return result
        except Exception as e:
            # Text already on screen (this attempt or a prior turn) -> caller must abort.
            if emitted_flag[0] or already_emitted:
                raise
            if is_rate_limit_error(e):
                record_anthropic_rate_limit()
            if is_retryable_anthropic_error(e) and attempt < max_attempts:
                delay = backoff_delay(attempt, e)
                logger.warning(
                    "Anthropic %s on chat_agent; retry %d/%d in %.1fs",
                    type(e).__name__,
                    attempt,
                    max_attempts,
                    delay,
                )
                if attempt == 1:
                    await callback.put_thought("High demand — retrying")
                await asyncio.sleep(delay)
                continue
            raise  # exhausted or non-retryable -> caller decides fallback


# ---------------------------------------------------------------------------
# Provider fallback (OpenAI-compatible), used when Anthropic stays unavailable
# ---------------------------------------------------------------------------


def coerce_text(content: Any) -> str:
    """Flatten LangChain message content (str or list-of-blocks) to plain text."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get('type') == 'text':
                parts.append(block.get('text', ''))
        return ''.join(parts)
    return '' if content is None else str(content)


def anthropic_msgs_to_langchain(anthropic_messages: list) -> list:
    """Convert first-turn Anthropic messages (plain text turns) to LangChain messages.

    The provider fallback only runs before any tools/tool_results are appended, so
    every content here is a simple string.
    """
    converted = []
    for m in anthropic_messages:
        content = m.get('content')
        if not isinstance(content, str):
            content = coerce_text(content)
        if m.get('role') == 'assistant':
            converted.append(AIMessage(content=content))
        else:
            converted.append(HumanMessage(content=content))
    return converted


def langchain_tool_to_openai(lc_tool) -> dict:
    """Convert a LangChain @tool to OpenAI function-tool schema (drops config/title)."""
    schema = lc_tool.args_schema.schema()
    properties = {k: v for k, v in schema.get('properties', {}).items() if k != 'config'}
    required = [r for r in schema.get('required', []) if r != 'config']
    cleaned_properties = {}
    for k, v in properties.items():
        cleaned_properties[k] = {pk: pv for pk, pv in v.items() if pk != 'title'}
    return {
        "type": "function",
        "function": {
            "name": lc_tool.name,
            "description": lc_tool.description,
            "parameters": {
                "type": "object",
                "properties": cleaned_properties,
                "required": required,
            },
        },
    }


async def run_openai_fallback_agent(
    system_prompt: str,
    anthropic_messages: list,
    tool_registry: dict,
    callback,
    full_response: list,
    safety_guard: AgentSafetyGuard,
    configurable: dict,
    *,
    core_tools: list,
    execute_tool: Callable,
    get_tool_display_name: Callable,
) -> bool:
    """Degraded-mode answer via an OpenAI-compatible provider when Anthropic is down.

    Runs a NON-streaming tool loop over the core tools only (app/deferred tools and
    Anthropic server tools like web_search are omitted), reusing the caller's tool
    registry / executor / configurable so citations still populate, then streams the
    final text to the client. ``core_tools``, ``execute_tool`` and
    ``get_tool_display_name`` are injected from ``agentic.py`` to avoid an import cycle.

    Returns True if any provider produced an answer (caller should stop), False if
    every provider in the chain failed (caller emits the generic error).
    """
    chain = fallback_chain()
    if not chain:
        return False

    # Only attempt providers whose API-key env var is actually set. Otherwise a
    # misconfigured chain (e.g. the default lists OpenRouter first but only
    # OPENAI_API_KEY is set) would burn a guaranteed-failing round-trip and surface a
    # confusing generic error instead of a clear, actionable "no fallback" signal.
    usable, skipped = [], []
    for provider, model in chain:
        (usable if provider_has_credentials(provider) else skipped).append((provider, model))
    if skipped:
        logger.info(
            "chat_agent fallback skipping providers without credentials: %s",
            sorted({p for p, _ in skipped}),
        )
    if not usable:
        required_envs = sorted(
            {OPENAI_COMPATIBLE_PROVIDERS[p].api_key_env for p, _ in chain if p in OPENAI_COMPATIBLE_PROVIDERS}
        )
        logger.error(
            "chat_agent fallback unavailable: none of the configured providers have credentials. "
            "Set one of %s, or set CHAT_AGENT_FALLBACK_CHAIN to a provider you have a key for.",
            required_envs,
        )
        return False

    base_messages = [SystemMessage(content=system_prompt)] + anthropic_msgs_to_langchain(anthropic_messages)
    openai_tools = [langchain_tool_to_openai(t) for t in core_tools]
    try:
        max_iters = max(1, int(os.getenv('CHAT_AGENT_FALLBACK_MAX_ITERS', '8')))
    except (TypeError, ValueError):
        max_iters = 8

    for provider, model in usable:
        try:
            llm = get_or_create_openai_compatible_llm(provider, model, streaming=False)
            llm_with_tools = llm.bind_tools(openai_tools)
            convo = list(base_messages)
            answered = False

            for _ in range(max_iters):
                ai = await llm_with_tools.ainvoke(convo)
                convo.append(ai)
                tool_calls = getattr(ai, 'tool_calls', None) or []
                if not tool_calls:
                    text = coerce_text(ai.content)
                    if text:
                        full_response.append(text)
                        await callback.put_data(text)
                    answered = True
                    break

                for tc in tool_calls:
                    name = tc.get('name')
                    args = tc.get('args') or {}
                    tc_id = tc.get('id')
                    tool_obj = tool_registry.get(name)
                    try:
                        safety_guard.validate_tool_call(name, args)
                    except SafetyGuardError as sg:
                        convo.append(ToolMessage(content=f"Blocked: {sg}", tool_call_id=tc_id))
                        continue
                    await callback.put_thought(get_tool_display_name(name, tool_obj))
                    try:
                        result = await execute_tool(name, args, tool_registry, configurable)
                    except Exception as te:
                        logger.error(f"Fallback tool execution error ({name}): {te}")
                        result = f"Error executing tool: {te}"
                    convo.append(ToolMessage(content=str(result), tool_call_id=tc_id))

            if not answered:
                # Exhausted the tool loop without a final answer — don't hang the stream.
                await callback.put_data("\n\nSorry, I couldn't complete that just now. Please try again.")
            logger.info("chat_agent provider fallback answered via %s/%s", provider, model)
            await callback.end()
            return True
        except Exception as e:
            logger.warning("chat_agent fallback provider %s/%s failed: %s", provider, model, e)
            continue

    return False
