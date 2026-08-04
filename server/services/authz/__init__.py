"""Authorization: who may act, and on what.

Public surface is re-exported here so callers never import submodules
directly and the boundary stays greppable.
"""

from services.authz.ws_surface import (
    INTERNAL_SOCKET_HANDLERS,
    execution_principal,
    resolve_internal_handler,
)

__all__ = [
    "INTERNAL_SOCKET_HANDLERS",
    "execution_principal",
    "resolve_internal_handler",
]
