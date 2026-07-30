"""Email service - credential resolution + HimalayaService orchestration.

All defaults and constants loaded from config/email_providers.json.
"""

import json
from pathlib import Path
from typing import Any, Dict, Optional, Set

from core.logging import get_logger
from services.plugin.singleton import ServiceSingleton

logger = get_logger(__name__)

_CONFIG: Optional[Dict] = None
_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "email_providers.json"


def _load_config() -> Dict:
    global _CONFIG
    if _CONFIG is None:
        with open(_CONFIG_PATH) as f:
            _CONFIG = json.load(f)
    return _CONFIG


class EmailService(ServiceSingleton):
    """Plugin-owned email orchestrator. Inherits ``instance`` /
    ``reset_instance`` from :class:`ServiceSingleton`."""

    @property
    def config(self) -> Dict:
        return _load_config()

    @property
    def defaults(self) -> Dict:
        return self.config.get("defaults", {})

    @property
    def polling(self) -> Dict:
        return self.config.get("polling", {})

    @property
    def himalaya(self):
        from ._himalaya import get_himalaya_service

        return get_himalaya_service()

    def _provider_preset(self, name: str) -> Dict:
        return self.config.get("providers", {}).get(name, {})

    # -- credentials --

    async def resolve_credentials(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Merge provider presets with stored credential keys.

        Precedence (per field): provider preset > stored key. Stored keys
        (``email_imap_host``, ``email_smtp_port``, ...) apply when the preset
        leaves the field blank, which is how the ``custom`` provider works.

        Credentials are deliberately NOT readable from node parameters. They
        used to be the documented top tier, but the fields were never declared
        on any Params model and all three set ``extra="ignore"``, so every
        ``params.get("password")`` silently returned None. Declaring them to
        revive the tier is not an option either: ``ToolNode.as_tool_schema``
        dumps ``Params.model_json_schema()`` wholesale with no field-exclusion
        hook, so a declared ``password`` becomes an argument the LLM can pass
        to a callable tool. ``provider`` stays because it IS declared.
        """
        from services.plugin.deps import get_auth_service

        auth = get_auth_service()

        provider = params.get("provider") or await auth.get_api_key("email_provider") or self.defaults.get("provider")
        preset = self._provider_preset(provider)

        email = await auth.get_api_key("email_address") or ""
        password = await auth.get_api_key("email_password") or ""

        if not email:
            raise ValueError("Email address not configured")
        if not password:
            raise ValueError("Email password not configured")

        def _coerce_port(value: Any) -> Any:
            if value in (None, ""):
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        imap_host = preset.get("imap_host") or await auth.get_api_key("email_imap_host") or ""
        imap_port = preset.get("imap_port") or _coerce_port(await auth.get_api_key("email_imap_port"))
        imap_encryption = preset.get("imap_encryption") or await auth.get_api_key("email_imap_encryption")
        smtp_host = preset.get("smtp_host") or await auth.get_api_key("email_smtp_host") or ""
        smtp_port = preset.get("smtp_port") or _coerce_port(await auth.get_api_key("email_smtp_port"))
        smtp_encryption = preset.get("smtp_encryption") or await auth.get_api_key("email_smtp_encryption")

        return {
            "email": email,
            "password": password,
            "display_name": await auth.get_api_key("email_display_name") or "",
            "imap_host": imap_host,
            "imap_port": imap_port,
            "imap_encryption": imap_encryption,
            "smtp_host": smtp_host,
            "smtp_port": smtp_port,
            "smtp_encryption": smtp_encryption,
        }

    def resolve_poll_params(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve polling parameters from node params + JSON polling config.

        Every read coerces rather than trusting the value: ``emailReceive``
        overrides ``execute()`` and passes raw parameters, so the Pydantic
        ``ge=30, le=3600`` guard never runs on this path and ``poll_interval``
        can arrive as ``None`` or a string. ``dict.get(key, default)`` only
        substitutes when the key is *absent*, so an explicit None reached
        ``min()`` and raised TypeError. Mirrors ``PollingTriggerNode._clamp_interval``.
        """
        p = self.polling
        try:
            interval = int(params.get("poll_interval") or p.get("interval"))
        except (TypeError, ValueError):
            interval = int(p.get("interval"))
        interval = max(int(p.get("min_interval")), min(int(p.get("max_interval")), interval))
        return {
            "interval": interval,
            "folder": params.get("folder") or self.defaults.get("folder"),
            "mark_as_read": bool(params.get("mark_as_read")),
        }

    # -- operations --

    async def send(self, params: Dict[str, Any]) -> Dict[str, Any]:
        creds = await self.resolve_credentials(params)
        d = self.defaults
        result = await self.himalaya.send_email(
            creds,
            to=params.get("to", ""),
            subject=params.get("subject", ""),
            body=params.get("body", ""),
            cc=params.get("cc", ""),
            bcc=params.get("bcc", ""),
            body_type=params.get("body_type", d.get("body_type")),
        )
        return {"from": creds["email"], **(result if isinstance(result, dict) else {})}

    async def read(self, params: Dict[str, Any]) -> Dict[str, Any]:
        creds = await self.resolve_credentials(params)
        d = self.defaults
        op = params.get("operation", "list")
        folder = params.get("folder", d.get("folder"))

        router = {
            "list": (
                "list_envelopes",
                {"folder": folder, "page": params.get("page", 1), "page_size": params.get("page_size", d.get("page_size"))},
            ),
            "search": ("search_envelopes", {"query": params.get("query", ""), "folder": folder}),
            "read": ("read_message", {"message_id": params.get("message_id", ""), "folder": folder}),
            "folders": ("list_folders", {}),
            "move": (
                "move_message",
                {"message_id": params.get("message_id", ""), "target_folder": params.get("target_folder", ""), "folder": folder},
            ),
            "delete": ("delete_message", {"message_id": params.get("message_id", ""), "folder": folder}),
            "flag": (
                "flag_message",
                {
                    "message_id": params.get("message_id", ""),
                    "flag": params.get("flag", d.get("flag")),
                    "action": params.get("flag_action", d.get("flag_action")),
                    "folder": folder,
                },
            ),
        }

        if op not in router:
            raise ValueError(f"Unknown operation: {op}")

        method_name, kwargs = router[op]
        data = await getattr(self.himalaya, method_name)(creds, **kwargs)

        result = {"operation": op, "folder": folder}
        if isinstance(data, dict):
            result.update(data)
        else:
            result["data"] = data
        return result

    # -- polling helpers --

    async def poll_ids(self, creds: dict, folder: str = None) -> Set[str]:
        if folder is None:
            folder = self.defaults.get("folder")
        page_size = self.polling.get("baseline_page_size")
        result = await self.himalaya.list_envelopes(creds, folder=folder, page_size=page_size)
        envs = result if isinstance(result, list) else result.get("data", [])
        return {str(e.get("id") or e.get("uid", "")) for e in envs if e.get("id") or e.get("uid")}

    async def fetch_detail(self, creds: dict, msg_id: str, folder: str = None) -> Dict:
        if folder is None:
            folder = self.defaults.get("folder")
        result = await self.himalaya.read_message(creds, msg_id, folder=folder)
        data = result if isinstance(result, dict) else {"raw": result}
        data.update(message_id=msg_id, folder=folder)
        return data


def get_email_service() -> EmailService:
    return EmailService.instance()
