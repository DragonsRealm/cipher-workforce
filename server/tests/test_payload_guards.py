"""Guards on how much data a node result may carry.

A node result is copied many times — three writes to ``node_outputs``, two
broadcasts, the retained status cache, the workflow aggregate, every
downstream activity's *input*, and (for a tool-exposed node) an LLM message.
Temporal caps a blob at 2 MiB for activity results and inputs alike.

Two separate mechanisms, tested here together because they defend the same
thing from different ends: the producer refuses to emit an uncarryable
payload, and the retained cache refuses to hold a large one forever.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest
from pydantic import BaseModel

from services.media.limits import (
    TEMPORAL_PAYLOAD_ERROR_BYTES,
    TEMPORAL_PAYLOAD_WARN_BYTES,
)
from services.plugin import NodeUserError
from services.plugin.base import BaseNode

pytestmark = pytest.mark.unit


class _Output(BaseModel):
    blob: str = ""

    model_config = {"extra": "allow"}


class _Stand_in:
    """Enough of a node for the size guard, without registering one.

    Subclassing ``ActionNode`` here would auto-register a fake node type into
    the global ``NODE_METADATA`` via ``__init_subclass__`` and break every
    plugin-contract invariant that walks the registry — which is exactly what
    happened the first time this file was written.
    """

    type = "_test_sized_node"
    Output = _Output

    _serialize_result = BaseNode._serialize_result
    _check_result_size = BaseNode._check_result_size


def _SizedNode() -> _Stand_in:
    return _Stand_in()


class TestResultSizeGuard:
    def test_a_normal_result_passes_untouched(self):
        node = _SizedNode()
        payload = node._serialize_result(_Output(blob="hello"))
        assert payload == {"blob": "hello"}

    def test_a_large_but_carryable_result_only_warns(self, caplog):
        """Existing nodes legitimately return hundreds of KB.

        Failing them would be a regression, so between the two thresholds
        this logs and nothing more.
        """
        node = _SizedNode()
        size = (TEMPORAL_PAYLOAD_WARN_BYTES + TEMPORAL_PAYLOAD_ERROR_BYTES) // 2
        payload = node._serialize_result(_Output(blob="x" * size))
        assert payload["blob"]

    def test_an_uncarryable_result_is_refused(self):
        """Not a new failure — Temporal rejects this payload anyway.

        What changes is that it fails once, here, with a message naming the
        node and the size, instead of three times inside the converter with a
        generic error. ``NodeUserError`` is already non-retryable.
        """
        node = _SizedNode()
        with pytest.raises(NodeUserError) as excinfo:
            node._serialize_result(_Output(blob="x" * (TEMPORAL_PAYLOAD_ERROR_BYTES + 1)))

        message = str(excinfo.value)
        assert "_test_sized_node" in message
        assert "workspace" in message.lower()

    def test_the_refusal_is_non_retryable(self):
        """The whole point of raising NodeUserError rather than anything else."""
        from services.temporal._retry_policies import NON_RETRYABLE_ERROR_TYPES

        assert NodeUserError.__name__ in NON_RETRYABLE_ERROR_TYPES

    def test_sizing_failure_never_blocks_a_result(self, monkeypatch):
        """Sizing is diagnostics; an unserializable payload still passes."""
        node = _SizedNode()

        class _Hostile:
            def __repr__(self):
                raise RuntimeError("nope")

        # Not a BaseModel and no declared Output match, so it passes through
        # _check_result_size, which must swallow its own failure.
        assert node._check_result_size({"x": _Hostile()}) is None


class TestStatusCacheElision:
    """``_status`` is never evicted and is replayed to every new client."""

    def test_a_small_output_is_cached_verbatim(self):
        from services.status_broadcaster import _elide_for_cache

        payload = {"result": "small"}
        assert _elide_for_cache(payload) is payload

    def test_a_large_output_is_replaced_by_a_stub(self):
        from services.status_broadcaster import _elide_for_cache

        elided = _elide_for_cache({"blob": "x" * (256 * 1024)})
        assert elided["_elided"] is True
        assert elided["_size_bytes"] > 256 * 1024
        assert "node_outputs" in elided["_note"]

    async def test_the_broadcast_still_carries_the_full_output(self):
        """Only the retained copy is elided — the live view is untouched."""
        from services.status_broadcaster import StatusBroadcaster

        broadcaster = StatusBroadcaster()
        sent: list[Dict[str, Any]] = []

        async def _capture(message):
            sent.append(message)

        broadcaster.broadcast = _capture  # type: ignore[method-assign]

        big = {"blob": "y" * (256 * 1024)}
        await broadcaster.update_node_output("node-1", big, workflow_id="wf-1")

        assert sent[0]["output"] == big
        cached = broadcaster._status["nodes"]["node-1"]["output"]
        assert cached["_elided"] is True

    def test_unsizeable_output_is_kept_rather_than_discarded(self):
        """Unknown size is not a reason to throw data away."""
        from services.status_broadcaster import _elide_for_cache

        class _Unserializable:
            pass

        value = {"x": _Unserializable()}
        assert _elide_for_cache(value) is value
