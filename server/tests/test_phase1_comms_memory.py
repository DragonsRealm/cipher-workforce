"""Phase 1 capability tests — Telegram comms + simpleMemory + taskManager.

Covers:
1. telegramSend enabled for reeve, maren, orion only.
2. Souls that should NOT gain telegramSend (cael, vera, zane, argus) don't.
3. telegramReceive (inbound trigger) wired to reeve via the
   telegram_soul_dispatch seed workflow.
4. simpleMemory enabled for all six souls (per-session durable recall).
5. taskManager enabled for reeve only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_ALL_SOULS = ["orion", "maren", "cael", "argus", "vera", "reeve", "zane"]
_TELEGRAM_SEND_SOULS = ["reeve", "maren", "orion"]
_NO_TELEGRAM_SEND_SOULS = ["cael", "vera", "zane", "argus"]
_SIMPLE_MEMORY_SOULS = ["orion", "maren", "cael", "argus", "vera", "zane", "reeve"]

_SEED_PATH = (
    Path(__file__).resolve().parents[1]
    / "seeds"
    / "phase4"
    / "telegram-soul-dispatch-workflow.json"
)


class TestPhase1TelegramSend:
    """telegramSend is enabled only for reeve, maren, orion."""

    @pytest.mark.parametrize("soul_id", _TELEGRAM_SEND_SOULS)
    def test_telegram_send_enabled(self, soul_id: str):
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        assert "telegramSend" in m.enabled_node_types(), (
            f"Soul '{soul_id}' is missing enabled telegramSend capability (Phase 1)"
        )

    @pytest.mark.parametrize("soul_id", _NO_TELEGRAM_SEND_SOULS)
    def test_telegram_send_not_granted(self, soul_id: str):
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        assert "telegramSend" not in m.enabled_node_types(), (
            f"Soul '{soul_id}' unexpectedly gained telegramSend — scoped to reeve/maren/orion only"
        )


class TestPhase1TelegramTrigger:
    """telegramReceive (inbound) is wired to reeve via the seed workflow."""

    def test_telegram_receive_present_in_seed(self):
        assert _SEED_PATH.exists(), f"Missing seed workflow: {_SEED_PATH}"
        data = json.loads(_SEED_PATH.read_text())
        node_types = {n["type"] for n in data["nodes"]}
        assert "telegramReceive" in node_types

    def test_telegram_receive_routes_to_reeve(self):
        data = json.loads(_SEED_PATH.read_text())
        soul_nodes = [n for n in data["nodes"] if n["type"] == "dcsSoul"]
        assert soul_nodes, "Seed workflow has no dcsSoul node"
        soul_id = soul_nodes[0]["id"]
        params = data["nodeParameters"][soul_id]
        assert params["soul"] == "reeve", (
            "Telegram trigger workflow must dispatch to reeve (coordination soul)"
        )


class TestPhase1SimpleMemory:
    """simpleMemory (durable per-session recall) is enabled for all six souls."""

    @pytest.mark.parametrize("soul_id", _SIMPLE_MEMORY_SOULS)
    def test_simple_memory_enabled(self, soul_id: str):
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        assert "simpleMemory" in m.enabled_node_types(), (
            f"Soul '{soul_id}' is missing enabled simpleMemory capability (Phase 1)"
        )


class TestPhase1TaskManager:
    """taskManager (durable cross-soul task tracking) is enabled for reeve only."""

    def test_task_manager_enabled_for_reeve(self):
        from services.soul_manifest import get_manifest

        m = get_manifest("reeve")
        assert "taskManager" in m.enabled_node_types(), (
            "Reeve is missing enabled taskManager capability (Phase 1 PM tool)"
        )

    @pytest.mark.parametrize("soul_id", [s for s in _ALL_SOULS if s != "reeve"])
    def test_task_manager_not_granted_to_others(self, soul_id: str):
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        assert "taskManager" not in m.enabled_node_types(), (
            f"Soul '{soul_id}' unexpectedly gained taskManager — scoped to reeve only"
        )
