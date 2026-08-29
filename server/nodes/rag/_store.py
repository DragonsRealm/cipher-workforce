"""SQLite-backed vector store for the RAG capability node.

Design (Orion D5):
- Shared SQLite instance at ``~/.cipheros/workforce/rag/rag.db``
- Per-soul namespace enforced by ``soul_id`` column + query filter
- Read cache only: vault → embed → store. Souls retrieve, never write to vault.
- Embeddings via OpenAI ``text-embedding-3-small`` (already in server deps).
- Cosine similarity computed in pure Python (no numpy/chromadb required).
- ``generated_at`` timestamps every chunk so staleness is detectable.

Argus-flagged: OPENAI_API_KEY is read here from ``os.environ`` solely for
embedding generation (same path every other node uses). It is not surfaced
in child env by ``build_safe_env()``.
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Optional

_DB_DIR = Path.home() / ".cipheros" / "workforce" / "rag"
_DB_PATH = _DB_DIR / "rag.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    soul_id     TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    embedding   TEXT    NOT NULL,   -- JSON-serialized float list
    metadata    TEXT    DEFAULT '{}',
    generated_at REAL   NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_soul ON chunks(soul_id);
"""


def _get_conn() -> sqlite3.Connection:
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.executescript(_SCHEMA)
    conn.commit()
    return conn


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


async def _embed(texts: list[str]) -> list[list[float]]:
    """Embed texts using OpenAI text-embedding-3-small."""
    from openai import AsyncOpenAI

    client = AsyncOpenAI()
    response = await client.embeddings.create(
        model="text-embedding-3-small",
        input=texts,
    )
    return [item.embedding for item in response.data]


async def embed_and_store(
    chunks: list[str],
    soul_id: str,
    metadata: Optional[dict] = None,
) -> int:
    """Embed ``chunks`` and store them under ``soul_id``.

    Args:
        chunks: Text chunks to embed and store.
        soul_id: DCS soul identifier (e.g. ``"maren"``, ``"orion"``).
        metadata: Optional dict attached to every chunk.

    Returns:
        Number of chunks stored.
    """
    if not chunks:
        return 0
    embeddings = await _embed(chunks)
    meta_str = json.dumps(metadata or {})
    now = time.time()

    conn = _get_conn()
    try:
        conn.executemany(
            "INSERT INTO chunks (soul_id, text, embedding, metadata, generated_at) VALUES (?, ?, ?, ?, ?)",
            [
                (soul_id, text, json.dumps(emb), meta_str, now)
                for text, emb in zip(chunks, embeddings)
            ],
        )
        conn.commit()
    finally:
        conn.close()

    return len(chunks)


async def query(
    soul_id: str,
    text: str,
    k: int = 5,
) -> list[dict]:
    """Query the store for the ``k`` most similar chunks for ``soul_id``.

    Soul isolation is enforced by the ``soul_id`` filter — soul A's query
    returns zero of soul B's chunks (asserted in tests/test_rag_store.py).

    Args:
        soul_id: Only chunks belonging to this soul are searched.
        text: Query text (will be embedded).
        k: Number of results to return.

    Returns:
        List of dicts with ``text``, ``score``, ``metadata``, ``generated_at``.
    """
    [query_emb] = await _embed([text])

    conn = _get_conn()
    try:
        rows = conn.execute(
            "SELECT text, embedding, metadata, generated_at FROM chunks WHERE soul_id = ?",
            (soul_id,),
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return []

    scored = []
    for text_val, emb_json, meta_json, gen_at in rows:
        emb = json.loads(emb_json)
        score = _cosine(query_emb, emb)
        scored.append(
            {
                "text": text_val,
                "score": score,
                "metadata": json.loads(meta_json),
                "generated_at": gen_at,
            }
        )

    scored.sort(key=lambda r: r["score"], reverse=True)
    return scored[:k]


def clear_soul(soul_id: str) -> int:
    """Delete all chunks for ``soul_id``. Returns deleted row count."""
    conn = _get_conn()
    try:
        cur = conn.execute("DELETE FROM chunks WHERE soul_id = ?", (soul_id,))
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()
