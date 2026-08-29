"""Per-soul memory namespace helpers.

Each soul gets its own isolated memory namespace. Cross-soul reads are
structurally impossible: the namespace is derived server-side from the
workflow metadata, never from model-provided content.

Namespace format: ``soul_<soul_id>``
where ``soul_id`` is the sanitized soul profile-id string from the workflow
manifest (e.g. ``upstagefullstack``, ``backend``, ``frontend``).

Cross-soul isolation test contract (test_soul_memory_isolation.py):
  Soul A query -> returns zero chunks from Soul B.
"""
from __future__ import annotations

import re

_SAFE = re.compile(r"[^a-zA-Z0-9_]")

# Prefix for all soul-scoped collections. Never change this once data is
# written — it is part of the collection name on-disk in ChromaDB.
_SOUL_PREFIX = "soul_"


def soul_collection_name(soul_id: str) -> str:
    """Return the ChromaDB collection name for a given soul.

    The name is ``soul_<sanitized_soul_id>``.
    Sanitization: non-alphanumeric/underscore chars replaced with ``_``.
    Empty or blank soul_id maps to ``soul_unknown``.
    """
    safe = _SAFE.sub("_", soul_id.strip()).strip("_") or "unknown"
    return f"{_SOUL_PREFIX}{safe}"


def soul_namespace(soul_id: str) -> str:
    """Return the MemoryScope namespace string for a given soul.

    Alias of soul_collection_name; use whichever reads clearer at the call
    site.
    """
    return soul_collection_name(soul_id)


def reject_caller_soul_prefix(name: str) -> None:
    """Raise ``ValueError`` if *name* starts with the reserved soul prefix.

    Souls address their own ChromaDB collections via their bound soul_id
    passed to :func:`soul_collection_name`; they must never construct the
    ``soul_`` prefix themselves.  A caller-supplied collection name that
    begins with ``soul_`` is treated as an impersonation attempt and
    rejected here at the node level, satisfying the Orion Phase-2
    Condition 2 guard.

    Usage (inside any node that accepts a caller-supplied collection_name)::

        from services.memory.soul_namespace import reject_caller_soul_prefix
        reject_caller_soul_prefix(params.collection_name)
    """
    if name.startswith(_SOUL_PREFIX):
        raise ValueError(
            f"Collection name {name!r} begins with the reserved prefix "
            f"{_SOUL_PREFIX!r}. Souls must address their collections via "
            "their server-bound soul_id, not by constructing the prefix "
            "directly. Use soul_collection_name(soul_id) instead."
        )


def soul_id_from_workflow_slug(slug: str) -> str | None:
    """Derive a soul_id from a workflow slug, if the slug follows the
    DCS soul-dispatch naming convention (``<soul_id>-<task_slug>``).

    Returns None when the slug does not match the convention — callers
    should fall back to a default namespace in that case.
    """
    if not slug:
        return None
    # Convention: first hyphen-separated segment is the soul_id.
    # Example: "upstagefullstack-phase1" -> "upstagefullstack"
    parts = slug.split("-", 1)
    if parts:
        candidate = parts[0].strip()
        if candidate:
            return candidate
    return None
