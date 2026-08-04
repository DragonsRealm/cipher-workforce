"""Tenancy principal and credential namespace must never be aliased.

``user_id`` (who is acting) and the credential ``customer_id`` (whose
stored tokens to read) happen to share the literal "owner". Collapsing
them means the day ``user_id`` becomes a real authenticated subject,
every OAuth-backed node starts looking up tokens under a customer id
nobody has stored anything under.
"""

from __future__ import annotations

import inspect
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parents[1]


def test_the_two_constants_exist_and_are_not_aliased():
    import constants

    assert constants.OWNER_PRINCIPAL_ID == "owner"
    assert constants.DEFAULT_CREDENTIAL_CUSTOMER_ID == "owner"
    source = inspect.getsource(constants)
    # Same value, but each must be its own literal. `X = Y` would mean a
    # change to one silently moves the other.
    assert "DEFAULT_CREDENTIAL_CUSTOMER_ID: str = OWNER_PRINCIPAL_ID" not in source
    assert "OWNER_PRINCIPAL_ID: str = DEFAULT_CREDENTIAL_CUSTOMER_ID" not in source


def test_node_context_keeps_them_independent():
    from services.plugin.context import NodeContext

    ctx = NodeContext.from_legacy(
        node_id="n", node_type="t", context={"user_id": "42"}
    )
    assert ctx.user_id == "42"
    # The credential namespace must NOT follow the principal.
    assert ctx.credential_customer_id == "owner"


def test_connection_factory_scopes_on_the_credential_namespace():
    """The actual coupling point: this read is what broke OAuth nodes."""
    from services.plugin import base

    source = inspect.getsource(base._make_connection_factory)
    assert "credential_customer_id" in source
    assert 'context.get("user_id"' not in source, (
        "the connection factory must not scope credentials by the tenancy "
        "principal — that re-points every OAuth token lookup"
    )


def test_credential_modules_do_not_import_the_tenancy_principal():
    """An import-graph guard: credential code has no business knowing
    about the tenancy principal, and importing it invites aliasing."""
    targets = [
        SERVER_DIR / "services" / "auth.py",
        SERVER_DIR / "services" / "plugin" / "connection.py",
        SERVER_DIR / "services" / "plugin" / "credential.py",
        SERVER_DIR / "core" / "credentials_database.py",
        SERVER_DIR / "core" / "credential_backends.py",
    ]
    offenders = [
        p.name
        for p in targets
        if p.exists() and "OWNER_PRINCIPAL_ID" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, f"credential modules import the tenancy principal: {offenders}"
