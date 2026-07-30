"""Himalaya CLI wrapper service for IMAP/SMTP email operations.

Wraps the himalaya CLI (https://github.com/pimalaya/himalaya) to provide
email send/receive/manage capabilities via any IMAP/SMTP provider.
"""

import asyncio
import re
import shutil
import tempfile
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from typing import Any, List, Optional

from core.logging import get_logger
from services.plugin.singleton import ServiceSingleton

logger = get_logger(__name__)

# Himalaya accepts these three transport-security values; anything else is
# rejected at config-parse time with an error that names the config path.
_ENCRYPTIONS = ("tls", "start-tls", "none")

# TOML basic-string escapes, per the spec. Order matters: the backslash
# substitution must run first or it would double-escape the ones below it.
_TOML_ESCAPES = (
    ("\\", "\\\\"),
    ('"', '\\"'),
    ("\b", "\\b"),
    ("\t", "\\t"),
    ("\n", "\\n"),
    ("\f", "\\f"),
    ("\r", "\\r"),
)

_TOML_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


def _toml_str(value: Any) -> str:
    """Render ``value`` as a quoted TOML basic string.

    Returns the value *including* its surrounding quotes so call sites read
    ``f"backend.host = {_toml_str(host)}"`` and cannot forget them.

    Without this, any credential containing a ``"`` breaks the config, and a
    ``"`` followed by a newline injects arbitrary himalaya keys -- e.g. a
    ``backend.host`` pointing at someone else's server. App passwords are
    user-chosen, so that is reachable without any prior compromise.
    """
    text = "" if value is None else str(value)
    for raw, escaped in _TOML_ESCAPES:
        text = text.replace(raw, escaped)
    # Remaining C0 controls (and DEL) have no shorthand escape.
    text = _TOML_CONTROL_RE.sub(lambda m: f"\\u{ord(m.group()):04X}", text)
    return f'"{text}"'


def _toml_port(value: Any, default: int) -> int:
    """Coerce ``value`` to a valid TCP port, falling back to ``default``.

    ``resolve_credentials`` always emits the port keys, sometimes valued
    ``None`` -- so ``credentials.get("imap_port", 993)`` finds the key present
    and never applies its default, rendering ``backend.port = None``. Ports are
    interpolated unquoted, so that is unparseable TOML rather than a bad value.
    """
    try:
        port = int(value)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


def _toml_encryption(value: Any, default: str = "tls") -> str:
    """Return an allowlisted encryption mode as a quoted TOML string."""
    text = "" if value is None else str(value).strip().lower()
    return _toml_str(text if text in _ENCRYPTIONS else default)


def _scrub(text: str, *secrets: str) -> str:
    """Redact ``secrets`` from ``text``.

    The himalaya error string is not log-only: it becomes the ``RuntimeError``
    message, which becomes the node error envelope, which is persisted and
    broadcast over the WebSocket. Scrubbing only the log would leave the secret
    in the payload the frontend renders.
    """
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


class HimalayaService(ServiceSingleton):
    """Manages Himalaya CLI configuration and execution.

    Inherits ``instance`` / ``reset_instance`` from
    :class:`ServiceSingleton`."""

    def __init__(self):
        self._binary_path: Optional[str] = None

    async def ensure_binary(self) -> str:
        """Detect himalaya binary in PATH. Returns path or raises."""
        if self._binary_path:
            return self._binary_path

        binary = shutil.which("himalaya")
        if binary:
            self._binary_path = binary
            logger.info(f"[Himalaya] Found binary: {binary}")
            return binary

        raise RuntimeError(
            "himalaya CLI not found in PATH. "
            "Install via: cargo install himalaya, brew install himalaya, "
            "or download from https://github.com/pimalaya/himalaya/releases"
        )

    def _timeout_seconds(self) -> float:
        """Per-invocation CLI timeout, from ``cli.timeout_seconds``.

        Imported lazily: ``_service`` reaches back into this module for the
        singleton, so a module-level import would be circular.
        """
        from ._service import _load_config

        raw = _load_config().get("cli", {}).get("timeout_seconds", 60)
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 60.0

    def _generate_config(self, account_name: str, credentials: dict) -> str:
        """Generate TOML config content for a himalaya account.

        Every interpolated value goes through ``_toml_str`` / ``_toml_port`` /
        ``_toml_encryption``. Do not add a raw f-string interpolation here --
        see the note on ``_toml_str`` for what that costs.
        """
        email = credentials.get("email", "")
        display_name = credentials.get("display_name", "")
        password = credentials.get("password", "")

        imap_host = credentials.get("imap_host", "")
        imap_port = _toml_port(credentials.get("imap_port"), 993)
        imap_encryption = _toml_encryption(credentials.get("imap_encryption"))

        smtp_host = credentials.get("smtp_host", "")
        smtp_port = _toml_port(credentials.get("smtp_port"), 465)
        smtp_encryption = _toml_encryption(credentials.get("smtp_encryption"))

        lines = [
            f"[accounts.{account_name}]",
            f"email = {_toml_str(email)}",
        ]
        if display_name:
            lines.append(f"display-name = {_toml_str(display_name)}")

        # IMAP backend
        lines.extend(
            [
                "",
                'backend.type = "imap"',
                f"backend.host = {_toml_str(imap_host)}",
                f"backend.port = {imap_port}",
                f"backend.encryption = {imap_encryption}",
                f"backend.login = {_toml_str(email)}",
                'backend.auth.type = "password"',
                f"backend.auth.raw = {_toml_str(password)}",
            ]
        )

        # SMTP sender
        lines.extend(
            [
                "",
                'message.send.backend.type = "smtp"',
                f"message.send.backend.host = {_toml_str(smtp_host)}",
                f"message.send.backend.port = {smtp_port}",
                f"message.send.backend.encryption = {smtp_encryption}",
                f"message.send.backend.login = {_toml_str(email)}",
                'message.send.backend.auth.type = "password"',
                f"message.send.backend.auth.raw = {_toml_str(password)}",
            ]
        )

        return "\n".join(lines)

    async def execute(
        self,
        account_name: str,
        credentials: dict,
        args: List[str],
        stdin_data: Optional[str] = None,
    ) -> dict:
        """Execute himalaya CLI command and return parsed JSON output.

        Delegates the subprocess lifecycle to the shared ``run_cli_command``
        so the kill-on-timeout guarantee is the same one every other CLI
        plugin gets, then adapts the envelope back to this method's
        raise-on-failure contract.
        """
        from services.events.cli import run_cli_command

        binary = await self.ensure_binary()
        config_content = self._generate_config(account_name, credentials)
        timeout = self._timeout_seconds()

        # NamedTemporaryFile is 0600 + O_EXCL, so the window is narrow --
        # but it is still a plaintext password on disk for the life of the
        # call. See docs-internal/email_service.md for the backend.auth.cmd
        # follow-up that would remove it entirely.
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".toml", prefix="himalaya_", delete=False)
        try:
            tmp.write(config_content)
            tmp.flush()
            tmp.close()

            logger.debug(f"[Himalaya] Executing: himalaya {' '.join(args)}")

            stdin_bytes = stdin_data.encode("utf-8") if stdin_data else None
            envelope = await run_cli_command(
                binary=binary,
                argv=["-c", tmp.name, "-a", account_name, "--output", "json", *args],
                timeout=timeout,
                stdin=asyncio.subprocess.PIPE if stdin_bytes else None,
                input=stdin_bytes,
            )

            if not envelope.get("success"):
                error_msg = (
                    envelope.get("error")
                    or envelope.get("stderr")
                    or envelope.get("stdout")
                    or "unknown failure"
                )
                # himalaya echoes the config path -- and on a parse failure,
                # config content -- into stderr. This string becomes the
                # RuntimeError message, the node error envelope, a persisted
                # node output, and a WebSocket broadcast, so scrub before it
                # leaves this function rather than only before the log call.
                error_msg = _scrub(error_msg, credentials.get("password", ""))
                logger.warning(f"[Himalaya] Command failed: {error_msg}")
                raise RuntimeError(f"himalaya error: {error_msg}")

            stdout_str = envelope.get("stdout") or ""
            if not stdout_str:
                return {}

            parsed = envelope.get("result")
            if parsed is None:
                return {"raw_output": stdout_str}
            return parsed

        finally:
            # Now actually effective on the timeout path: run_cli_command
            # tree-kills the child first, so the handle is released.
            Path(tmp.name).unlink(missing_ok=True)

    # =========================================================================
    # HIGH-LEVEL OPERATIONS
    # =========================================================================

    async def send_email(
        self,
        credentials: dict,
        to: str,
        subject: str,
        body: str,
        cc: str = "",
        bcc: str = "",
        body_type: str = "text",
    ) -> dict:
        """Send an email via SMTP. Composes RFC 2822 and pipes to himalaya."""
        account = self._account_name(credentials)

        if body_type == "html":
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(body, "html", "utf-8"))
        else:
            msg = MIMEText(body, "plain", "utf-8")

        msg["From"] = credentials.get("email", "")
        msg["To"] = to
        msg["Subject"] = subject
        if cc:
            msg["Cc"] = cc
        if bcc:
            msg["Bcc"] = bcc

        return await self.execute(
            account,
            credentials,
            ["message", "send"],
            stdin_data=msg.as_string(),
        )

    async def list_envelopes(
        self,
        credentials: dict,
        folder: str = "INBOX",
        page: int = 1,
        page_size: int = 20,
    ) -> dict:
        """List email envelopes in a folder."""
        account = self._account_name(credentials)
        return await self.execute(
            account,
            credentials,
            [
                "envelope",
                "list",
                "-f",
                folder,
                "--page",
                str(page),
                "--page-size",
                str(page_size),
            ],
        )

    async def search_envelopes(
        self,
        credentials: dict,
        query: str,
        folder: str = "INBOX",
    ) -> dict:
        """Search email envelopes by query."""
        account = self._account_name(credentials)
        return await self.execute(
            account,
            credentials,
            ["envelope", "list", "-f", folder, "--query", query],
        )

    async def read_message(
        self,
        credentials: dict,
        message_id: str,
        folder: str = "INBOX",
    ) -> dict:
        """Read full message content."""
        account = self._account_name(credentials)
        return await self.execute(
            account,
            credentials,
            ["message", "read", message_id, "-f", folder],
        )

    async def move_message(
        self,
        credentials: dict,
        message_id: str,
        target_folder: str,
        folder: str = "INBOX",
    ) -> dict:
        """Move a message to another folder."""
        account = self._account_name(credentials)
        return await self.execute(
            account,
            credentials,
            ["message", "move", message_id, target_folder, "-f", folder],
        )

    async def delete_message(
        self,
        credentials: dict,
        message_id: str,
        folder: str = "INBOX",
    ) -> dict:
        """Delete a message."""
        account = self._account_name(credentials)
        return await self.execute(
            account,
            credentials,
            ["message", "delete", message_id, "-f", folder],
        )

    async def flag_message(
        self,
        credentials: dict,
        message_id: str,
        flag: str,
        action: str = "add",
        folder: str = "INBOX",
    ) -> dict:
        """Add or remove a flag on a message."""
        account = self._account_name(credentials)
        flag_cmd = "add" if action == "add" else "remove"
        return await self.execute(
            account,
            credentials,
            ["flag", flag_cmd, message_id, "--flag", flag, "-f", folder],
        )

    async def list_folders(self, credentials: dict) -> dict:
        """List all mailbox folders."""
        account = self._account_name(credentials)
        return await self.execute(account, credentials, ["folder", "list"])

    def _account_name(self, credentials: dict) -> str:
        """Generate a consistent account name from credentials.

        The result lands in two places with different rules: the TOML table
        header ``[accounts.X]``, where only bare-key characters are legal, and
        argv as ``-a X``, where a leading ``-`` is parsed as a flag. Email
        local-parts legally contain ``!#$%&'*/=?^`{|}~``, none of which are
        valid bare-key characters -- ``o'brien@x.com`` used to produce
        ``[accounts.o'brien]`` and a config parse error.
        """
        email = credentials.get("email") or "default"
        local_part = str(email).split("@")[0]
        name = re.sub(r"[^A-Za-z0-9_-]", "_", local_part).lstrip("-")
        return name or "default"


def get_himalaya_service() -> HimalayaService:
    """Get singleton instance."""
    return HimalayaService.instance()
