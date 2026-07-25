"""Contract for the workflow-id -> workspace-directory translation.

The directory on disk is named by ``Workflow.slug``, which the rename path
moves; every stable reference carries the immutable ``Workflow.id``. One
module owns that translation so the naming rule and the ``"default"``
fallback are reasoned about once.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from services import workspace_locator
from services.workspace_locator import (
    DEFAULT_WORKSPACE_SLUG,
    resolve_workflow_slug,
    resolve_workspace_root,
)

pytestmark = pytest.mark.unit

WORKFLOW_ID = "019f99e2dc997cf390193ad3e6260de1"
SLUG = "My_Workflow_1"


@pytest.fixture
def roots(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace_locator, "workspaces_dir", lambda: tmp_path)
    return tmp_path


def _db(slug=SLUG):
    database = AsyncMock()
    database.get_workflow.return_value = SimpleNamespace(slug=slug) if slug else None
    return database


class TestResolution:
    async def test_uses_the_slug_not_the_id(self, roots):
        root = await resolve_workspace_root(WORKFLOW_ID, _db())

        assert root == roots / SLUG
        assert WORKFLOW_ID not in str(root)

    async def test_missing_row_falls_back_to_default_for_reads(self, roots):
        assert await resolve_workspace_root(WORKFLOW_ID, _db(slug=None)) == (
            roots / DEFAULT_WORKSPACE_SLUG
        )

    async def test_database_failure_degrades_rather_than_raising(self, roots):
        database = AsyncMock()
        database.get_workflow.side_effect = RuntimeError("db down")

        assert await resolve_workflow_slug(WORKFLOW_ID, database) is None
        assert await resolve_workspace_root(WORKFLOW_ID, database) == (
            roots / DEFAULT_WORKSPACE_SLUG
        )


class TestMutationsRefuseTheDefaultFallback:
    """The only irreversible failure this surface can produce.

    With the fallback enabled, an id that does not resolve — a stale tab, a
    workflow deleted in another window — silently lands in the shared
    anonymous workspace. Harmless for a read; for a delete it destroys files
    belonging to a different context than the caller believes.
    """

    async def test_unresolvable_workflow_is_refused(self, roots):
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            await resolve_workspace_root(WORKFLOW_ID, _db(slug=None), allow_default=False)

    async def test_missing_workflow_id_is_refused(self, roots):
        from services.plugin import NodeUserError

        with pytest.raises(NodeUserError):
            await resolve_workspace_root("", _db(), allow_default=False)

    async def test_a_resolvable_workflow_still_works(self, roots):
        root = await resolve_workspace_root(WORKFLOW_ID, _db(), allow_default=False)

        assert root == roots / SLUG
