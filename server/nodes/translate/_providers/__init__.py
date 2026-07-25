"""Translate provider plugins — importing this package registers them all.

Adding a provider:

1. Add a ``providers.<name>`` block to ``server/config/translate_defaults.json``
   with the capability sub-blocks it serves.
2. Write ``<name>.py`` implementing the matching Protocol(s) from
   :mod:`nodes.translate._protocol`, subclassing
   :class:`nodes.translate._providers._http.HttpTranslateProvider` for a plain
   HTTP API — or nothing at all, as ``llm.py`` shows.
3. Call the matching ``register_*_provider`` at the bottom of the module.
4. Add the side-effect import below.
5. Add its credential id to ``credential_providers.json`` plus a ``Credential``
   subclass, unless it reuses one another plugin already declares.

Register only into the capabilities the provider actually serves. That is the
whole point of three registries: a translate-only vendor is then unreachable
from the other two nodes rather than failing inside them.

**Never import a provider SDK at module level** — ``sdk_exception_refs`` are
lazy ``"module:ClassName"`` strings so registration stays free.
"""

from __future__ import annotations

# Side-effect imports. Order is documentation only; the module cache makes
# duplicate registration impossible.
from nodes.translate._providers import deepl  # noqa: F401
from nodes.translate._providers import sarvam  # noqa: F401
from nodes.translate._providers import llm  # noqa: F401
