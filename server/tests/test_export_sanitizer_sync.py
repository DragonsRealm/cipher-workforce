"""The export sanitizer must cover everything "clear memory" wipes.

Two lists describe the same concept in two languages:

- ``clear_agent_session_state`` (``services/memory/state.py``) is the
  backend's authoritative definition of "every piece of state an agent
  reuses across a conversation". It resets some fields and removes others.
- ``RUNTIME_KEYS`` (``client/src/utils/parameterSanitizer.ts``) decides what
  never leaves the instance in an exported workflow.

They drifted once already, and silently: the sanitizer held the camelCase
``memoryContent`` while the field is ``memory_content``, so every export
carried the full conversation history -- into the shipped example workflows,
and from there into a public repository.

Cross-language, so it cannot be a type. This test reads both sources and
asserts the second covers the first.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
SANITIZER = REPO_ROOT / "client" / "src" / "utils" / "parameterSanitizer.ts"


def _memory_state_fields() -> set[str]:
    """Field names ``clear_agent_session_state`` resets or removes."""
    from services.memory.state import clear_agent_session_state

    source = inspect.getsource(clear_agent_session_state)

    updates = re.search(r"parameter_updates=\{(.*?)\}", source, re.S)
    removals = re.search(r"remove_parameters=\((.*?)\)", source, re.S)
    assert updates and removals, (
        "Could not locate parameter_updates / remove_parameters in "
        "clear_agent_session_state. If that function was restructured, update "
        "this extraction -- do not delete the test, the invariant still holds."
    )

    fields = set(re.findall(r'"([a-z_]+)"', updates.group(1)))
    fields |= set(re.findall(r'"([a-z_]+)"', removals.group(1)))
    return fields


def _normalize(key: str) -> str:
    return re.sub(r"[_\-\s]", "", key.lower())


def _sanitizer_runtime_keys() -> set[str]:
    """The normalized RUNTIME_KEYS entries declared in the TS sanitizer."""
    assert SANITIZER.is_file(), f"sanitizer not found at {SANITIZER}"
    source = SANITIZER.read_text(encoding="utf-8")

    block = re.search(r"const RUNTIME_KEYS = normalizedSet\(\[(.*?)\]\)", source, re.S)
    assert block, "Could not locate the RUNTIME_KEYS array in parameterSanitizer.ts"

    # Strip `//` comments first. Prose in there legitimately contains
    # apostrophes ("the author's own session"), and a naive quote-pair scan
    # would treat one as a string delimiter and mis-read the whole list.
    body = re.sub(r"//[^\n]*", "", block.group(1))
    return {_normalize(k) for k in re.findall(r"'([^']+)'", body)}


class TestExportSanitizerCoversConversationalState:
    def test_extraction_actually_found_something(self):
        """Guard the guard: a silently-empty set would pass everything."""
        backend = _memory_state_fields()
        assert len(backend) >= 5, backend
        assert len(_sanitizer_runtime_keys()) >= 5

    def test_every_cleared_field_is_stripped_from_exports(self):
        missing = sorted(
            field
            for field in _memory_state_fields()
            if _normalize(field) not in _sanitizer_runtime_keys()
        )
        assert not missing, (
            f"{missing} are wiped by clear_agent_session_state -- they are "
            "conversational state -- but the export sanitizer does not strip "
            f"them. Add them to RUNTIME_KEYS in {SANITIZER.name}."
        )

    @pytest.mark.parametrize(
        "field",
        [
            "memory_content",
            "memory_jsonl",
            "last_session_id",
            "vertex_interaction_id",
            "vertex_environment_id",
        ],
    )
    def test_known_conversational_fields_are_named_explicitly(self, field):
        """Pin the individual names, so a regression reads clearly."""
        assert _normalize(field) in _sanitizer_runtime_keys()

    def test_sanitizer_compares_normalized_keys(self):
        """The camelCase/snake_case seam that caused the original leak."""
        source = SANITIZER.read_text(encoding="utf-8")
        assert "function normalizeKey" in source, (
            "parameterSanitizer.ts must fold case and drop separators before "
            "comparing. Exact-match Set.has() is what let `memoryContent` "
            "shadow `memory_content` and leak every conversation."
        )
