"""Shape validation for plugin identifiers used in URL routes + lookups.

Plugin node types (and credential provider ids) follow Python-identifier
rules: a letter or underscore followed by word characters. URL-facing
routes (``GET /api/schemas/nodes/{node_type}/icon`` etc.) and internal
helpers (``nodes._visuals.get_plugin_icon_path`` etc.) both validate
against this constraint to reject path-traversal injections at the
boundary.

Why this lives here (not inline at each callsite):

- One source of truth for the regex pattern. FastAPI consumes the raw
  pattern string via ``Path(pattern=...)``; Python code consumes the
  pre-compiled validator via :func:`is_valid_node_type`. Keeping the
  pattern in one module guarantees the route-level and function-level
  checks stay in lockstep.
- States the constraint explicitly for readers and for static analysis.
  Note it does **not** silence CodeQL's ``py/path-injection``: the
  icon-resolution alerts this was introduced for are still reported on
  every scan and were closed by manual dismissal. The sink CodeQL flags
  is the path construction itself, so a ``fullmatch`` in a helper does
  not act as a barrier for it. Keep the check -- it is correct and cheap
  -- but do not expect an alert to disappear because of it.
  (Rule: https://codeql.github.com/codeql-query-help/python/py-path-injection/)
"""

from __future__ import annotations

import re
from typing import Final


# Letter/underscore then word chars — the shape every registered
# ``BaseNode.type`` and ``Credential.id`` follows. Exposed as a raw
# string so FastAPI's ``Path(pattern=...)`` / ``Query(pattern=...)``
# can consume it directly.
NODE_TYPE_PATTERN: Final[str] = r"^[A-Za-z_][A-Za-z0-9_]*$"

_NODE_TYPE_RE: Final[re.Pattern[str]] = re.compile(NODE_TYPE_PATTERN)


def is_valid_node_type(value: str) -> bool:
    """True if ``value`` matches :data:`NODE_TYPE_PATTERN`.

    Treats ``None`` / non-string input as invalid (defensive — the
    type hint is ``str`` but callers may forward URL params verbatim).
    """
    if not isinstance(value, str):
        return False
    return bool(_NODE_TYPE_RE.fullmatch(value))


__all__ = ["NODE_TYPE_PATTERN", "is_valid_node_type"]
