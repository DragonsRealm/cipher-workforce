"""
Modern FastAPI backend for React Flow workflow automation platform.

Refactored with dependency injection, modular services, and clean architecture.
"""


def _startup_log(msg):
    """Pre-logger boot-progress marker.

    Emits via ``print()`` so the global CLI wrapper's
    ``[HH:MM:SS.ffffff]`` prefix supplies the timestamp — we don't add
    a second one here. Used during the import phase (before
    ``configure_logging`` runs) and inside the lifespan startup hook
    for milestones; after that, regular ``logger.info`` is preferred.
    """
    print(f"  {msg}", flush=True)


# Performance: Install uvloop if available (Linux/macOS only)
try:
    import uvloop

    uvloop.install()
except ImportError:
    pass  # Windows - uvloop not available, use default asyncio

import asyncio
from datetime import datetime
from contextlib import asynccontextmanager

# Note: We don't register custom signal handlers.
# uvicorn already handles SIGINT (Ctrl+C) and SIGTERM (docker stop) gracefully.
# Adding custom handlers that raise KeyboardInterrupt causes cascading errors
# during async operations (WebSocket handlers, logging, etc.).

_startup_log("Importing FastAPI...")
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

# Configure logging FIRST — before any imports that may trigger
# ``logger.debug(...)`` calls (DI container, routers, plugin
# self-registration). Otherwise structlog's *default* processor chain
# wins on first use, which includes ``TimeStamper`` and produces
# double-time output (``[10:51:08.157] server | 2026-05-15 10:51:08
# [debug] ...``) because the supervisor already prepends
# ``[HH:MM:SS.fff]`` to every aggregated line.
_startup_log("Importing settings + logging...")
from core.config import Settings, cookie_posture_warnings, dev_secret_offenders
from core.logging import configure_logging, get_logger, setup_websocket_logging, shutdown_websocket_logging
from core.tracing import init_tracing

settings = Settings()
configure_logging(settings)
init_tracing(console_spans=settings.tracing_console_spans_enabled)
logger = get_logger(__name__)

_startup_log("Importing DI container + all services...")
from core.container import container

_startup_log("Importing routers...")
from routers import workflow, database, websocket, webhook, auth, credentials, schemas, workspace

_startup_log("All imports complete")

# Suppress noisy loggers
import logging

logging.getLogger("uvicorn").setLevel(logging.WARNING)
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
logging.getLogger("watchfiles").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan management."""
    # Startup
    _startup_log("Lifespan startup begin")

    # Non-fatal security posture check: warn loudly when the shipped dev
    # placeholder secrets are still in use outside a local dev posture
    # (auth enabled, or DEPLOYMENT_MODE != local). Never raises — the
    # operator gets a prominent banner instead of a bricked server.
    offenders = dev_secret_offenders(settings)
    if offenders:
        logger.error(
            "SECURITY WARNING: dev placeholder secrets detected in a non-dev posture.\n"
            "The following env vars still carry the publicly known .env.template values:\n"
            "    %s\n"
            "Generate real secrets, e.g.:\n"
            "    python -c \"import secrets; print(secrets.token_hex(24))\"\n"
            "and set them in .env (or the process environment), then restart.",
            ", ".join(offenders),
        )

    # Same posture-warning pattern for the session cookie. Warnings only:
    # `company deploy` intentionally ships JWT_COOKIE_SECURE=false because
    # the VM is reached over plain HTTP on its IP, so raising here would
    # brick every LAN/IP deployment.
    for warning in cookie_posture_warnings(settings):
        logger.warning("SECURITY: %s", warning)

    # Wave 10.C: discover node plugins so their register_node() calls
    # populate the four registries before any router serves NodeSpec.
    # Side-effect import; the package __init__ walks its submodules.
    import nodes  # noqa: F401

    # Side-effect import: services/cli_agent/__init__.py self-registers
    # `cli_login` / `cli_auth_status` into `services.ws_handler_registry`.
    import services.cli_agent  # noqa: F401

    # Wave 13.1: services/skills/__init__.py self-registers the 13 skill
    # WS handlers (get_skill_content / user-skill CRUD / clear_memory /
    # reset_skill / ...) into ws_handler_registry — moved out of
    # routers/websocket.py.
    import services.skills  # noqa: F401

    # Wave 13.3: services/settings/__init__.py self-registers the 9 settings
    # handlers (user_settings + provider_defaults + validated_ai_providers +
    # save_global_model + compaction + provider_usage_summary).
    import services.settings  # noqa: F401

    # Wave 13.4: services/agent_teams/__init__.py self-registers the 10
    # team-lifecycle handlers (create_team / get_team / ... / get_team_messages).
    import services.agent_teams  # noqa: F401

    # Wave 13.5: services/credentials/__init__.py self-registers the 4
    # credential CRUD handlers (validate_api_key / get_stored_api_key /
    # save_api_key / delete_api_key).
    import services.credentials  # noqa: F401

    # Wave 13.7: services/workflow_storage/__init__.py self-registers the
    # 5 workflow-record CRUD handlers (save_workflow / import_workflow /
    # get_workflow / get_all_workflows / delete_workflow).
    import services.workflow_storage  # noqa: F401

    # Wave 13.8: services/pricing_handlers.py self-registers the 3
    # pricing handlers (get_pricing_config / save_pricing_config /
    # get_api_usage_summary). Flat module (sibling to services/pricing.py)
    # to avoid renaming the existing `services.pricing` import path.
    import services.pricing_handlers  # noqa: F401

    container.wire(
        modules=[
            "routers.workflow",
            "routers.database",
            "routers.websocket",
            "routers.webhook",
            "routers.auth",
            "middleware.auth",
        ]
    )

    # Start services
    await container.database().startup()
    await container.cache().startup()
    _startup_log("Database + cache started")

    # Initialize credentials database (creates tables if not exist)
    credentials_db = container.credentials_database()
    salt = await credentials_db.initialize()
    logger.info("Credentials database initialized")

    # Initialize encryption with server-scoped key (n8n pattern)
    # Key from .env persists across restarts, not tied to user sessions
    encryption = container.encryption_service()
    if not encryption.is_initialized():
        encryption.initialize(settings.api_key_encryption_key, salt)
        logger.info("Encryption service initialized")
    _startup_log("Credentials + encryption initialized")

    # Seed the owner login credential from the environment (container
    # login gate). Idempotent: only creates the owner when no user exists,
    # so restarts and in-app password changes are never clobbered. Reuses
    # the built-in auth (UserAuthService.register -> bcrypt User row) — no
    # new auth code. Set OPENCOMPANY_OWNER_EMAIL +
    # OPENCOMPANY_OWNER_PASSWORD (password >= 8 chars) via Secret Manager.
    # MACHINA_OWNER_* remains a read-only fallback for existing deployments.
    import os as _seed_os

    _owner_email = _seed_os.environ.get("OPENCOMPANY_OWNER_EMAIL", _seed_os.environ.get("MACHINA_OWNER_EMAIL", "")).strip()
    _owner_password = _seed_os.environ.get(
        "OPENCOMPANY_OWNER_PASSWORD",
        _seed_os.environ.get("MACHINA_OWNER_PASSWORD", ""),
    )
    if _owner_email and _owner_password:
        try:
            _user_auth = container.user_auth_service()
            if await _user_auth.get_user_count() == 0:
                _owner_name = _seed_os.environ.get(
                    "OPENCOMPANY_OWNER_NAME",
                    _seed_os.environ.get("MACHINA_OWNER_NAME", "Owner"),
                )
                _seeded, _seed_err = await _user_auth.register(_owner_email, _owner_password, _owner_name)
                if _seeded is not None:
                    logger.info("Seeded owner account from environment", email=_owner_email)
                else:
                    logger.error("Owner account seed failed", reason=_seed_err)
            else:
                logger.debug("Owner seed skipped: a user already exists")
        except Exception as _seed_exc:  # noqa: BLE001 — seeding must never block startup
            logger.error("Owner account seed raised", error=str(_seed_exc))

    # Initialize event waiter with cache service for Redis Streams support
    from services import event_waiter

    event_waiter.capture_main_loop()

    # Initialize execution engine recovery sweeper
    from services.execution import (
        ExecutionCache,
        RecoverySweeper,
        set_recovery_sweeper,
    )

    execution_cache = ExecutionCache(container.cache())
    recovery_sweeper = RecoverySweeper(execution_cache)
    set_recovery_sweeper(recovery_sweeper)

    # Scan for incomplete executions on startup
    if settings.redis_enabled:
        incomplete = await recovery_sweeper.scan_on_startup()
        if incomplete:
            logger.info("Found incomplete executions on startup", count=len(incomplete), execution_ids=incomplete)

        # Start background recovery sweeper
        await recovery_sweeper.start()
        logger.info("Execution recovery sweeper started")

    # Start WebSocket logging handler to broadcast logs to frontend
    loop = asyncio.get_running_loop()
    setup_websocket_logging(loop)
    logger.info("WebSocket logging handler started")

    # Start cleanup service for long-running daemon
    from core.cleanup import CleanupService
    from core.health import set_startup_time

    cleanup_service = None
    if settings.cleanup_enabled:
        cleanup_service = CleanupService(database=container.database(), cache=container.cache(), settings=settings)
        await cleanup_service.start()

    # Initialize compaction service and wire AI service
    compaction_svc = container.compaction_service()  # Trigger singleton initialization
    compaction_svc.set_ai_service(container.ai_service())
    logger.info("Compaction service initialized")
    _startup_log("Compaction service ready")

    # Initialize model registry service
    from services.model_registry import get_model_registry

    model_registry = get_model_registry()
    model_registry.startup()
    logger.info("Model registry initialized")

    # Background refresh if cache is stale
    if model_registry.is_stale():

        async def _refresh_registry():
            try:
                count = await model_registry.refresh()
                logger.info(f"Model registry refreshed: {count} models")
            except Exception as e:
                logger.warning(f"Model registry refresh failed (offline?): {e}")

        asyncio.create_task(_refresh_registry())

    # Initialize agent team service
    from services.agent_team import init_agent_team_service
    from services.status_broadcaster import get_status_broadcaster

    init_agent_team_service(container.database(), get_status_broadcaster())

    # Wire process service to broadcaster for Terminal tab streaming
    from services.process_service import get_process_service

    proc_svc = get_process_service()
    proc_svc.set_broadcaster(get_status_broadcaster())
    # Load max_processes from user settings if configured
    try:
        user_settings = await container.database().get_user_settings("default")
        if user_settings and "max_processes" in user_settings:
            proc_svc.max_processes = int(user_settings["max_processes"])
    except Exception:
        pass
    logger.info("Agent team service initialized")

    # Initialize proxy service (loads providers from DB, reads credentials)
    from services.proxy.service import init_proxy_service

    proxy_svc = init_proxy_service(
        auth_service=container.auth_service(),
        database=container.database(),
        settings=settings,
    )
    await proxy_svc.startup()
    _startup_log("Proxy service initialized")

    # Record startup time for health reporting
    set_startup_time()

    # Initialize Temporal in the background - do NOT block lifespan startup.
    # This lifespan owns the Temporal dev server (spawned below via
    # TemporalServerRuntime.ensure_started), and on fresh startup it may take
    # several seconds to become reachable. Blocking the lifespan here would
    # delay FastAPI HTTP serving and cascade into frontend
    # ERR_CONNECTION_REFUSED on /api/auth/status. Instead, yield fast and
    # let Temporal init happen in a background task. WorkflowService falls back
    # to parallel/sequential execution until Temporal is ready.
    app.state.temporal_worker_manager = None
    app.state.temporal_pool = None
    temporal_init_task: asyncio.Task | None = None

    if settings.temporal_enabled:
        # The whole Temporal runtime story (dev-server supervision,
        # connect loop, executor/worker wiring, boot-time control
        # reconcile, resident dev-server watchdog) lives in
        # services.temporal.lifecycle — main.py only schedules it.
        from services.temporal.lifecycle import run_temporal_lifecycle

        logger.info(
            "Scheduling Temporal initialization in background",
            server_address=settings.temporal_server_address,
            namespace=settings.temporal_namespace,
            task_queue=settings.temporal_task_queue,
        )
        _startup_log(f"[Temporal] Init scheduled for {settings.temporal_server_address}")
        temporal_init_task = asyncio.create_task(
            run_temporal_lifecycle(app.state, settings, startup_log=_startup_log),
            name="temporal-init",
        )
    else:
        _startup_log("[Temporal] Disabled")

    # Enter the CLI-agent MCP server's lifespan so its
    # StreamableHTTPSessionManager task group is initialised (Starlette
    # does NOT auto-propagate lifespans across `app.mount`). Stored on
    # `app.state` so shutdown can exit the context cleanly.
    cli_mcp_lifespan_ctx = None
    try:
        from services.cli_agent.mcp_server import get_mcp_app as _get_cli_mcp_app

        _cli_mcp_app = _get_cli_mcp_app()
        cli_mcp_lifespan_ctx = _cli_mcp_app.router.lifespan_context(_cli_mcp_app)
        await cli_mcp_lifespan_ctx.__aenter__()
        app.state.cli_mcp_lifespan_ctx = cli_mcp_lifespan_ctx
        _startup_log("[CLI MCP] StreamableHTTP session manager initialised")
    except Exception as exc:
        logger.warning("[CLI MCP] lifespan init failed: %s", exc)

    # One-time status-broadcaster refresh: populates the cache and runs
    # the load-bearing auto-reconnects (Telegram bot via stored token,
    # Android relay via stored pairing). Per-WS-client refresh used to
    # do this on every connect, which produced an M-by-N storm under
    # PartySocket's auto-reconnect on every page nav / network blip --
    # the refresh now runs ONCE at startup, and state changes after
    # that flow through the originating code path's event-driven
    # broadcasts (whatsapp `_handle_event`, telegram `_broadcast_status`,
    # OAuth callbacks, android relay broadcaster).
    #
    # Spawned as a background task so a slow upstream (Telegram getMe,
    # Twitter token validation) doesn't block lifespan startup.
    asyncio.create_task(get_status_broadcaster()._refresh_all_services())

    _startup_log("All services initialized")
    _startup_log("Application startup complete")
    yield

    # Shutdown
    # Stop WebSocket logging handler
    shutdown_websocket_logging()

    # Cancel the Temporal lifecycle task (connect loop while starting;
    # resident dev-server watchdog once the engine is up).
    if temporal_init_task is not None and not temporal_init_task.done():
        temporal_init_task.cancel()
        try:
            await temporal_init_task
        except (asyncio.CancelledError, Exception):
            pass

    # Stop the per-queue worker pool first (activity-only workers), then
    # the manager worker that also hosts workflows.
    pool = getattr(app.state, "temporal_pool", None)
    if pool is not None:
        try:
            await pool.stop()
            logger.info("Temporal worker pool stopped")
        except Exception as exc:
            logger.warning(f"Temporal worker pool stop raised: {exc}")

    # Stop Temporal worker if it successfully started.
    worker_manager = getattr(app.state, "temporal_worker_manager", None)
    if worker_manager is not None:
        try:
            await worker_manager.stop()
            logger.info("Temporal worker stopped")
        except Exception as exc:
            logger.warning(f"Temporal worker stop raised: {exc}")

    # Disconnect Temporal client.
    if settings.temporal_enabled:
        try:
            temporal_client_wrapper = container.temporal_client()
            if temporal_client_wrapper is not None:
                await temporal_client_wrapper.disconnect()
        except Exception:
            pass

    # Shutdown proxy service
    from services.proxy.service import get_proxy_service

    _proxy_svc = get_proxy_service()
    if _proxy_svc:
        await _proxy_svc.shutdown()

    # Wave 12 C4 sub-piece B: drain every plugin that self-registered
    # a shutdown hook via services.plugin.shutdown_hooks. Today:
    # android relay client (prevents "Unclosed client session" warning),
    # browser service (prevents orphaned processes + EBUSY file locks).
    # New plugins with cleanup needs register a hook from their
    # __init__.py — main.py never edits.
    from services.plugin.shutdown_hooks import run_shutdown_hooks

    await run_shutdown_hooks()

    # Kill all managed processes (process manager node)
    from services.process_service import shutdown_process_service

    await shutdown_process_service()

    # Stop every supervisor that registered itself via
    # services._supervisor.register_supervisor() (WhatsApp runtime,
    # Node.js executor runtime, Temporal dev server).
    from services._supervisor import shutdown_all_supervisors

    await shutdown_all_supervisors()

    # Stop cleanup service
    if cleanup_service is not None:
        await cleanup_service.stop()

    # Stop recovery sweeper first
    if settings.redis_enabled:
        await recovery_sweeper.stop()
        logger.info("Execution recovery sweeper stopped")

    # Exit the CLI-agent MCP server's lifespan
    cli_mcp_lifespan_ctx = getattr(app.state, "cli_mcp_lifespan_ctx", None)
    if cli_mcp_lifespan_ctx is not None:
        try:
            await cli_mcp_lifespan_ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.debug("[CLI MCP] lifespan shutdown: %s", exc)

    # Drain cached native SDK clients before credentials/database teardown.
    await container.chat_unifier().aclose()
    await container.cache().shutdown()
    await container.database().shutdown()
    logger.info("Services shutdown complete")


# Interactive docs and the raw OpenAPI schema are in the middleware's public
# allowlist, so they were reachable unauthenticated on every deployment. Keep
# them for local development, where they are genuinely useful, and switch them
# off wherever auth is actually enforced.
_docs_settings = Settings()
_expose_docs = (
    (_docs_settings.vite_auth_enabled or "").lower() == "false"
    or _docs_settings.deployment_mode == "local"
)

# Create FastAPI app
app = FastAPI(
    title="OpenCompany API",
    version="3.0.0",
    description="OpenCompany workflow automation backend",
    lifespan=lifespan,
    docs_url="/docs" if _expose_docs else None,
    redoc_url="/redoc" if _expose_docs else None,
    openapi_url="/openapi.json" if _expose_docs else None,
)

# Add exception handler middleware BEFORE CORS to catch all errors
from fastapi import Request, status
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware


class CatchAllExceptionsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except Exception as e:
            # Full traceback goes to the structured log + file handler;
            # the HTTP response body intentionally carries a generic
            # message so an attacker hitting an unhandled-exception code
            # path can't enumerate stack frames, file paths, or library
            # versions. Matches the OWASP ``py/stack-trace-exposure``
            # mitigation pattern.
            logger.error("Unhandled exception", error_type=type(e).__name__, error=str(e), exc_info=True)
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={"success": False, "error": "Internal server error", "detail": "Internal server error"},
            )


app.add_middleware(CatchAllExceptionsMiddleware)

# Add Auth middleware (checks JWT cookie for protected routes)
from middleware.auth import AuthMiddleware

app.add_middleware(AuthMiddleware)

# Add CORS middleware (must be AFTER exception middleware)
logger.info("Configuring CORS middleware", origins_count=len(settings.cors_origins), origins=settings.cors_origins)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Include framework-level routers (everything that's NOT plugin-specific
# stays here). Plugin-owned routers self-register via
# ``services.ws_handler_registry.register_router`` from their plugin
# folder's ``__init__.py`` and are mounted via the loop below — main.py
# never imports a migrated plugin module by name.
app.include_router(auth.router)  # Auth routes (login, register, logout, status)
app.include_router(workflow.router)
app.include_router(database.router)
app.include_router(websocket.router)
app.include_router(credentials.router)  # Credentials panel - lazy per-tile icon endpoint (n8n pattern)
app.include_router(schemas.router)  # Per-node output schema endpoint (GET /api/schemas/nodes/{type}.json)
app.include_router(workspace.router)  # Per-workflow workspace file serving + uploads

# Routers awaiting migration into their plugin folders. As each plugin
# moves to the self-contained pattern (nodes/<plugin>/_router.py +
# register_router from __init__.py), the corresponding line below is
# removed. Tracked in the plugin-extraction plan.
app.include_router(webhook.router)
# Twitter, Google, Android, and Maps routers moved (Maps deleted in
# Wave 11.I milestone N -- all four endpoints were dead, the validate-key
# path now flows through CREDENTIAL_REGISTRY's GoogleMapsCredential._probe).

# Plugin-registered routers — populated by `nodes/<plugin>/__init__.py`
# at import time via `register_router(...)`. Plugins are imported during
# the node-discovery walk on app startup; iterating here picks up
# anything that registered.
from services.ws_handler_registry import get_routers as _get_plugin_routers

for _plugin_router in _get_plugin_routers():
    app.include_router(_plugin_router)


# ---------------------------------------------------------------------------
# CLI agent IDE MCP server (VSCode pattern)
# ---------------------------------------------------------------------------
#
# Spawned Claude Code / Codex CLI sessions auto-discover this server via
# a per-batch lockfile (~/.claude/ide/<pid>.lock) and call tools like
# mcp__opencompany__getSkill / getCredential / broadcastLog over MCP-over-HTTP.
# Bearer-token auth scoped per batch (see services/cli_agent/mcp_server.py).
try:
    from services.cli_agent.mcp_server import get_mcp_app as _get_cli_mcp_app

    _cli_mcp_app = _get_cli_mcp_app()
    app.mount("/mcp/ide", _cli_mcp_app)
    logger.info("[main] mounted CLI agent MCP server at /mcp/ide")
except Exception as exc:  # pragma: no cover — defensive; MCP must not block startup
    logger.warning("[main] failed to mount CLI agent MCP server: %s", exc)


# Stale lockfile sweep on startup — mirrors VSCode's behavior. PIDs in
# leftover lockfiles that are no longer alive get cleaned up.
@app.on_event("startup")
async def _sweep_cli_lockfiles_on_startup() -> None:
    try:
        from services.cli_agent.config import list_provider_names
        from services.cli_agent.factory import create_cli_provider
        from services.cli_agent.lockfile import sweep_stale_lockfiles

        # Ask the provider class for its lockfile dir rather than the
        # raw JSON config — the provider is the source of truth (e.g.
        # the claude provider derives its dir from
        # ``OPENCOMPANY_CLAUDE_DIR/ide`` so it stays in sync with the
        # ``CLAUDE_CONFIG_DIR`` env var we set on spawn). Reading from
        # the config dict would miss provider-class-computed paths.
        for name in list_provider_names():
            try:
                provider = create_cli_provider(name)
            except Exception:
                continue
            if provider.ide_lockfile_dir:
                sweep_stale_lockfiles(provider.ide_lockfile_dir)
    except Exception as exc:
        logger.debug("[main] CLI lockfile sweep failed: %s", exc)


@app.get("/health")
async def health_check():
    """Detailed health check with resource monitoring."""
    from services import event_waiter
    from services.execution import get_recovery_sweeper
    from core.health import get_health_status

    sweeper = get_recovery_sweeper()

    # Get comprehensive health status
    health = await get_health_status(database=container.database(), cache=container.cache(), settings=settings)

    # Check Temporal status
    temporal_status = {
        "enabled": settings.temporal_enabled,
        "connected": False,
    }
    if settings.temporal_enabled:
        try:
            temporal_client_wrapper = container.temporal_client()
            if temporal_client_wrapper is not None:
                temporal_status["connected"] = temporal_client_wrapper.is_connected
                temporal_status["server_address"] = settings.temporal_server_address
                temporal_status["task_queue"] = settings.temporal_task_queue
        except Exception:
            pass

    return {
        "status": health["status"],
        "service": "python",
        "version": "3.3.0",  # Bumped for daemon service support
        "environment": "development" if settings.debug else "production",
        "uptime_seconds": health["uptime_seconds"],
        "resources": {
            "memory_mb": health["memory_mb"],
            "disk_percent": health["disk_percent"],
            "cpu_percent": health["cpu_percent"],
        },
        "checks": health["checks"],
        "features": health["features"],
        "redis_enabled": settings.redis_enabled,
        "event_waiter_mode": event_waiter.get_backend_mode(),
        "execution_engine": {
            "enabled": settings.redis_enabled,
            "recovery_sweeper": sweeper is not None and sweeper._running,
        },
        "temporal": temporal_status,
        "timestamp": datetime.now().isoformat(),
    }


# ---------------------------------------------------------------------------
# Single-port SPA serving (container / Cloud Run)
# ---------------------------------------------------------------------------
#
# One uvicorn process fronts the API, the WebSocket, AND the built React
# client. This is the only static-serving path: ``company start`` and the
# containerised deployment both rely on it (``company dev`` uses Vite for
# HMR instead). Gated on the dist existing + SERVE_STATIC_CLIENT (default
# on) so a dist-less dev checkout is unaffected.
#
# Registered LAST so every real API / WS / mounted route above wins; this
# only catches otherwise-unmatched GET paths and returns the SPA shell for
# client-side routing. Non-SPA prefixes return 404 so API misses stay JSON.
import os as _spa_os
from pathlib import Path as _SpaPath
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

_CLIENT_DIST = _SpaPath(__file__).resolve().parents[1] / "client" / "dist"
_SERVE_STATIC = _spa_os.environ.get("SERVE_STATIC_CLIENT", "true").lower() in ("1", "true", "yes")
# Path prefixes owned by the backend — never shadowed by the SPA fallback.
_NON_SPA_PREFIXES = ("api/", "ws/", "webhook/", "mcp/", "health", "docs", "redoc", "openapi.json")

if _SERVE_STATIC and (_CLIENT_DIST / "index.html").is_file():
    # The SPA owns "/" — no other GET "/" route exists (the legacy
    # nodejs-compat router that used to shadow it was removed entirely).
    _assets_dir = _CLIENT_DIST / "assets"
    if _assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _serve_spa(full_path: str):
        """Serve a built static asset when it exists, else the SPA shell."""
        if full_path.startswith(_NON_SPA_PREFIXES):
            return JSONResponse(status_code=404, content={"detail": "Not Found"})
        candidate = (_CLIENT_DIST / full_path).resolve()
        # is_file() + containment check guard against ``..`` path traversal
        # escaping the build directory.
        if full_path and candidate.is_file() and _CLIENT_DIST in candidate.parents:
            return FileResponse(str(candidate))
        return FileResponse(str(_CLIENT_DIST / "index.html"))

    logger.info("Serving built client from %s", _CLIENT_DIST)


if __name__ == "__main__":
    import uvicorn

    logger.info("Starting React Flow Python Services", host=settings.host, port=settings.port, debug=settings.debug)
    uvicorn.run(
        "main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
        reload_dirs=["."] if settings.debug else None,
        reload_excludes=["*.pyc", "__pycache__", "*.log", "*.db"] if settings.debug else None,
        workers=1 if settings.debug else settings.workers,
    )
