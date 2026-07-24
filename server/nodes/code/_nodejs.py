"""Shared Node.js executor client for JS/TS plugins.

Both ``javascript_executor.py`` and ``typescript_executor.py`` dispatch
through the same persistent Node.js server (see ``services/nodejs/``).
This helper owns the singleton client so both plugins share one HTTP
session.
"""

from __future__ import annotations

from typing import Optional

from services.nodejs_client import NodeJSClient

_client: Optional[NodeJSClient] = None


def get_nodejs_client(base_url: str = "http://localhost:5680", timeout: int = 30) -> NodeJSClient:
    global _client
    if _client is None:
        _client = NodeJSClient(base_url, timeout)
    return _client
