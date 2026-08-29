"""Phase 4 automation tests.

Tests:
1. cronScheduler appears in Argus manifest enabled capabilities.
2. cronScheduler appears in Reeve manifest enabled capabilities.
3. The three seed files are valid JSON with required fields.
4. Argus security scan seed uses soul=argus with autonomy=report-only.
5. Reeve planning seed uses soul=reeve with autonomy=report-only.
6. Incident intake seed uses webhookTrigger as the first node type.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SEEDS_DIR = Path(__file__).parent.parent / "seeds" / "phase4"

REQUIRED_SEED_FIELDS = {"id", "name", "nodes", "edges", "nodeParameters"}


# ---------------------------------------------------------------------------
# Manifest tests
# ---------------------------------------------------------------------------

def test_argus_manifest_has_cron_scheduler():
    from services.soul_manifest import get_manifest
    manifest = get_manifest("argus")
    assert "cronScheduler" in manifest.enabled_node_types(), (
        "cronScheduler must be in Argus manifest enabled capabilities"
    )


def test_reeve_manifest_has_cron_scheduler():
    from services.soul_manifest import get_manifest
    manifest = get_manifest("reeve")
    assert "cronScheduler" in manifest.enabled_node_types(), (
        "cronScheduler must be in Reeve manifest enabled capabilities"
    )


# ---------------------------------------------------------------------------
# Seed file validity
# ---------------------------------------------------------------------------

def _load_seed(filename: str) -> dict:
    path = SEEDS_DIR / filename
    assert path.exists(), f"Seed file not found: {path}"
    with path.open() as fh:
        data = json.load(fh)
    return data


@pytest.mark.parametrize("filename", [
    "argus-security-scan-workflow.json",
    "reeve-planning-prompt-workflow.json",
    "argus-incident-intake-workflow.json",
])
def test_seed_has_required_fields(filename):
    data = _load_seed(filename)
    missing = REQUIRED_SEED_FIELDS - set(data.keys())
    assert not missing, f"{filename} is missing fields: {missing}"


@pytest.mark.parametrize("filename", [
    "argus-security-scan-workflow.json",
    "reeve-planning-prompt-workflow.json",
    "argus-incident-intake-workflow.json",
])
def test_seed_nodes_and_edges_are_lists(filename):
    data = _load_seed(filename)
    assert isinstance(data["nodes"], list), f"{filename}: nodes must be a list"
    assert isinstance(data["edges"], list), f"{filename}: edges must be a list"
    assert len(data["nodes"]) >= 1, f"{filename}: must have at least one node"


# ---------------------------------------------------------------------------
# Soul and autonomy assertions
# ---------------------------------------------------------------------------

def test_argus_security_scan_uses_argus_soul_report_only():
    data = _load_seed("argus-security-scan-workflow.json")
    params = data["nodeParameters"]
    soul_params = next(
        (v for v in params.values() if v.get("soul") == "argus"),
        None,
    )
    assert soul_params is not None, "argus security scan must have soul=argus node parameter"
    assert soul_params.get("autonomy") == "report-only", (
        "argus security scan autonomy must be report-only"
    )


def test_reeve_planning_uses_reeve_soul_report_only():
    data = _load_seed("reeve-planning-prompt-workflow.json")
    params = data["nodeParameters"]
    soul_params = next(
        (v for v in params.values() if v.get("soul") == "reeve"),
        None,
    )
    assert soul_params is not None, "reeve planning seed must have soul=reeve node parameter"
    assert soul_params.get("autonomy") == "report-only", (
        "reeve planning seed autonomy must be report-only"
    )


def test_incident_intake_uses_webhook_trigger_as_first_node():
    data = _load_seed("argus-incident-intake-workflow.json")
    nodes = data["nodes"]
    assert nodes[0]["type"] == "webhookTrigger", (
        f"First node must be webhookTrigger, got: {nodes[0]['type']}"
    )
