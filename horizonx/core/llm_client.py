"""Shared LLM client for HorizonX internal calls (Summarizer, LLMJudge, SemanticSpin).

Uses the Anthropic SDK with prompt caching enabled. The system prompt block is
marked with cache_control so it stays in cache across calls within the 5-minute
TTL — critical for cost control in long-horizon runs where the summarizer and
validators fire repeatedly.

All callers get structured JSON output via a prefilled assistant turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import anthropic

from horizonx.agents.repair import repair_dangling_tool_calls

logger = logging.getLogger(__name__)

_client: anthropic.AsyncAnthropic | None = None


def get_client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic()
    return _client


async def call_llm_json(
    *,
    system: str,
    user_prompt: str,
    model: str = "claude-haiku-4-5",
    max_tokens: int = 4096,
    temperature: float = 0.0,
    cache_system: bool = True,
) -> dict[str, Any]:
    """Call an Anthropic model and parse the response as JSON.

    Args:
        system: System prompt (cached across calls within 5-min TTL).
        user_prompt: The user-role content.
        model: Model ID (default: haiku for cost).
        max_tokens: Response token cap.
        temperature: Sampling temperature.
        cache_system: Whether to mark system block with cache_control.

    Returns:
        Parsed JSON dict from the assistant response.
    """
    client = get_client()

    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": system,
            **({"cache_control": {"type": "ephemeral"}} if cache_system else {}),
        }
    ]

    messages = [
        {"role": "user", "content": user_prompt},
        {"role": "assistant", "content": "{"},
    ]

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,
        messages=messages,
    )

    raw_text = response.content[0].text if response.content else ""
    json_str = "{" + raw_text

    usage = response.usage
    logger.debug(
        "LLM call: model=%s input=%d output=%d cache_create=%s cache_read=%s",
        model,
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_creation_input_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
    )

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        end = json_str.rfind("}")
        if end != -1:
            try:
                return json.loads(json_str[: end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("LLM returned non-JSON: %s...", json_str[:200])
        return {"error": "json_parse_failed", "raw": json_str[:500]}


async def call_llm_multiturn(
    *,
    system: str,
    messages: list[dict[str, Any]],
    model: str = "claude-haiku-4-5",
    max_tokens: int = 4096,
    temperature: float = 0.0,
    cache_system: bool = True,
) -> dict[str, Any]:
    """Multi-turn variant of call_llm_json with automatic dangling-tool-call repair.

    Repairs the message history before sending so a crashed session cannot
    cause an Anthropic API validation error. Returns the assistant's last
    text response as a parsed JSON dict (prefill approach).
    """
    repaired = repair_dangling_tool_calls(messages)
    if len(repaired) != len(messages):
        logger.warning(
            "repair_dangling_tool_calls injected %d synthetic tool_result(s)",
            len(repaired) - len(messages),
        )

    client = get_client()
    system_blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": system,
            **({"cache_control": {"type": "ephemeral"}} if cache_system else {}),
        }
    ]

    # Append prefill for JSON forcing
    send_messages = repaired + [{"role": "assistant", "content": "{"}]

    response = await client.messages.create(
        model=model,
        max_tokens=max_tokens,
        temperature=temperature,
        system=system_blocks,
        messages=send_messages,
    )

    raw_text = response.content[0].text if response.content else ""
    json_str = "{" + raw_text

    usage = response.usage
    logger.debug(
        "LLM multiturn: model=%s input=%d output=%d cache_create=%s cache_read=%s",
        model,
        usage.input_tokens,
        usage.output_tokens,
        getattr(usage, "cache_creation_input_tokens", None),
        getattr(usage, "cache_read_input_tokens", None),
    )

    try:
        return json.loads(json_str)
    except json.JSONDecodeError:
        end = json_str.rfind("}")
        if end != -1:
            try:
                return json.loads(json_str[: end + 1])
            except json.JSONDecodeError:
                pass
        logger.warning("LLM multiturn returned non-JSON: %s...", json_str[:200])
        return {"error": "json_parse_failed", "raw": json_str[:500]}
