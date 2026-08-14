"""Locks the single-standard end state of the agent wire/engine layer.

The v1/v2 duality (legacy ``llm_engine`` branches, ``message_wire_v2``
naming, wire ``version`` discriminator) was purged deliberately. This test
keeps it from creeping back — same pattern as ``test_langchain_removed.py``.
"""

from pathlib import Path

SERVER_ROOT = Path(__file__).resolve().parents[2]

# Retired identifiers that must not reappear in production source.
_FORBIDDEN = (
    "LEGACY_LLM_ENGINE",
    "NATIVE_LLM_ENGINE",
    "has_recorded_engine",
    "message_wire_v2",
    "MessageWireV2",
    "MESSAGE_WIRE_VERSION",
    "message_wire_version",
    "reconstruct_message_wire_v2",
)

_PRODUCTION_DIRS = ("services", "nodes", "models", "routers", "core")


def _production_files():
    for directory in _PRODUCTION_DIRS:
        root = SERVER_ROOT / directory
        if not root.is_dir():
            continue
        yield from root.rglob("*.py")


def test_no_versioned_wire_or_engine_identifiers_in_production_source():
    offenders: list[str] = []
    for path in _production_files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for name in _FORBIDDEN:
            if name in text:
                offenders.append(f"{path.relative_to(SERVER_ROOT)}: {name}")
    assert not offenders, (
        "retired v1/v2 identifiers found in production source:\n"
        + "\n".join(offenders)
    )


def test_message_wire_has_no_version_key():
    from services.llm.protocol import Message, message_to_wire

    wire = message_to_wire(Message(role="user", content="hi"))
    assert "version" not in wire
    assert "wire_version" not in wire
