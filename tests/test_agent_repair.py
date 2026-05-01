"""Tests for horizonx.agents.repair."""

from __future__ import annotations

import pytest

from horizonx.agents.repair import repair_dangling_tool_calls


def _asst(tool_uses: list[tuple[str, str]]) -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tid, "name": name, "input": {}}
            for tid, name in tool_uses
        ],
    }


def _result(tool_use_id: str) -> dict:
    return {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": tool_use_id, "content": "ok"}
        ],
    }


class TestRepairDanglingToolCalls:
    def test_no_op_when_clean(self):
        msgs = [_asst([("t1", "Bash")]), _result("t1")]
        assert repair_dangling_tool_calls(msgs) == msgs

    def test_empty_list(self):
        assert repair_dangling_tool_calls([]) == []

    def test_single_dangling_call(self):
        msgs = [_asst([("t1", "Read")])]
        repaired = repair_dangling_tool_calls(msgs)
        assert len(repaired) == 2
        injected = repaired[1]
        assert injected["role"] == "user"
        block = injected["content"][0]
        assert block["type"] == "tool_result"
        assert block["tool_use_id"] == "t1"
        assert block["is_error"] is True

    def test_two_dangling_calls_in_one_message(self):
        msgs = [_asst([("t1", "Read"), ("t2", "Write")])]
        repaired = repair_dangling_tool_calls(msgs)
        assert len(repaired) == 2
        ids = {b["tool_use_id"] for b in repaired[1]["content"]}
        assert ids == {"t1", "t2"}

    def test_first_dangling_second_satisfied(self):
        msgs = [
            _asst([("t1", "Bash"), ("t2", "Read")]),
            _result("t2"),
        ]
        repaired = repair_dangling_tool_calls(msgs)
        assert len(repaired) == 3
        injected_ids = {b["tool_use_id"] for b in repaired[1]["content"]}
        assert injected_ids == {"t1"}

    def test_multiple_assistant_messages_each_with_dangling(self):
        msgs = [
            _asst([("t1", "Bash")]),
            _asst([("t2", "Write")]),
        ]
        repaired = repair_dangling_tool_calls(msgs)
        assert len(repaired) == 4

    def test_does_not_mutate_original(self):
        msgs = [_asst([("t1", "Bash")])]
        original_len = len(msgs)
        repair_dangling_tool_calls(msgs)
        assert len(msgs) == original_len

    def test_no_tool_use_blocks(self):
        msgs = [{"role": "assistant", "content": "hello"}]
        assert repair_dangling_tool_calls(msgs) == msgs

    def test_content_is_string_not_list(self):
        # Some CLI events emit content as a plain string — must not crash
        msgs = [{"role": "assistant", "content": "plain text response"}]
        assert repair_dangling_tool_calls(msgs) == msgs
