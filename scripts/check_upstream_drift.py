#!/usr/bin/env python3
"""Fork upstream drift check.

Compares the local fork of zeenie-ai/OpenCompany against the pinned baseline
SHA. Reports new commits on the upstream default branch since the pin.

Usage:
    python3 scripts/check_upstream_drift.py [--json] [--fail-on-drift]

Exits 0 if no drift, 1 if drift detected and --fail-on-drift is set.

Environment:
    GITHUB_TOKEN — optional; avoids GitHub API rate limits.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Fork provenance — DO NOT CHANGE without updating docs-internal/CIPHER_WORKFORCE_FORK.md
# ---------------------------------------------------------------------------
UPSTREAM_OWNER = "zeenie-ai"
UPSTREAM_REPO = "OpenCompany"
BASELINE_SHA = "49d667e2"  # Short SHA; full SHA recorded in CIPHER_WORKFORCE_FORK.md
BASELINE_DATE = "2026-08-28"  # Date the fork was cut


def _github_api(path: str) -> dict | list:
    url = f"https://api.github.com{path}"
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def _compare(base: str, head: str) -> dict:
    """Return GitHub compare result between base and head SHA/ref."""
    return _github_api(
        f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}/compare/{base}...{head}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Emit JSON output")
    parser.add_argument(
        "--fail-on-drift",
        action="store_true",
        help="Exit 1 when upstream has new commits",
    )
    args = parser.parse_args()

    print(
        f"[drift-check] Checking {UPSTREAM_OWNER}/{UPSTREAM_REPO} "
        f"against baseline {BASELINE_SHA} (cut {BASELINE_DATE})",
        file=sys.stderr,
    )

    try:
        # Fetch the default branch head.
        repo_info = _github_api(f"/repos/{UPSTREAM_OWNER}/{UPSTREAM_REPO}")
        default_branch = repo_info.get("default_branch", "main")
        compare = _compare(BASELINE_SHA, default_branch)
    except Exception as exc:
        msg = f"[drift-check] ERROR fetching upstream: {exc}"
        if args.json:
            print(json.dumps({"error": str(exc), "drift": None}))
        else:
            print(msg, file=sys.stderr)
        return 0  # Network failures are not treated as drift.

    ahead = compare.get("ahead_by", 0)
    commits = compare.get("commits", [])

    result = {
        "baseline_sha": BASELINE_SHA,
        "baseline_date": BASELINE_DATE,
        "upstream": f"{UPSTREAM_OWNER}/{UPSTREAM_REPO}",
        "default_branch": default_branch,
        "new_commits": ahead,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "commits": [
            {
                "sha": c["sha"][:8],
                "message": c["commit"]["message"].splitlines()[0][:120],
                "author": c["commit"]["author"]["name"],
                "date": c["commit"]["author"]["date"],
            }
            for c in commits[:20]  # cap at 20 for readability
        ],
    }

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        if ahead == 0:
            print(
                f"[drift-check] OK — upstream is at baseline ({BASELINE_SHA}). "
                "No new commits."
            )
        else:
            print(
                f"[drift-check] DRIFT DETECTED — {ahead} new commit(s) on "
                f"{UPSTREAM_OWNER}/{UPSTREAM_REPO}/{default_branch} since {BASELINE_SHA}:"
            )
            for c in result["commits"]:
                print(f"  {c['sha']} {c['date'][:10]}  {c['message']}")
            if len(commits) > 20:
                print(f"  ... and {ahead - 20} more.")

    if ahead > 0 and args.fail_on_drift:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
