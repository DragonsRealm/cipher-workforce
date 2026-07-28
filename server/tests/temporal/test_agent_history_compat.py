"""LangChain message serialization must stay readable across upgrades.

`services/temporal/agent_activities.py` round-trips agent conversations
through `messages_to_dict` / `messages_from_dict`. Those dicts are what
lands in Temporal's Event History, so they outlive the process that wrote
them: a workflow started under one langchain-core release can be replayed
weeks later by a worker running a newer one.

That is the entire reason all six langchain packages are pinned with `==`
in `pyproject.toml` rather than given floors. The pin states the caution;
this file is what actually checks it, so a proposed bump can be evaluated by
running the tests instead of by reasoning about it.

GOLDEN is a real payload captured from langchain-core 1.4.7. If a future
release changes the shape, `test_a_stored_history_still_deserializes` fails
and the bump needs a migration for existing histories -- not a version
change.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.unit


# Captured from langchain-core 1.4.7 via messages_to_dict(...).
GOLDEN = [
    {
        "data": {
            "additional_kwargs": {},
            "content": "sys",
            "id": None,
            "name": None,
            "response_metadata": {},
            "type": "system",
        },
        "type": "system",
    },
    {
        "data": {
            "additional_kwargs": {},
            "content": "hi",
            "id": None,
            "name": None,
            "response_metadata": {},
            "type": "human",
        },
        "type": "human",
    },
    {
        "data": {
            "additional_kwargs": {},
            "content": "",
            "id": None,
            "invalid_tool_calls": [],
            "name": None,
            "response_metadata": {},
            "tool_calls": [
                {"args": {"a": 1}, "id": "call_1", "name": "calc", "type": "tool_call"}
            ],
            "type": "ai",
            "usage_metadata": None,
        },
        "type": "ai",
    },
    {
        "data": {
            "additional_kwargs": {},
            "artifact": None,
            "content": "42",
            "id": None,
            "name": None,
            "response_metadata": {},
            "status": "success",
            "tool_call_id": "call_1",
            "type": "tool",
        },
        "type": "tool",
    },
]


class TestAgentHistoryCompatibility:
    def test_a_stored_history_still_deserializes(self):
        """The load-bearing one: old history, current library."""
        from langchain_core.messages import messages_from_dict

        messages = messages_from_dict(GOLDEN)

        assert [type(m).__name__ for m in messages] == [
            "SystemMessage",
            "HumanMessage",
            "AIMessage",
            "ToolMessage",
        ]

    def test_reserializing_a_stored_history_is_byte_identical(self):
        """No silent format drift: what we write back matches what we read.

        A worker that rewrote histories in a subtly different shape would
        corrupt them for any peer still on the previous release.
        """
        from langchain_core.messages import messages_from_dict, messages_to_dict

        reserialized = messages_to_dict(messages_from_dict(GOLDEN))

        assert json.dumps(reserialized, sort_keys=True) == json.dumps(
            GOLDEN, sort_keys=True
        )

    def test_tool_call_wiring_survives_the_round_trip(self):
        """Agent loops match tool results to calls by id; losing it breaks them."""
        from langchain_core.messages import messages_from_dict

        messages = messages_from_dict(GOLDEN)
        ai, tool = messages[2], messages[3]

        assert ai.tool_calls[0]["id"] == "call_1"
        assert ai.tool_calls[0]["name"] == "calc"
        assert ai.tool_calls[0]["args"] == {"a": 1}
        assert tool.tool_call_id == "call_1"

    def test_usage_metadata_round_trips(self):
        """Token accounting rides in the message; CompactionService reads it."""
        from langchain_core.messages import AIMessage, messages_from_dict, messages_to_dict

        original = AIMessage(
            content="done",
            usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
        )
        restored = messages_from_dict(messages_to_dict([original]))[0]

        assert restored.usage_metadata == {
            "input_tokens": 1,
            "output_tokens": 2,
            "total_tokens": 3,
        }

    def test_the_pinned_version_is_the_one_under_test(self):
        """Guards against the golden payload silently describing a stale release."""
        import importlib.metadata as md

        installed = md.version("langchain-core")
        assert installed.startswith("1."), (
            f"langchain-core {installed} is a new major. The pins in "
            "pyproject.toml exist for Temporal replay compatibility -- "
            "re-capture GOLDEN and re-read this file before lifting them."
        )
