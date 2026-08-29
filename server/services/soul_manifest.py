"""Soul capability manifest — per-soul declarative tool node allowlist.

Implements Orion D1 (PARAMETRIC topology): one ``soul-dispatch`` workflow
template + per-soul manifests declaring which tool nodes that soul may reach.

Design:
- Manifests are static Python dicts (auditable, diffable, no runtime eval).
- A soul may only receive tool nodes whose ``type`` appears in its manifest.
- ``UNKNOWN_SOUL_MANIFEST`` is the default for souls not in this registry:
  zero capabilities (fail-closed, not fail-open).
- Phase 3 capabilities (email, RAG, calendar, webhook, code) are listed here.

Orion D1 standing rule: manifest hashes are recorded in run records for
auditability. ``manifest_hash(soul_id)`` returns the SHA-256 of the manifest
JSON for inclusion in dispatch logs.

Dragon-gated capabilities are listed but marked ``enabled=False`` — they are
wired but inactive until Dragon's explicit sign-off and Argus re-proof.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class CapabilityEntry:
    node_type: str
    enabled: bool = True
    note: Optional[str] = None


@dataclass(frozen=True)
class SoulManifest:
    soul_id: str
    display_name: str
    capabilities: tuple[CapabilityEntry, ...] = field(default_factory=tuple)

    def enabled_node_types(self) -> frozenset[str]:
        return frozenset(e.node_type for e in self.capabilities if e.enabled)

    def as_dict(self) -> dict:
        return {
            "soul_id": self.soul_id,
            "display_name": self.display_name,
            "capabilities": [
                {"node_type": e.node_type, "enabled": e.enabled, "note": e.note}
                for e in self.capabilities
            ],
        }


def _manifest(soul_id: str, display_name: str, *entries: CapabilityEntry) -> SoulManifest:
    return SoulManifest(soul_id=soul_id, display_name=display_name, capabilities=tuple(entries))


def _cap(node_type: str, enabled: bool = True, note: Optional[str] = None) -> CapabilityEntry:
    return CapabilityEntry(node_type=node_type, enabled=enabled, note=note)


# ---------------------------------------------------------------------------
# Soul manifests — Phase 3 + Phase 1 capabilities
# ---------------------------------------------------------------------------

# Phase 1 (cipher-workforce capability upgrade): simpleMemory is wired as a
# default tool for ALL souls so every soul has persistent recall across
# sessions.  taskManager is added to reeve (PM) so cross-soul task
# coordination is durable.  telegramSend is added to all souls so they can
# send outbound Telegram messages (e.g. status updates back to Dragon).

_REGISTRY: dict[str, SoulManifest] = {
    m.soul_id: m
    for m in [
        _manifest(
            "orion",
            "Orion (CTO)",
            _cap("pythonExecutor"),
            _cap("javascriptExecutor"),
            _cap("typescriptExecutor"),
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            _cap("calendarListEvents"),
            _cap("calendarGetEvent"),
            _cap("webhookTrigger"),
            # Phase 1: persistent recall + outbound Telegram
            _cap("simpleMemory"),
            _cap("telegramSend"),
            # Phase 2: search tools
            _cap("braveSearch"),
            _cap("perplexitySearch"),
        ),
        _manifest(
            "maren",
            "Maren (Backend)",
            _cap("pythonExecutor"),
            _cap("javascriptExecutor"),
            _cap("typescriptExecutor"),
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            _cap("calendarListEvents"),
            _cap("calendarGetEvent"),
            _cap("webhookTrigger"),
            _cap("gcloud", enabled=False, note="Dragon-gated: GCP write scopes — requires sign-off"),
            # Phase 1: persistent recall + outbound Telegram
            _cap("simpleMemory"),
            _cap("telegramSend"),
            # Phase 2: GitHub (backend/infra VCS)
            _cap("githubAction"),
        ),
        _manifest(
            "cael",
            "Cael (Frontend)",
            _cap("pythonExecutor"),
            _cap("javascriptExecutor"),
            _cap("typescriptExecutor"),
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            _cap("calendarListEvents"),
            _cap("calendarGetEvent"),
            # Phase 1: persistent recall + outbound Telegram
            _cap("simpleMemory"),
            _cap("telegramSend"),
            # Phase 2: GitHub + Vercel (frontend VCS + deploy context); no code executor added
            _cap("githubAction"),
            _cap("vercelAction"),
        ),
        _manifest(
            "argus",
            "Argus (CISO)",
            _cap("pythonExecutor"),
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            _cap("webhookTrigger"),
            _cap("calendarListEvents"),
            _cap("calendarGetEvent"),
            _cap("cronScheduler"),
            # Phase 1: persistent recall + outbound Telegram
            _cap("simpleMemory"),
            _cap("telegramSend"),
        ),
        _manifest(
            "vera",
            "Vera (QA)",
            _cap("pythonExecutor"),
            _cap("javascriptExecutor"),
            _cap("typescriptExecutor"),
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            # Phase 1: persistent recall + outbound Telegram
            _cap("simpleMemory"),
            _cap("telegramSend"),
            # Phase 2: browser harness for E2E tests and visual QA
            _cap("browser"),
            _cap("browserHarness"),
        ),
        _manifest(
            "reeve",
            "Reeve (PM)",
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            _cap("calendarListEvents"),
            _cap("calendarGetEvent"),
            _cap("webhookTrigger"),
            _cap("cronScheduler"),
            # Phase 1: persistent recall + durable task coordination + outbound Telegram
            _cap("simpleMemory"),
            _cap("taskManager"),
            _cap("telegramSend"),
        ),
        _manifest(
            "zane",
            "Zane (Full-Stack)",
            _cap("pythonExecutor"),
            _cap("javascriptExecutor"),
            _cap("typescriptExecutor"),
            _cap("ragStore"),
            _cap("ragQuery"),
            _cap("emailRead"),
            _cap("emailSend"),
            _cap("calendarListEvents"),
            _cap("calendarGetEvent"),
            _cap("webhookTrigger"),
            # Phase 1: persistent recall + outbound Telegram
            _cap("simpleMemory"),
            _cap("telegramSend"),
        ),
    ]
}

_UNKNOWN_MANIFEST = SoulManifest(
    soul_id="unknown",
    display_name="Unknown Soul",
    capabilities=(),
)


def get_manifest(soul_id: str) -> SoulManifest:
    """Return the manifest for ``soul_id``, or the zero-capability default."""
    return _REGISTRY.get(soul_id, _UNKNOWN_MANIFEST)


def manifest_hash(soul_id: str) -> str:
    """SHA-256 of the manifest JSON — for inclusion in dispatch run records."""
    manifest = get_manifest(soul_id)
    serialized = json.dumps(manifest.as_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def list_soul_ids() -> list[str]:
    return sorted(_REGISTRY.keys())
