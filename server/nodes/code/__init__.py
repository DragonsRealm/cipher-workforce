"""Plugins for the 'code' palette group. See ../__init__.py for the package layout.

The Node.js executor sidecar (JS/TS code nodes) is plugin-owned: the
supervisor in :mod:`._runtime` spawns it on demand and is registered
here so ``shutdown_all_supervisors()`` reaches it at lifespan shutdown.
"""

from services._supervisor import register_supervisor

from ._runtime import NodeJSExecutorRuntime, get_nodejs_executor_runtime

register_supervisor(NodeJSExecutorRuntime.get_instance())

__all__ = [
    "NodeJSExecutorRuntime",
    "get_nodejs_executor_runtime",
]
