"""Discord interaction signature verifier.

Headers: ``X-Signature-Ed25519`` (hex) and ``X-Signature-Timestamp``.
Signed payload: ``timestamp + raw_body``.
Reference: https://discord.com/developers/docs/interactions/receiving-and-responding

The first asymmetric verifier in the tree -- the four shipped ones are all
HMAC. The "secret" here is the application's Ed25519 *public* key, which is
not secret at all; it rides the same ``secret_field`` plumbing because what
that plumbing does is fetch one stored string.

Verification runs over the raw request body, never a re-serialised JSON dict:
any difference in key order or separators changes the bytes and the signature
stops matching for authentic requests.
"""

from __future__ import annotations

from typing import Mapping

from services.events.verifiers.base import WebhookVerifier


class DiscordEd25519Verifier(WebhookVerifier):
    @classmethod
    def verify(cls, headers: Mapping[str, str], body: bytes, secret: str) -> None:
        # Imported here rather than at module scope so importing the plugin
        # never depends on the crypto backend loading.
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        signature = cls._header(headers, "x-signature-ed25519")
        timestamp = cls._header(headers, "x-signature-timestamp")
        if not signature or not timestamp:
            raise ValueError("X-Signature-Ed25519 or X-Signature-Timestamp header missing")

        try:
            public_key = Ed25519PublicKey.from_public_bytes(bytes.fromhex(secret))
        except ValueError as exc:
            raise ValueError(f"Discord public key is not valid hex: {exc}") from exc

        try:
            signature_bytes = bytes.fromhex(signature)
        except ValueError as exc:
            raise ValueError("X-Signature-Ed25519 is not valid hex") from exc

        try:
            public_key.verify(signature_bytes, timestamp.encode() + body)
        except InvalidSignature as exc:
            raise ValueError("Discord signature mismatch") from exc


__all__ = ["DiscordEd25519Verifier"]
