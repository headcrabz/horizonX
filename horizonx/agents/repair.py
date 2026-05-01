"""Repair malformed message histories before Anthropic API calls.

A dangling tool call is an assistant message with a tool_use content block
that has no matching tool_result in a subsequent message. This arises when
a session crashes mid-tool-call. The API rejects such histories with a
validation error, causing resume to fail silently.

Usage:
    from horizonx.agents.repair import repair_dangling_tool_calls
    messages = repair_dangling_tool_calls(messages)
"""
from __future__ import annotations


def repair_dangling_tool_calls(messages: list[dict]) -> list[dict]:
    """Return a copy of *messages* with synthetic tool_results injected for any
    tool_use blocks that have no matching tool_result downstream.

    Does not mutate the input list.
    """
    if not messages:
        return messages

    # Pass 1: collect every tool_use_id that already has a result.
    satisfied: set[str] = set()
    for msg in messages:
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                tid = block.get("tool_use_id", "")
                if tid:
                    satisfied.add(tid)

    # Pass 2: find assistant messages whose tool_use blocks are not satisfied.
    # Record (message_index, list_of_dangling_blocks).
    repairs: list[tuple[int, list[dict]]] = []
    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue
        content = msg.get("content", [])
        if not isinstance(content, list):
            continue
        dangling = [
            b for b in content
            if isinstance(b, dict)
            and b.get("type") == "tool_use"
            and b.get("id") not in satisfied
        ]
        if dangling:
            repairs.append((i, dangling))

    if not repairs:
        return messages

    # Pass 3: build synthetic user messages and insert them immediately after
    # each offending assistant message. Process in reverse so earlier indices
    # stay valid as we grow the list.
    result = list(messages)
    for insert_after, dangling_blocks in reversed(repairs):
        synthetic_content = [
            {
                "type": "tool_result",
                "tool_use_id": b["id"],
                "content": (
                    "Error: tool call was interrupted — the session crashed before a result "
                    "was recorded. Please retry the operation."
                ),
                "is_error": True,
            }
            for b in dangling_blocks
        ]
        synthetic_msg = {"role": "user", "content": synthetic_content}
        result.insert(insert_after + 1, synthetic_msg)

    return result
