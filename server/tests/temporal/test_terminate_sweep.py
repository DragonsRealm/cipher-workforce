"""Visibility-sweep retry in TemporalClientWrapper.terminate_running_workflows.

The boot-time terminate sweep issues a Visibility ``list_workflows`` query
that races shard acquisition right after server start
(visibility-queue-processor reports "shard status unknown" until shard-1
is acquired). The sweep now retries the query on transient errors instead
of being silently abandoned for that boot; per-workflow terminate failures
are still swallowed (idempotent best-effort).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from services.temporal.client import TemporalClientWrapper


def _agen(items=None, raise_exc=None):
    """Return a fresh async generator that yields ``items`` or raises."""

    async def _g():
        if raise_exc is not None:
            raise raise_exc
        for it in items or []:
            yield it

    return _g()


def _make_wrapper(client: MagicMock, *, attempts: int = 4) -> TemporalClientWrapper:
    w = TemporalClientWrapper("localhost:5681", "default")
    w._client = client
    w._sweep_attempts = attempts
    w._sweep_backoff_seconds = 0.0
    return w


def _client_with(list_side_effect) -> MagicMock:
    c = MagicMock()
    c.list_workflows = MagicMock(side_effect=list_side_effect)
    handle = MagicMock()
    handle.terminate = AsyncMock()
    c.get_workflow_handle = MagicMock(return_value=handle)
    return c


async def test_sweep_terminates_all_running():
    wfs = [SimpleNamespace(id="wf1", run_id="r1"), SimpleNamespace(id="wf2", run_id="r2")]
    client = _client_with([_agen(items=wfs)])
    w = _make_wrapper(client)

    count = await w.terminate_running_workflows()

    assert count == 2
    assert client.list_workflows.call_count == 1
    assert client.get_workflow_handle.return_value.terminate.await_count == 2


async def test_sweep_retries_transient_then_succeeds():
    client = _client_with(
        [
            _agen(raise_exc=Exception('error="shard status unknown"')),
            _agen(items=[SimpleNamespace(id="wf1", run_id="r1")]),
        ]
    )
    w = _make_wrapper(client, attempts=4)

    count = await w.terminate_running_workflows()

    assert count == 1
    assert client.list_workflows.call_count == 2


async def test_sweep_propagates_non_transient():
    client = _client_with([_agen(raise_exc=Exception("genuine visibility bug"))])
    w = _make_wrapper(client, attempts=4)

    with pytest.raises(Exception, match="genuine visibility bug"):
        await w.terminate_running_workflows()

    assert client.list_workflows.call_count == 1
