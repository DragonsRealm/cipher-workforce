"""Phase 5 capability tests — RAG pipeline and Stripe wiring.

Covers:
1. RAG pipeline (vectorStore + documentParser) — present and enabled for all
   six souls: orion, maren, cael, argus, vera, reeve.
2. Stripe wiring — stripeAction and stripeReceive present in maren's manifest
   AND explicitly disabled (Dragon-gated, enabled=False).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# 1. RAG pipeline — vectorStore + documentParser in every soul's manifest
# ---------------------------------------------------------------------------

_RAG_SOULS = ["orion", "maren", "cael", "argus", "vera", "reeve"]
_RAG_CAPABILITIES = ["vectorStore", "documentParser"]


class TestPhase5RagPipeline:
    """vectorStore and documentParser are enabled in every named soul's manifest."""

    @pytest.mark.parametrize("soul_id", _RAG_SOULS)
    def test_vector_store_enabled(self, soul_id: str):
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        assert "vectorStore" in m.enabled_node_types(), (
            f"Soul '{soul_id}' is missing enabled vectorStore capability (Phase 5 RAG)"
        )

    @pytest.mark.parametrize("soul_id", _RAG_SOULS)
    def test_document_parser_enabled(self, soul_id: str):
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        assert "documentParser" in m.enabled_node_types(), (
            f"Soul '{soul_id}' is missing enabled documentParser capability (Phase 5 RAG)"
        )

    @pytest.mark.parametrize("soul_id", _RAG_SOULS)
    def test_rag_capabilities_additive_not_replacing(self, soul_id: str):
        """Existing capabilities (ragStore, ragQuery) still present alongside new RAG nodes."""
        from services.soul_manifest import get_manifest

        m = get_manifest(soul_id)
        enabled = m.enabled_node_types()
        assert "ragStore" in enabled, (
            f"Soul '{soul_id}' lost existing ragStore capability after Phase 5 additions"
        )
        assert "ragQuery" in enabled, (
            f"Soul '{soul_id}' lost existing ragQuery capability after Phase 5 additions"
        )

    def test_soul_namespace_isolation_guard_present_in_vector_store_node(self):
        """vectorStore node calls reject_caller_soul_prefix() — isolation guard is wired."""
        import inspect
        from nodes.document.vector_store import VectorStoreNode

        src = inspect.getsource(VectorStoreNode)
        assert "reject_caller_soul_prefix" in src, (
            "vectorStore node must call reject_caller_soul_prefix() to enforce soul namespace isolation"
        )


# ---------------------------------------------------------------------------
# 2. Stripe wiring — maren only, both nodes present AND disabled
# ---------------------------------------------------------------------------

class TestPhase5StripeMaren:
    """Stripe capability is wired in maren's manifest with enabled=False (Dragon-gated)."""

    def test_stripe_action_in_maren_manifest(self):
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        all_types = {e.node_type for e in m.capabilities}
        assert "stripeAction" in all_types, (
            "stripeAction is missing from maren's manifest — Phase 5 requires it to be wired"
        )

    def test_stripe_receive_in_maren_manifest(self):
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        all_types = {e.node_type for e in m.capabilities}
        assert "stripeReceive" in all_types, (
            "stripeReceive is missing from maren's manifest — Phase 5 requires it to be wired"
        )

    def test_stripe_action_is_disabled(self):
        """stripeAction must be enabled=False (Dragon-gated)."""
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        entry = next((e for e in m.capabilities if e.node_type == "stripeAction"), None)
        assert entry is not None, "stripeAction not found in maren's capabilities"
        assert entry.enabled is False, (
            f"stripeAction must be disabled (Dragon-gated) but found enabled={entry.enabled}"
        )

    def test_stripe_receive_is_disabled(self):
        """stripeReceive must be enabled=False (Dragon-gated)."""
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        entry = next((e for e in m.capabilities if e.node_type == "stripeReceive"), None)
        assert entry is not None, "stripeReceive not found in maren's capabilities"
        assert entry.enabled is False, (
            f"stripeReceive must be disabled (Dragon-gated) but found enabled={entry.enabled}"
        )

    def test_stripe_not_in_enabled_set(self):
        """Stripe nodes must NOT appear in enabled_node_types() for maren."""
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        enabled = m.enabled_node_types()
        assert "stripeAction" not in enabled, (
            "stripeAction is in maren's enabled_node_types() — it must remain Dragon-gated"
        )
        assert "stripeReceive" not in enabled, (
            "stripeReceive is in maren's enabled_node_types() — it must remain Dragon-gated"
        )

    def test_stripe_not_wired_to_non_maren_souls(self):
        """Stripe must not appear in any other soul's manifest."""
        from services.soul_manifest import get_manifest

        non_maren = ["orion", "cael", "argus", "vera", "reeve"]
        for soul_id in non_maren:
            m = get_manifest(soul_id)
            all_types = {e.node_type for e in m.capabilities}
            assert "stripeAction" not in all_types, (
                f"stripeAction unexpectedly found in {soul_id}'s manifest"
            )
            assert "stripeReceive" not in all_types, (
                f"stripeReceive unexpectedly found in {soul_id}'s manifest"
            )

    def test_stripe_note_references_workforce_prefix(self):
        """Stripe capability notes must reference WORKFORCE_STRIPE_* prefix, not STRIPE_*."""
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        for node_type in ("stripeAction", "stripeReceive"):
            entry = next((e for e in m.capabilities if e.node_type == node_type), None)
            assert entry is not None
            note = entry.note or ""
            assert "WORKFORCE_STRIPE_" in note, (
                f"{node_type} note must reference WORKFORCE_STRIPE_* prefix "
                f"(not bare STRIPE_*) but got: {note!r}"
            )
