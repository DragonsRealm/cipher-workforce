"""Shared HTTP plumbing for the Discord nodes.

One place owns the API version pin, the request helper, the path guard and the
error translation, so the node files hold message shaping and nothing else.

REST lives here rather than going through discord.py's HTTPClient. The nodes
must work with no gateway connection at all, ``discordAction`` needs arbitrary
routes the library's typed methods do not expose, and the invalid-request
guard has to be process-wide across accounts, which a per-client limiter
cannot express.
"""

from __future__ import annotations

import json as jsonlib
from typing import Any, Dict, Optional, Tuple

import httpx

from core.logging import get_logger
from services.plugin import ActionNode, NodeUserError

from . import _ratelimit
from ._accounts import CREDENTIAL_ID, DEFAULT_ACCOUNT, resolve_secrets

logger = get_logger(__name__)


class AccountScopedNode(ActionNode, abstract=True):
    """ActionNode whose ``server_controlled_fields`` survive the tool path.

    ``BaseNode.execute_as_tool`` enforces ``server_controlled_fields`` only in
    its ToolNode branch. A dual-purpose ActionNode takes an earlier return
    that merges ``{**node_params, **tool_args}`` with model arguments winning,
    so the declaration alone protects nothing here.

    That matters because inbound Discord messages are the realistic source of
    hostile tool arguments, and ``account_id`` selects which bot identity
    sends. Stripping the locked fields from the model's arguments before the
    merge is what makes the declaration real. whatsapp_business solves the
    same problem by sourcing its sending number from the credential instead;
    that is not available here, because choosing between several stored
    accounts is the point.
    """

    async def execute_as_tool(
        self,
        tool_args: Dict[str, Any],
        node_params: Dict[str, Any],
        context: Any,
    ) -> Dict[str, Any]:
        locked = getattr(type(self), "server_controlled_fields", frozenset())
        sanitized = {k: v for k, v in (tool_args or {}).items() if k not in locked}
        return await super().execute_as_tool(sanitized, node_params, context)

API_BASE_URL = "https://discord.com/api"

# Pinned, never interpolated per call site. Discord keeps old versions alive
# for a long time, so drift shows up as behaviour changes rather than errors.
API_VERSION = "v10"

# Discord blocks requests without a descriptive User-Agent. The format is
# documented and enforced.
USER_AGENT = "DiscordBot (https://github.com/trohitg/opencompany, 1.0)"

REQUEST_TIMEOUT_SECONDS = 30.0

# Only these hosts may receive a bot token or a webhook post.
_ALLOWED_HOSTS = frozenset({"discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com"})


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------
#
# Discord returns a numeric JSON error code alongside the HTTP status. The
# split that matters is retryable-vs-terminal: NodeUserError is in the shared
# non-retryable list, so classifying a transient failure as one fails fast
# instead of backing off, and classifying a permanent rejection as retryable
# burns three attempts to reach the same answer.
#
# Codes from discord.com/developers/topics/opcodes-and-status-codes

# The token is wrong or was reset. Retrying with the same token cannot help.
# Code 0 is deliberately absent: Discord uses it for a general/malformed
# request, and it is also what an empty error body parses to, so treating it
# as auth would mislabel every unclassified failure.
_AUTH_CODES = frozenset({40001, 50014})

# The bot is authenticated but lacks access. Terminal until someone changes
# permissions in Discord.
_PERMISSION_CODES = frozenset({50001, 50013, 50021, 160002})

# The target does not exist, or the request is malformed. User-correctable.
_NOT_FOUND_CODES = frozenset({10003, 10004, 10008, 10013, 10062})


def classify_error(code: int, status_code: int) -> Tuple[str, bool]:
    """Return ``(category, retryable)`` for a Discord error."""
    if code in _AUTH_CODES or status_code == 401:
        return "auth", False
    if code in _PERMISSION_CODES or status_code == 403:
        return "permission", False
    if code in _NOT_FOUND_CODES or status_code == 404:
        return "not_found", False
    if status_code == 429:
        return "throttle", True
    if status_code >= 500:
        return "transient", True
    return "unknown", False


def _friendly_message(code: int, category: str, detail: str) -> str:
    if category == "auth":
        return (
            f"Discord rejected the bot token. Check it in the Credentials modal; "
            f"a reset token must be re-copied from the Developer Portal. "
            f"(error {code}: {detail})"
        )
    if category == "permission":
        return (
            f"The bot lacks permission for this action. Check its role and the "
            f"channel's permission overwrites in Discord. (error {code}: {detail})"
        )
    if category == "not_found":
        return (
            f"Discord could not find that resource. Check the id, and that the "
            f"bot is in the server. (error {code}: {detail})"
        )
    return f"Discord API error {code}: {detail}"


def raise_for_discord_error(payload: Dict[str, Any], status_code: int) -> None:
    """Translate a Discord error body into the right exception type."""
    code = int((payload or {}).get("code") or 0)
    detail = (payload or {}).get("message") or "no detail provided"
    category, retryable = classify_error(code, status_code)

    logger.warning(
        "discord api error",
        code=code,
        category=category,
        retryable=retryable,
        status_code=status_code,
        errors=(payload or {}).get("errors"),
    )

    if category == "auth":
        # An annotated PermissionError gets the framework's credential
        # envelope and a reconnect affordance that a NodeUserError does not.
        exc = PermissionError(_friendly_message(code, category, detail))
        exc.provider = CREDENTIAL_ID  # type: ignore[attr-defined]
        exc.reason = "invalid"  # type: ignore[attr-defined]
        exc.auth = "api_key"  # type: ignore[attr-defined]
        raise exc

    if retryable:
        raise RuntimeError(_friendly_message(code, category, detail))

    raise NodeUserError(_friendly_message(code, category, detail))


# --------------------------------------------------------------------------
# Path handling
# --------------------------------------------------------------------------


def build_url(path: str) -> str:
    """Join a relative API path onto the pinned base, refusing anything else.

    ``discordAction``'s custom operation lets a workflow name its own route.
    Without this guard that is an SSRF primitive that would send the bot token
    to whatever host the path pointed at, so the check lives here rather than
    in the node -- no future call site can skip it.
    """
    candidate = (path or "").strip()
    if not candidate:
        raise NodeUserError("An API path is required, for example 'users/@me'.")
    if "://" in candidate or candidate.startswith("//"):
        raise NodeUserError(
            f"'{path}' looks like a full URL. Give a path relative to the Discord "
            "API instead, for example 'channels/123/messages'."
        )
    if ".." in candidate:
        raise NodeUserError(f"'{path}' may not contain '..'.")
    return f"{API_BASE_URL}/{API_VERSION}/{candidate.lstrip('/')}"


def assert_discord_host(url: str) -> None:
    """Refuse to post to anything but Discord.

    Used for the webhook URL, which is operator-supplied and is itself the
    credential -- posting it anywhere else both leaks it and is an SSRF.
    """
    host = httpx.URL(url).host
    if host not in _ALLOWED_HOSTS:
        raise NodeUserError(
            f"'{host}' is not a Discord host. A webhook URL looks like "
            "https://discord.com/api/webhooks/<id>/<token>."
        )


# --------------------------------------------------------------------------
# Requests
# --------------------------------------------------------------------------


def _parse(response: httpx.Response) -> Dict[str, Any]:
    if not response.content:
        return {}
    try:
        parsed = response.json()
    except (ValueError, jsonlib.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {"data": parsed}


async def request(
    method: str,
    path: str,
    *,
    account_id: str = DEFAULT_ACCOUNT,
    json: Optional[Any] = None,
    params: Optional[Dict[str, Any]] = None,
    files: Optional[Any] = None,
    data: Optional[Dict[str, Any]] = None,
    token: Optional[str] = None,
) -> Any:
    """Call the Discord API for one account and translate failures.

    Args:
        token: Pre-resolved bot token. Saves a credential read when the caller
            already holds one; otherwise it is resolved from ``account_id``.
    """
    url = build_url(path)
    bot_token = token or (await resolve_secrets(account_id))["token"]

    guard = _ratelimit.invalid_request_guard()
    guard.check()
    limiter = _ratelimit.limiter_for(account_id)
    await limiter.acquire()

    headers = {
        # Not ApiKeyCredential.inject: its "bearer" mode hardcodes the literal
        # "Bearer " prefix, and Discord bot auth is "Bot <token>".
        "Authorization": f"Bot {bot_token}",
        "User-Agent": USER_AGENT,
    }

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        response = await client.request(
            method, url, headers=headers, json=json, params=params, files=files, data=data
        )

    guard.record(response.status_code)

    if response.status_code == 429:
        response_headers = dict(response.headers)
        if _ratelimit.is_cloudflare_ban(response_headers):
            raise NodeUserError(
                "Discord's edge is rejecting this host's requests outright, which "
                "happens after too many invalid requests. It clears on its own; "
                "check that stored bot tokens are valid before retrying."
            )
        payload = _parse(response)
        retry_after = _ratelimit.parse_retry_after(payload, response_headers)
        if _ratelimit.is_global_limit(payload, response_headers):
            limiter.hold(retry_after)
        raise _ratelimit.RateLimitExceeded(retry_after)

    payload = _parse(response)
    if response.status_code >= 400:
        raise_for_discord_error(payload, response.status_code)

    # 204 No Content is the success shape for deletes.
    return payload


async def get(path: str, *, account_id: str = DEFAULT_ACCOUNT, params: Optional[Dict[str, Any]] = None) -> Any:
    return await request("GET", path, account_id=account_id, params=params)


async def post(
    path: str,
    body: Optional[Any] = None,
    *,
    account_id: str = DEFAULT_ACCOUNT,
    files: Optional[Any] = None,
    data: Optional[Dict[str, Any]] = None,
) -> Any:
    return await request("POST", path, account_id=account_id, json=body, files=files, data=data)


async def patch(path: str, body: Optional[Any] = None, *, account_id: str = DEFAULT_ACCOUNT) -> Any:
    return await request("PATCH", path, account_id=account_id, json=body)


async def delete(path: str, *, account_id: str = DEFAULT_ACCOUNT) -> Any:
    return await request("DELETE", path, account_id=account_id)


__all__ = [
    "API_BASE_URL",
    "API_VERSION",
    "USER_AGENT",
    "assert_discord_host",
    "build_url",
    "classify_error",
    "delete",
    "get",
    "patch",
    "post",
    "raise_for_discord_error",
    "request",
]
