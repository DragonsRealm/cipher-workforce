"""Central handler for node visuals (icon + color).

Two icon sources co-exist by design (per RFC §6.5):

1. **Per-plugin ``icon.svg``** co-located with the plugin folder
   (e.g. ``server/nodes/telegram/icon.svg``). Resolved at runtime via
   :func:`get_plugin_icon_path`. Preferred for new plugins; served
   by ``GET /api/schemas/nodes/{type}/icon`` (see ``routers/schemas.py``).

2. **Per-plugin ``meta.json``** for library references that need no
   artwork at all — ``{"icons": {"<node_type>": "lucide:Send"}}``.
   Resolved via :func:`get_plugin_icon_ref`. Keeps a plugin's visual
   surface inside its own folder, the same way ``color`` already lives
   there, while reusing an icon set the frontend already bundles.

3. **``visuals.json``** for emoji and library entries belonging to
   plugins that predate per-folder ``meta.json``. Resolved via
   :func:`get_icon` / :func:`get_color`.

``BaseNode._metadata_dict`` tries the co-located SVG, then the plugin's
own ``meta.json``, then ``visuals.json``. The frontend resolver
dispatches by the wire-format prefix (URL paths route to ``<img>``;
``lucide:``, ``lobehub:``, ``asset:`` and emoji each have their own
branch).

Adding a new node, in order of preference:
- Declare ``icons`` / ``icon`` in the plugin's ``meta.json`` pointing at
  a library glyph (nothing to draw, nothing to maintain), OR
- Drop ``icon.svg`` into the plugin folder when the node needs artwork
  a library does not have — typically a brand mark, OR
- Add an entry to ``visuals.json`` (legacy central registry).

Node files do NOT declare ``icon`` or ``color`` themselves.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Dict, Optional

from services.plugin.identifiers import is_valid_node_type


_VISUALS_PATH = Path(__file__).resolve().parent / "visuals.json"


def _load() -> Dict[str, Dict[str, str]]:
    if not _VISUALS_PATH.exists():
        return {}
    with _VISUALS_PATH.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict):
        return {}
    return data


# Loaded once at import; the JSON is small (<5 KB) and editing it
# requires a backend restart to refresh, same as any other node-spec
# metadata change.
_VISUALS: Dict[str, Dict[str, str]] = _load()


def get_icon(node_type: str) -> str:
    """Return the registered icon for ``node_type`` or empty string.

    Icon strings follow the same wire format the frontend's
    ``resolveIcon`` understands: emoji, ``asset:<key>``, or
    ``lobehub:<brand>``.
    """
    entry = _VISUALS.get(node_type)
    if not entry:
        return ""
    return str(entry.get("icon", ""))


def get_color(node_type: str) -> str:
    """Return the registered color for ``node_type`` or empty string.

    Color strings are arbitrary CSS color literals — the canvas node
    components apply them as-is to gradients, borders, and badges.

    Falls back to ``visuals.json`` for legacy entries that haven't been
    migrated to per-plugin ``meta.json`` yet (F2 cleanup of the plugin
    authoring RFC).
    """
    entry = _VISUALS.get(node_type)
    if not entry:
        return ""
    return str(entry.get("color", ""))


def get_plugin_icon_ref(node_type: str) -> str:
    """Return a library/emoji icon reference from the plugin's ``meta.json``.

    The file-based chain (:func:`get_plugin_icon_path`) only answers with an
    SVG on disk. This is its no-file counterpart, so a plugin can point at
    an icon that already exists in a library instead of vendoring artwork:

        {"color": "#128C7E",
         "icons": {"whatsappBusinessSend": "lucide:Send"}}

    Two levels, mirroring the ``icon_<node_type>.svg`` / ``icon.svg`` pair:

    1. ``icons[<node_type>]`` — per-node-type, for folders that serve
       several node types from one plugin directory.
    2. ``icon`` — one reference for every node type in the folder.

    Values use the same wire format the frontend's resolver understands
    (``lucide:<Name>``, ``lobehub:<brand>``, emoji). ``lucide`` names are
    matched case-insensitively against the package's own exports, so they
    are the export identifier (``CheckCheck``), not the kebab-case file
    name (``check-check``), which would not resolve.

    Returns ``""`` when nothing is declared, so callers can fall through to
    :func:`get_icon`. Note a folder that also ships ``icon.svg`` never
    reaches here: the file wins. A plugin choosing library references
    should not vendor an SVG as well.
    """
    meta = get_plugin_meta(node_type)
    if not isinstance(meta, dict):
        return ""
    per_type = meta.get("icons")
    if isinstance(per_type, dict):
        value = per_type.get(node_type)
        if value:
            return str(value)
    return str(meta.get("icon") or "")


def get_plugin_meta(node_type: str, key: Optional[str] = None) -> Optional[dict | str]:
    """Read the plugin's co-located ``meta.json`` file.

    Same folder-resolution path as :func:`get_plugin_icon_path` — uses
    :func:`inspect.getfile` on the plugin class to locate the folder,
    then loads ``meta.json`` if present.

    Returns the value at ``key`` (str) when given, the whole dict when
    ``key`` is ``None``, or ``None`` when the file or key is absent.
    Callers fall back to :func:`get_color` / other ``visuals.json`` keys
    for legacy entries.

    Per RFC §6.2 / F2 of the deferred follow-ups: ``meta.json`` mirrors
    ``icon.svg`` co-location so a plugin's entire visual surface area
    lives in one folder. The previous central ``visuals.json`` color
    map remains as a transitional fallback for entries without a
    per-plugin ``meta.json``.
    """
    from services.node_registry import get_node_class

    if not is_valid_node_type(node_type):
        return None
    cls = get_node_class(node_type)
    if cls is None:
        return None
    try:
        plugin_dir = Path(inspect.getfile(cls)).resolve().parent
    except (TypeError, OSError):
        return None
    meta_path = plugin_dir / "meta.json"
    if not meta_path.exists():
        return None
    try:
        with meta_path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    if key is None:
        return data
    value = data.get(key)
    return None if value is None else str(value)


def get_plugin_icon_path(node_type: str, variant: str = "light") -> Optional[Path]:
    """Return the on-disk path to a plugin's co-located icon SVG.

    ``variant="dark"`` looks for the dark-mode SVG first and falls back
    to the light variant when no dark file exists.

    Resolution chain (first hit wins):
    1. **Per-node-type icon** — ``<plugin_dir>/icon_<node_type>.svg``
       (or ``icon_<node_type>.dark.svg`` for ``variant="dark"``).
       Lets multi-node-per-plugin folders (whatsapp / telegram / stripe)
       serve distinct icons per node type WITHOUT splitting the folder.
       e.g. ``server/nodes/whatsapp/icon_whatsappSend.svg`` serves the
       ``whatsappSend`` node while sharing the folder with
       ``whatsappReceive`` and ``whatsappDb``.
    2. **Shared plugin icon** — ``<plugin_dir>/icon.svg`` (or
       ``icon.dark.svg``). The original Phase 9 contract; one icon for
       the whole folder.

    Folder resolution itself:
    1. Look up the plugin class via :func:`services.node_registry.get_node_class`.
    2. Resolve the class's source file via ``inspect.getfile``.
    3. The plugin folder is the file's parent directory — equally
       correct for single-file plugins (``server/nodes/tool/calc.py``
       → parent ``server/nodes/tool/``) and self-contained-folder
       plugins (``server/nodes/telegram/telegram_send.py`` → parent
       ``server/nodes/telegram/``).

    Returns ``None`` when the type is unknown or no icon SVG is
    present — caller falls back to :func:`get_icon` (visuals.json).
    """
    # Local import to avoid a top-level circular dep (node_registry
    # itself doesn't import _visuals, but plugin modules import both).
    from services.node_registry import get_node_class

    if not is_valid_node_type(node_type):
        return None
    cls = get_node_class(node_type)
    if cls is None:
        return None
    try:
        plugin_dir = Path(inspect.getfile(cls)).resolve().parent
    except (TypeError, OSError):
        return None

    # Candidate filenames in resolution order. Per-node-type first so
    # multi-node folders can override the shared icon for specific node
    # types. ``node_type`` is already constrained to ``NODE_TYPE_PATTERN``
    # (``^[A-Za-z_][A-Za-z0-9_]*$``) by ``is_valid_node_type`` above --
    # no path separators possible -- but we resolve the candidate and
    # call ``Path.relative_to(plugin_dir)`` (which raises ``ValueError``
    # on traversal). CodeQL's taint tracker recognises
    # ``Path.relative_to`` as a ``py/path-injection`` sanitizer; the
    # ``is_relative_to`` predicate it does not.
    candidates: list[str] = []
    if variant == "dark":
        candidates.append(f"icon_{node_type}.dark.svg")
    candidates.append(f"icon_{node_type}.svg")
    if variant == "dark":
        candidates.append("icon.dark.svg")
    candidates.append("icon.svg")

    for name in candidates:
        candidate = (plugin_dir / name).resolve()
        try:
            candidate.relative_to(plugin_dir)
        except ValueError:
            continue
        if candidate.exists():
            return candidate
    return None


def get_skill(node_type: str) -> str:
    """Return the teaching skill folder name registered for ``node_type``.

    Many tool / utility nodes have a paired skill in ``server/skills/``
    that documents how an AI agent should use them. The ``skill`` field
    in ``visuals.json`` is the reverse lookup consumed by
    ``services.auto_skill`` to decide what to do when a tool node is
    connected to an AI agent.
    """
    entry = _VISUALS.get(node_type)
    if not entry:
        return ""
    return str(entry.get("skill", ""))
