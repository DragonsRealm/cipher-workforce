"""
CIPHER OS — Human Approval Panel

GET  /approvals           — Serves the HTML approval panel (no auth required; browser page load)
GET  /api/approvals       — List pending approvals (Bearer token required)
POST /api/approvals/{id}/approve — Approve a pending dispatch (Bearer token required)
POST /api/approvals/{id}/reject  — Reject a pending dispatch  (Bearer token required)

Security (Argus Gate 3): only a human approver holding the correct Bearer token may
grant or reject. No agent reaches this surface — it is served as a browser page and
requires interactive token entry.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Auth helper — Bearer token gate
# ---------------------------------------------------------------------------

# The approval token is set via CIPHER_APPROVAL_TOKEN env var.
# If unset the surface is disabled (all API calls return 503).

_APPROVAL_TOKEN_ENV = "CIPHER_APPROVAL_TOKEN"


def _get_token() -> str | None:
    return os.environ.get(_APPROVAL_TOKEN_ENV)


def _require_bearer(authorization: str = Header(default="")) -> str:
    token = _get_token()
    if token is None:
        raise HTTPException(
            status_code=503,
            detail=f"{_APPROVAL_TOKEN_ENV} is not configured on this server.",
        )
    scheme, _, provided = authorization.partition(" ")
    if scheme.lower() != "bearer" or provided.strip() != token:
        raise HTTPException(status_code=401, detail="Invalid or missing Bearer token.")
    return provided.strip()


# ---------------------------------------------------------------------------
# DB helpers (read-only path to governor store)
# ---------------------------------------------------------------------------

_DB_PATH = os.path.expanduser("~/.cipheros/soul_approvals.db")

STATE_PENDING = "PENDING"
APPROVAL_TTL_SECONDS = 1800  # mirrors governor.py


def _open_db() -> sqlite3.Connection:
    if not os.path.exists(_DB_PATH):
        return None  # type: ignore[return-value]
    conn = sqlite3.connect(_DB_PATH, timeout=5, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _list_pending() -> List[Dict[str, Any]]:
    conn = _open_db()
    if conn is None:
        return []
    try:
        now = time.time()
        rows = conn.execute(
            """
            SELECT approval_id, root_exec_id, soul, state,
                   created_at, expires_at, notes
            FROM soul_approvals
            WHERE state = ?
              AND expires_at > ?
            ORDER BY created_at ASC
            """,
            (STATE_PENDING, now),
        ).fetchall()
        result = []
        for row in rows:
            notes: Dict[str, Any] = {}
            try:
                notes = json.loads(row["notes"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(
                {
                    "approval_id": row["approval_id"],
                    "root_exec_id": row["root_exec_id"],
                    "soul": row["soul"],
                    "state": row["state"],
                    "created_at": row["created_at"],
                    "expires_at": row["expires_at"],
                    "autonomy": notes.get("autonomy", "write"),
                    "context": notes.get("context", {}),
                }
            )
        return result
    finally:
        conn.close()


def _set_state(approval_id: str, new_state: str) -> bool:
    """Transition a PENDING row to new_state. Returns True on success."""
    conn = _open_db()
    if conn is None:
        return False
    try:
        with conn:
            cur = conn.execute(
                "UPDATE soul_approvals SET state=? "
                "WHERE approval_id=? AND state=?",
                (new_state, approval_id, STATE_PENDING),
            )
            return cur.rowcount == 1
    except sqlite3.Error as exc:
        logger.error("approval state update failed: %s", exc)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------


@router.get("/api/approvals", response_class=JSONResponse)
async def list_approvals(_token: str = Depends(_require_bearer)):
    """Return all pending (non-expired) approval requests."""
    return {"approvals": _list_pending()}


@router.post("/api/approvals/{approval_id}/approve", response_class=JSONResponse)
async def approve_dispatch(
    approval_id: str,
    _token: str = Depends(_require_bearer),
):
    """Approve a pending soul dispatch."""
    pending = _list_pending()
    matching = [r for r in pending if r["approval_id"] == approval_id]
    if not matching:
        raise HTTPException(
            status_code=409,
            detail="Approval not found or already actioned.",
        )
    soul = matching[0]["soul"]
    ok = _set_state(approval_id, "APPROVED")
    if not ok:
        raise HTTPException(status_code=409, detail="Already actioned.")
    logger.info("Approval APPROVED: %s (soul=%s)", approval_id, soul)
    return {"status": "approved", "approval_id": approval_id, "soul": soul}


@router.post("/api/approvals/{approval_id}/reject", response_class=JSONResponse)
async def reject_dispatch(
    approval_id: str,
    _token: str = Depends(_require_bearer),
):
    """Reject (deny) a pending soul dispatch."""
    pending = _list_pending()
    matching = [r for r in pending if r["approval_id"] == approval_id]
    if not matching:
        raise HTTPException(
            status_code=409,
            detail="Approval not found or already actioned.",
        )
    soul = matching[0]["soul"]
    ok = _set_state(approval_id, "DENIED")
    if not ok:
        raise HTTPException(status_code=409, detail="Already actioned.")
    logger.info("Approval DENIED: %s (soul=%s)", approval_id, soul)
    return {"status": "rejected", "approval_id": approval_id, "soul": soul}


# ---------------------------------------------------------------------------
# Browser panel — GET /approvals
# ---------------------------------------------------------------------------

_PANEL_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CIPHER OS — Soul Dispatch Approvals</title>
<style>
  :root {
    --bg:       #080a0e;
    --surface:  #0f1318;
    --border:   #1e2530;
    --teal:     #00d4aa;
    --gold:     #f5a623;
    --red:      #e05252;
    --muted:    #556070;
    --text:     #c8d0dc;
    --font-mono: "IBM Plex Mono", "Fira Mono", "Cascadia Code", monospace;
    --font-body: system-ui, -apple-system, "Segoe UI", sans-serif;
    --radius:   6px;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body {
    background: var(--bg);
    color: var(--text);
    font-family: var(--font-body);
    font-size: 14px;
    line-height: 1.6;
    min-height: 100vh;
  }

  /* ── Header ── */
  header {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    padding: 14px 24px;
    display: flex;
    align-items: center;
    gap: 16px;
    position: sticky;
    top: 0;
    z-index: 10;
  }
  header h1 {
    font-family: var(--font-mono);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: .06em;
    color: var(--teal);
    flex: 1;
  }
  #clock {
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--muted);
    white-space: nowrap;
  }
  #refresh-btn {
    background: transparent;
    border: 1px solid var(--border);
    color: var(--teal);
    font-family: var(--font-mono);
    font-size: 11px;
    padding: 5px 12px;
    border-radius: var(--radius);
    cursor: pointer;
    transition: border-color .15s, background .15s;
  }
  #refresh-btn:hover { border-color: var(--teal); background: rgba(0,212,170,.07); }
  #countdown {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--muted);
    width: 52px;
    text-align: right;
  }

  /* ── Token gate ── */
  #token-gate {
    max-width: 480px;
    margin: 80px auto;
    padding: 32px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
  }
  #token-gate h2 {
    font-family: var(--font-mono);
    font-size: 13px;
    color: var(--teal);
    margin-bottom: 16px;
  }
  #token-gate p { color: var(--muted); font-size: 13px; margin-bottom: 20px; }
  #token-input {
    width: 100%;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    color: var(--text);
    font-family: var(--font-mono);
    font-size: 13px;
    padding: 10px 12px;
    outline: none;
    transition: border-color .15s;
  }
  #token-input:focus { border-color: var(--teal); }
  #token-submit {
    margin-top: 12px;
    width: 100%;
    background: var(--teal);
    border: none;
    border-radius: var(--radius);
    color: #080a0e;
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 700;
    padding: 10px;
    cursor: pointer;
    transition: opacity .15s;
  }
  #token-submit:hover { opacity: .85; }
  #token-error {
    margin-top: 12px;
    font-size: 12px;
    color: var(--red);
    display: none;
  }

  /* ── Panel ── */
  #panel { display: none; }
  #panel.visible { display: block; }

  /* ── Cards ── */
  #cards {
    max-width: 860px;
    margin: 32px auto;
    padding: 0 24px;
    display: flex;
    flex-direction: column;
    gap: 16px;
  }
  .empty-state {
    text-align: center;
    padding: 80px 0;
    color: var(--muted);
    font-family: var(--font-mono);
    font-size: 13px;
  }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 20px 24px;
    transition: opacity .3s, transform .3s;
  }
  .card.fading {
    opacity: 0;
    transform: translateX(24px);
  }

  /* card header row */
  .card-head {
    display: flex;
    align-items: baseline;
    gap: 12px;
    margin-bottom: 10px;
    flex-wrap: wrap;
  }
  .soul-name {
    font-family: var(--font-mono);
    font-size: 16px;
    font-weight: 700;
    color: var(--teal);
    letter-spacing: .04em;
  }
  .badge {
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    padding: 3px 8px;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: .08em;
  }
  .badge-write  { background: rgba(245,166,35,.12); color: var(--gold); border: 1px solid rgba(245,166,35,.3); }
  .badge-read   { background: rgba(0,212,170,.10);  color: var(--teal); border: 1px solid rgba(0,212,170,.25); }
  .badge-exec   { background: rgba(224,82,82,.12);  color: var(--red);  border: 1px solid rgba(224,82,82,.3); }

  /* meta row */
  .card-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 18px;
    margin-bottom: 14px;
  }
  .meta-item { font-size: 12px; color: var(--muted); }
  .meta-item code {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text);
    background: var(--bg);
    padding: 2px 6px;
    border-radius: 3px;
  }
  .expires-soon { color: var(--red) !important; }

  /* brief */
  .brief-block {
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 12px;
    margin-bottom: 14px;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text);
    white-space: pre-wrap;
    word-break: break-word;
    max-height: 160px;
    overflow-y: auto;
  }

  /* actions */
  .card-actions { display: flex; gap: 10px; }
  .btn {
    border: none;
    border-radius: var(--radius);
    font-family: var(--font-mono);
    font-size: 12px;
    font-weight: 600;
    padding: 8px 20px;
    cursor: pointer;
    transition: opacity .15s;
  }
  .btn:disabled { opacity: .4; cursor: not-allowed; }
  .btn:hover:not(:disabled) { opacity: .82; }
  .btn-approve { background: var(--teal); color: #080a0e; }
  .btn-reject  { background: var(--red);  color: #fff; }

  /* inline error */
  .card-error {
    margin-top: 10px;
    font-size: 12px;
    color: var(--red);
    display: none;
  }
  .card-error.visible { display: block; }

  /* ── Toast ── */
  #toast {
    position: fixed;
    bottom: 28px;
    right: 28px;
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 12px 20px;
    font-family: var(--font-mono);
    font-size: 12px;
    color: var(--text);
    max-width: 340px;
    opacity: 0;
    transform: translateY(8px);
    transition: opacity .25s, transform .25s;
    pointer-events: none;
    z-index: 100;
  }
  #toast.show {
    opacity: 1;
    transform: translateY(0);
  }
  #toast.toast-approve { border-color: var(--teal); color: var(--teal); }
  #toast.toast-reject  { border-color: var(--red);  color: var(--red); }
</style>
</head>
<body>

<header>
  <h1>CIPHER OS — Soul Dispatch Approvals</h1>
  <span id="clock">--:--:--</span>
  <button id="refresh-btn" onclick="manualRefresh()">Refresh</button>
  <span id="countdown">20s</span>
</header>

<!-- Token gate (shown when no token is set) -->
<div id="token-gate">
  <h2>Authentication required</h2>
  <p>Enter your approver token to access the dispatch queue.</p>
  <input
    id="token-input"
    type="password"
    placeholder="Bearer token…"
    autocomplete="off"
    onkeydown="if(event.key==='Enter') submitToken()"
  >
  <button id="token-submit" onclick="submitToken()">Continue</button>
  <div id="token-error">Authentication failed — check your token.</div>
</div>

<!-- Approval panel (shown after successful auth) -->
<div id="panel">
  <div id="cards">
    <div class="empty-state">Loading…</div>
  </div>
</div>

<div id="toast"></div>

<script>
"use strict";

// ── State ──────────────────────────────────────────────────────────────────
let _token = "";
let _refreshTimer = null;
let _countdown = 20;
const REFRESH_INTERVAL = 20;

// ── Boot ───────────────────────────────────────────────────────────────────
(function init() {
  tickClock();
  setInterval(tickClock, 1000);

  // Prefer URL param, then sessionStorage
  const params = new URLSearchParams(window.location.search);
  const urlToken = params.get("token") || "";
  const stored = (() => { try { return sessionStorage.getItem("cipher_approval_token") || ""; } catch { return ""; } })();

  if (urlToken) {
    _token = urlToken;
    // Clean the token from the URL without reload
    const clean = new URL(window.location.href);
    clean.searchParams.delete("token");
    history.replaceState(null, "", clean.toString());
    saveToken(_token);
    showPanel();
    loadApprovals();
    startCountdown();
  } else if (stored) {
    _token = stored;
    showPanel();
    loadApprovals();
    startCountdown();
  } else {
    document.getElementById("token-gate").style.display = "";
  }
})();

// ── Clock ──────────────────────────────────────────────────────────────────
function tickClock() {
  const now = new Date();
  document.getElementById("clock").textContent = now.toLocaleTimeString("en-US", { hour12: false });
}

// ── Token ──────────────────────────────────────────────────────────────────
function saveToken(t) {
  try { sessionStorage.setItem("cipher_approval_token", t); } catch {}
}
function clearToken() {
  _token = "";
  try { sessionStorage.removeItem("cipher_approval_token"); } catch {}
}

function submitToken() {
  const val = document.getElementById("token-input").value.trim();
  if (!val) return;
  _token = val;
  saveToken(_token);
  // Probe with a real fetch; on 401 show error
  fetch("/api/approvals", { headers: { Authorization: "Bearer " + _token } })
    .then(r => {
      if (r.status === 401) {
        clearToken();
        showTokenError();
        return;
      }
      hideTokenError();
      showPanel();
      return r.json().then(data => renderCards(data.approvals || []));
    })
    .catch(() => {
      clearToken();
      showTokenError();
    });
  stopCountdown();
  showPanel();
  startCountdown();
}

function showTokenError() {
  document.getElementById("token-error").style.display = "";
}
function hideTokenError() {
  document.getElementById("token-error").style.display = "none";
}

// ── Panel ──────────────────────────────────────────────────────────────────
function showPanel() {
  document.getElementById("token-gate").style.display = "none";
  document.getElementById("panel").classList.add("visible");
}

// ── Countdown ─────────────────────────────────────────────────────────────
function startCountdown() {
  stopCountdown();
  _countdown = REFRESH_INTERVAL;
  updateCountdownLabel();
  _refreshTimer = setInterval(() => {
    _countdown -= 1;
    updateCountdownLabel();
    if (_countdown <= 0) {
      _countdown = REFRESH_INTERVAL;
      loadApprovals();
    }
  }, 1000);
}
function stopCountdown() {
  if (_refreshTimer) { clearInterval(_refreshTimer); _refreshTimer = null; }
}
function updateCountdownLabel() {
  document.getElementById("countdown").textContent = _countdown + "s";
}
function manualRefresh() {
  _countdown = REFRESH_INTERVAL;
  updateCountdownLabel();
  loadApprovals();
}

// ── Load approvals ─────────────────────────────────────────────────────────
function loadApprovals() {
  fetch("/api/approvals", { headers: { Authorization: "Bearer " + _token } })
    .then(r => {
      if (r.status === 401) {
        clearToken();
        stopCountdown();
        document.getElementById("panel").classList.remove("visible");
        document.getElementById("token-gate").style.display = "";
        showTokenError();
        return;
      }
      return r.json().then(data => renderCards(data.approvals || []));
    })
    .catch(err => console.error("load approvals:", err));
}

// ── Render ─────────────────────────────────────────────────────────────────
function renderCards(approvals) {
  const container = document.getElementById("cards");
  if (!approvals.length) {
    container.innerHTML = '<div class="empty-state">No pending approvals</div>';
    return;
  }
  container.innerHTML = "";
  approvals.forEach(a => container.appendChild(buildCard(a)));
}

function buildCard(a) {
  const card = document.createElement("div");
  card.className = "card";
  card.id = "card-" + a.approval_id;

  // Timing
  const now = Date.now() / 1000;
  const ageMs = (now - a.created_at) * 1000;
  const expiresInSec = a.expires_at - now;
  const expiresSoon = expiresInSec < 300;

  // Brief (from context)
  const ctx = a.context || {};
  const brief = ctx.brief || ctx.task || ctx.description || "";

  // Badge class
  const tier = (a.autonomy || "write").toLowerCase();
  const badgeClass = tier === "read" ? "badge-read" : tier === "exec" ? "badge-exec" : "badge-write";

  // Root exec id (truncated)
  const execShort = (a.root_exec_id || "").slice(0, 16) + "…";

  card.innerHTML = `
    <div class="card-head">
      <span class="soul-name">${esc(a.soul)}</span>
      <span class="badge ${badgeClass}">${esc(a.autonomy || "write")}</span>
    </div>
    <div class="card-meta">
      <span class="meta-item">ID <code title="${esc(a.root_exec_id)}">${esc(execShort)}</code></span>
      <span class="meta-item">Created <code>${relTime(ageMs)}</code></span>
      <span class="meta-item ${expiresSoon ? "expires-soon" : ""}">
        Expires in <code class="${expiresSoon ? "expires-soon" : ""}">${fmtDuration(expiresInSec)}</code>
      </span>
    </div>
    ${brief ? `<pre class="brief-block">${esc(brief)}</pre>` : ""}
    <div class="card-actions">
      <button class="btn btn-approve" onclick="act('${a.approval_id}', 'approve', '${esc(a.soul)}')">Approve</button>
      <button class="btn btn-reject"  onclick="act('${a.approval_id}', 'reject',  '${esc(a.soul)}')">Reject</button>
    </div>
    <div class="card-error" id="err-${a.approval_id}"></div>
  `;
  return card;
}

// ── Actions ───────────────────────────────────────────────────────────────
function act(approvalId, action, soul) {
  const card = document.getElementById("card-" + approvalId);
  if (!card) return;

  const btns = card.querySelectorAll(".btn");
  btns.forEach(b => { b.disabled = true; });

  fetch("/api/approvals/" + encodeURIComponent(approvalId) + "/" + action, {
    method: "POST",
    headers: { Authorization: "Bearer " + _token },
  })
    .then(r => {
      if (r.status === 401) {
        clearToken();
        stopCountdown();
        document.getElementById("panel").classList.remove("visible");
        document.getElementById("token-gate").style.display = "";
        showTokenError();
        return;
      }
      if (r.status === 409) {
        return r.json().then(d => {
          showCardError(approvalId, d.detail || "Already actioned.");
          btns.forEach(b => { b.disabled = false; });
          loadApprovals();
        });
      }
      if (!r.ok) {
        return r.json().then(d => {
          showCardError(approvalId, d.detail || "Unexpected error.");
          btns.forEach(b => { b.disabled = false; });
        });
      }
      // Success — fade out card, show toast
      card.classList.add("fading");
      const label = action === "approve" ? "Approved" : "Rejected";
      toast(label + ": " + soul, action === "approve" ? "toast-approve" : "toast-reject");
      setTimeout(() => {
        if (card.parentNode) card.parentNode.removeChild(card);
        if (!document.getElementById("cards").querySelector(".card")) {
          document.getElementById("cards").innerHTML = '<div class="empty-state">No pending approvals</div>';
        }
      }, 320);
    })
    .catch(err => {
      showCardError(approvalId, "Network error — " + err.message);
      btns.forEach(b => { b.disabled = false; });
    });
}

function showCardError(approvalId, msg) {
  const el = document.getElementById("err-" + approvalId);
  if (!el) return;
  el.textContent = msg;
  el.classList.add("visible");
}

// ── Toast ─────────────────────────────────────────────────────────────────
let _toastTimer = null;
function toast(msg, cls) {
  const el = document.getElementById("toast");
  el.textContent = msg;
  el.className = "show " + (cls || "");
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => { el.className = ""; }, 3000);
}

// ── Helpers ───────────────────────────────────────────────────────────────
function esc(s) {
  return String(s || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function relTime(ms) {
  const s = ms / 1000;
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  return Math.round(s / 3600) + " hr ago";
}

function fmtDuration(sec) {
  if (sec <= 0) return "expired";
  if (sec < 60) return Math.round(sec) + "s";
  return Math.round(sec / 60) + " min";
}
</script>
</body>
</html>
"""


@router.get("/approvals", response_class=HTMLResponse)
async def approval_panel(request: Request):
    """Human-facing approval panel. No auth required to load the page."""
    return HTMLResponse(content=_PANEL_HTML)
