"""Phase 3 capability tests.

Covers the four Gate-3-cleared requirements per Argus doctrine:
1. Workforce env loader — never leaks blocked keys, strips 'export ' prefix
2. RAG isolation — soul A query returns zero of soul B's chunks
3. Webhook HMAC — bad signatures are rejected; no-secret-configured = accept
4. Calendar — graceful CredentialsNotConfigured when creds absent
5. Soul manifest — unknown soul gets zero capabilities (fail-closed)
6. Code sandbox env scrubbing — CIPHER_*/ANTHROPIC_* never reach executor env
"""

from __future__ import annotations

import hashlib
import hmac
import os
import sqlite3
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# 1. Workforce env loader
# ---------------------------------------------------------------------------

class TestWorkforceEnv:
    def test_parses_plain_key_value(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("WORKFORCE_EMAIL_ADDRESS=test@example.com\n")
        from services.workforce_env import _parse_dotenv
        result = _parse_dotenv(env_file)
        assert result["WORKFORCE_EMAIL_ADDRESS"] == "test@example.com"

    def test_strips_export_prefix(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("export WORKFORCE_EMAIL_ADDRESS=hello@world.com\n")
        from services.workforce_env import _parse_dotenv
        result = _parse_dotenv(env_file)
        assert result["WORKFORCE_EMAIL_ADDRESS"] == "hello@world.com"

    def test_blocked_prefixes_never_surfaced(self, tmp_path):
        """CIPHER_*, ANTHROPIC_*, STRIPE_*, GOOGLE_*, OPENAI_* keys are dropped."""
        env_file = tmp_path / ".env"
        env_file.write_text(
            textwrap.dedent("""\
                CIPHER_APPROVAL_TOKEN=should-be-blocked
                ANTHROPIC_API_KEY=also-blocked
                STRIPE_SECRET_KEY=blocked
                GOOGLE_PRIVATE_KEY=blocked
                OPENAI_API_KEY=blocked
                WORKFORCE_SAFE_KEY=allowed
            """)
        )
        from services import workforce_env

        # Force reload with temp path
        workforce_env._load_env.cache_clear()
        with patch.object(workforce_env, "_WORKFORCE_ENV_PATH", env_file):
            workforce_env._load_env.cache_clear()
            env = workforce_env._load_env()

        assert "CIPHER_APPROVAL_TOKEN" not in env
        assert "ANTHROPIC_API_KEY" not in env
        assert "STRIPE_SECRET_KEY" not in env
        assert "GOOGLE_PRIVATE_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert env.get("WORKFORCE_SAFE_KEY") == "allowed"
        workforce_env._load_env.cache_clear()

    def test_missing_file_returns_none(self, tmp_path):
        from services import workforce_env

        workforce_env._load_env.cache_clear()
        with patch.object(workforce_env, "_WORKFORCE_ENV_PATH", tmp_path / "nonexistent.env"):
            workforce_env._load_env.cache_clear()
            result = workforce_env.get_workforce_credential("WORKFORCE_FOO")
        assert result is None
        workforce_env._load_env.cache_clear()

    def test_empty_value_returns_none(self, tmp_path):
        env_file = tmp_path / ".env"
        env_file.write_text("WORKFORCE_EMPTY=\n")
        from services import workforce_env

        workforce_env._load_env.cache_clear()
        with patch.object(workforce_env, "_WORKFORCE_ENV_PATH", env_file):
            workforce_env._load_env.cache_clear()
            result = workforce_env.get_workforce_credential("WORKFORCE_EMPTY")
        assert result is None
        workforce_env._load_env.cache_clear()


# ---------------------------------------------------------------------------
# 2. RAG isolation — soul A query returns zero soul B chunks
# ---------------------------------------------------------------------------

class TestRagIsolation:
    """Soul A cannot retrieve soul B's chunks. Asserted without hitting OpenAI."""

    def _make_fake_chunks(self, db_path, soul_id: str, texts: list[str], dim: int = 4) -> None:
        """Insert pre-computed dummy embeddings directly into the DB."""
        import json, time, math, random
        conn = sqlite3.connect(str(db_path))
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                soul_id TEXT NOT NULL,
                text TEXT NOT NULL,
                embedding TEXT NOT NULL,
                metadata TEXT DEFAULT '{}',
                generated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_chunks_soul ON chunks(soul_id);
        """)
        now = time.time()
        for i, text in enumerate(texts):
            # Deterministic unit vector so cosine similarity is well-defined
            raw = [float(i + 1), float((i + 1) * 2), 0.0, 0.0]
            norm = math.sqrt(sum(x*x for x in raw))
            emb = [x / norm for x in raw]
            conn.execute(
                "INSERT INTO chunks (soul_id, text, embedding, metadata, generated_at) VALUES (?,?,?,?,?)",
                (soul_id, text, json.dumps(emb), '{}', now),
            )
        conn.commit()
        conn.close()

    @pytest.mark.asyncio
    async def test_soul_isolation(self, tmp_path):
        from nodes.rag import _store

        db_path = tmp_path / "rag.db"

        with patch.object(_store, "_DB_PATH", db_path), \
             patch.object(_store, "_DB_DIR", tmp_path):

            # Seed soul_a and soul_b chunks without hitting OpenAI
            self._make_fake_chunks(db_path, "soul_a", ["alpha chunk", "beta chunk"])
            self._make_fake_chunks(db_path, "soul_b", ["gamma chunk", "delta chunk"])

            # Query using a fake embedding that is also a unit vector
            fake_query_emb = [1.0, 0.0, 0.0, 0.0]

            async def fake_embed(texts):
                return [fake_query_emb for _ in texts]

            with patch.object(_store, "_embed", fake_embed):
                results_a = await _store.query("soul_a", "anything", k=10)
                results_b = await _store.query("soul_b", "anything", k=10)

        # soul_a query returns only soul_a chunks
        assert all(r["text"] in {"alpha chunk", "beta chunk"} for r in results_a), results_a
        assert len(results_a) == 2

        # soul_b query returns only soul_b chunks — none of soul_a's
        assert all(r["text"] in {"gamma chunk", "delta chunk"} for r in results_b), results_b
        assert len(results_b) == 2

        # Cross-contamination is zero
        a_texts = {r["text"] for r in results_a}
        b_texts = {r["text"] for r in results_b}
        assert a_texts.isdisjoint(b_texts), f"Isolation violated: overlap = {a_texts & b_texts}"


# ---------------------------------------------------------------------------
# 3. Webhook HMAC verification
# ---------------------------------------------------------------------------

class TestWebhookHmac:
    def _sig(self, payload: bytes, secret: bytes) -> str:
        return "sha256=" + hmac.new(secret, payload, hashlib.sha256).hexdigest()

    def test_valid_signature_accepted(self):
        from services import webhook_store

        secret = b"test-secret"
        payload = b'{"event":"push"}'
        sig = self._sig(payload, secret)

        with patch.object(webhook_store, "_load_hmac_secret", return_value=secret):
            assert webhook_store.verify_hmac(payload, sig) is True

    def test_bad_signature_rejected(self):
        from services import webhook_store

        secret = b"test-secret"
        payload = b'{"event":"push"}'

        with patch.object(webhook_store, "_load_hmac_secret", return_value=secret):
            assert webhook_store.verify_hmac(payload, "sha256=deadbeef") is False

    def test_missing_header_rejected_when_secret_configured(self):
        from services import webhook_store

        with patch.object(webhook_store, "_load_hmac_secret", return_value=b"secret"):
            assert webhook_store.verify_hmac(b"body", None) is False

    def test_no_secret_configured_fails_closed(self):
        """If no HMAC secret is configured, all requests are rejected (fail closed).

        Argus F2: the prior fail-open behavior was a HIGH security defect.
        An unconfigured endpoint must never silently accept requests as verified.
        """
        from services import webhook_store

        with patch.object(webhook_store, "_load_hmac_secret", return_value=None):
            assert webhook_store.verify_hmac(b"body", None) is False
            assert webhook_store.verify_hmac(b"body", "sha256=garbage") is False

    def test_event_stored_in_db(self, tmp_path):
        from services import webhook_store

        db_path = tmp_path / "webhooks.db"
        with patch.object(webhook_store, "_DB_PATH", db_path), \
             patch.object(webhook_store, "_DB_DIR", tmp_path):
            row_id = webhook_store.store_event(
                path="test/path",
                method="POST",
                headers={"content-type": "application/json"},
                body='{"x":1}',
                verified=True,
            )
            events = webhook_store.list_events("test/path")

        assert row_id == 1
        assert len(events) == 1
        assert events[0]["path"] == "test/path"
        assert events[0]["verified"] is True

    def test_different_path_events_isolated(self, tmp_path):
        from services import webhook_store

        db_path = tmp_path / "webhooks.db"
        with patch.object(webhook_store, "_DB_PATH", db_path), \
             patch.object(webhook_store, "_DB_DIR", tmp_path):
            webhook_store.store_event("path/a", "POST", {}, "a", False)
            webhook_store.store_event("path/b", "POST", {}, "b", False)
            events_a = webhook_store.list_events("path/a")
            events_b = webhook_store.list_events("path/b")

        assert len(events_a) == 1 and events_a[0]["body"] == "a"
        assert len(events_b) == 1 and events_b[0]["body"] == "b"


# ---------------------------------------------------------------------------
# 4. Calendar — graceful degradation
# ---------------------------------------------------------------------------

class TestCalendarCredentials:
    def test_raises_credentials_not_configured_when_path_absent(self):
        from nodes.calendar import CredentialsNotConfigured, _get_calendar_service

        # get_workforce_credential is imported lazily inside _get_calendar_service;
        # patch at the source module so the lazy import picks up the mock.
        with patch("services.workforce_env.get_workforce_credential", return_value=None):
            with pytest.raises(CredentialsNotConfigured) as exc_info:
                _get_calendar_service()
        assert "credentials not configured" in str(exc_info.value).lower()

    def test_raises_credentials_not_configured_when_file_missing(self, tmp_path):
        from nodes.calendar import CredentialsNotConfigured, _get_calendar_service

        nonexistent = str(tmp_path / "creds.json")
        with patch("services.workforce_env.get_workforce_credential", return_value=nonexistent):
            with pytest.raises(CredentialsNotConfigured) as exc_info:
                _get_calendar_service()
        assert "not found" in str(exc_info.value).lower()


# ---------------------------------------------------------------------------
# 5. Soul manifest — fail-closed default
# ---------------------------------------------------------------------------

class TestSoulManifest:
    def test_unknown_soul_has_zero_capabilities(self):
        from services.soul_manifest import get_manifest

        m = get_manifest("nonexistent_soul_xyz")
        assert len(m.enabled_node_types()) == 0

    def test_known_soul_has_capabilities(self):
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        assert "pythonExecutor" in m.enabled_node_types()
        assert "ragQuery" in m.enabled_node_types()
        assert "ragStore" in m.enabled_node_types()

    def test_dragon_gated_capability_disabled(self):
        from services.soul_manifest import get_manifest

        m = get_manifest("maren")
        # GCP is listed but disabled (Dragon-gated)
        assert "gcloud" not in m.enabled_node_types()
        all_types = {e.node_type for e in m.capabilities}
        assert "gcloud" in all_types  # wired

    def test_manifest_hash_is_deterministic(self):
        from services.soul_manifest import manifest_hash

        h1 = manifest_hash("orion")
        h2 = manifest_hash("orion")
        assert h1 == h2
        assert len(h1) == 64  # SHA-256 hex

    def test_different_souls_have_different_hashes(self):
        from services.soul_manifest import manifest_hash

        assert manifest_hash("orion") != manifest_hash("maren")


# ---------------------------------------------------------------------------
# 6. Code sandbox — env scrubbing (existing executor, verify safe_env applies)
# ---------------------------------------------------------------------------

class TestCodeSandboxEnvScrubbing:
    def test_build_safe_env_excludes_blocked_prefixes(self):
        """The existing safe_env module excludes CIPHER_*/ANTHROPIC_* — verify."""
        from services.safe_env import build_safe_env, _BLOCKED_PREFIXES

        # Temporarily inject a blocked key into os.environ
        test_key = "CIPHER_TEST_SECRET_PHASE3"
        original = os.environ.pop(test_key, None)
        try:
            os.environ[test_key] = "should-not-appear"
            env = build_safe_env()
        finally:
            if original is None:
                os.environ.pop(test_key, None)
            else:
                os.environ[test_key] = original

        assert test_key not in env

    def test_blocked_prefixes_cover_all_secret_namespaces(self):
        from services.safe_env import _BLOCKED_PREFIXES

        required = {"CIPHER_", "ANTHROPIC_", "STRIPE_", "GOOGLE_"}
        assert required.issubset(set(_BLOCKED_PREFIXES)), (
            f"Missing blocked prefixes: {required - set(_BLOCKED_PREFIXES)}"
        )
