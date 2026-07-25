"""Workflow id -> workspace directory, in one place.

The workspace directory is named by ``Workflow.slug``, which is **mutable**
(renaming a workflow renames the directory). Every stable reference — a
``FileRef``, the file-serving route, a WebSocket request from the editor —
carries the immutable ``Workflow.id`` instead, so the id must be translated
to a slug against the database on every use.

That translation existed in two copies before this module (the workspace
router and ``WorkflowService``), and a third consumer was about to be added.
One implementation means one place to change when the naming rule changes,
and one place where the ``"default"`` fallback below is reasoned about.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from core.logging import get_logger
from core.paths import workspaces_dir

logger = get_logger(__name__)

# The anonymous workspace a one-off run without a saved workflow row writes
# into. Reads may fall back to it; mutations must not (see below).
DEFAULT_WORKSPACE_SLUG = "default"


async def resolve_workflow_slug(workflow_id: Optional[str], database: Any) -> Optional[str]:
    """Look up a workflow's slug. ``None`` when it cannot be resolved.

    Never raises: a database hiccup during a read should degrade to the
    default workspace rather than fail the request.
    """
    if not workflow_id:
        return None
    try:
        workflow = await database.get_workflow(workflow_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "workspace slug lookup failed",
            workflow_id=workflow_id,
            error=str(exc),
        )
        return None
    return getattr(workflow, "slug", None) if workflow else None


async def resolve_workspace_root(
    workflow_id: Optional[str],
    database: Any,
    *,
    allow_default: bool = True,
) -> Path:
    """Resolve a workflow id to its on-disk workspace directory.

    ``allow_default=False`` is mandatory for any **mutating** caller.

    With the fallback enabled, an id that does not resolve — a stale tab, a
    typo, a workflow deleted in another window — silently lands in the shared
    anonymous workspace. For a read that is harmless. For a delete or a
    rename it means destroying files belonging to a different context than
    the caller believes they are operating on, which is the only irreversible
    failure this surface can produce.
    """
    slug = await resolve_workflow_slug(workflow_id, database)
    if slug is None:
        if not allow_default:
            from services.plugin import NodeUserError

            raise NodeUserError(
                "This workflow could not be found, so its workspace cannot be "
                "modified. Save the workflow and try again."
            )
        slug = DEFAULT_WORKSPACE_SLUG
    return workspaces_dir() / slug


__all__ = [
    "DEFAULT_WORKSPACE_SLUG",
    "resolve_workflow_slug",
    "resolve_workspace_root",
]
