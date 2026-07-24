"""Field-level encryption using Fernet (AES-128-CBC + HMAC-SHA256).

This module provides secure encryption for API keys and OAuth tokens stored in
the credentials database. Uses PBKDF2HMAC for key derivation from a server-scoped
encryption key (API_KEY_ENCRYPTION_KEY in .env), following the n8n pattern.

Security:
- Fernet: AES-128-CBC with PKCS7 padding + HMAC-SHA256 for authentication
- PBKDF2: SHA256, 600,000 iterations (OWASP 2024 recommendation)
- Key derived from server config, initialized at startup, persists across restarts
"""

import base64
import hashlib
import logging
import os
from typing import Dict, Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# Deterministic credential fingerprinting for in-memory cache keys. The salt is
# a fixed context string (not a secret): fingerprints must be stable across
# processes and restarts, so a random per-process salt would break cache-key
# identity. PBKDF2 (not plain SHA-256) because credentials require a
# computationally expensive hash (CodeQL py/weak-sensitive-data-hashing);
# results are memoized so the KDF cost is paid once per credential per process.
_FINGERPRINT_SALT = b"opencompany-credential-fingerprint-v1"
_fingerprint_cache: Dict[str, str] = {}


def fingerprint_credential(credential: str) -> str:
    """Stable hex fingerprint of a credential (API key, token) for cache keys.

    Never use for password verification storage; this is for deriving
    non-reversible in-memory cache-key components from credentials.
    """
    cached = _fingerprint_cache.get(credential)
    if cached is None:
        cached = hashlib.pbkdf2_hmac(
            "sha256",
            credential.encode("utf-8"),
            _FINGERPRINT_SALT,
            EncryptionService.PBKDF2_ITERATIONS,
        ).hex()
        _fingerprint_cache[credential] = cached
    return cached


class EncryptionService:
    """
    Fernet-based encryption service with PBKDF2 key derivation.

    Uses a server-scoped encryption key (API_KEY_ENCRYPTION_KEY from .env)
    following the n8n pattern. Initialized once at startup, persists across
    the application lifecycle. Not tied to user sessions.

    Usage:
        encryption = EncryptionService()
        salt = await credentials_db.get_or_create_salt()
        encryption.initialize(server_key, salt)

        encrypted = encryption.encrypt("my-api-key")
        decrypted = encryption.decrypt(encrypted)
    """

    # OWASP recommended iterations for PBKDF2-SHA256 (2024)
    PBKDF2_ITERATIONS = 600_000
    SALT_LENGTH = 32  # 256 bits

    def __init__(self):
        self._fernet: Optional[Fernet] = None
        self._salt: Optional[bytes] = None

    def derive_key_from_password(self, password: str, salt: bytes) -> bytes:
        """
        Derive Fernet-compatible key from passphrase using PBKDF2.

        Args:
            password: Server-scoped encryption key (API_KEY_ENCRYPTION_KEY)
            salt: Random salt (stored in credentials DB)

        Returns:
            Base64-encoded 32-byte key suitable for Fernet
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,  # Fernet uses 32-byte keys
            salt=salt,
            iterations=self.PBKDF2_ITERATIONS,
        )
        key = kdf.derive(password.encode())
        # Fernet requires URL-safe base64-encoded key
        return base64.urlsafe_b64encode(key)

    def initialize(self, password: str, salt: bytes) -> None:
        """
        Initialize Fernet cipher with key derived from passphrase.

        Called once at server startup. The derived key is stored only in memory.

        Args:
            password: Server-scoped encryption key (API_KEY_ENCRYPTION_KEY)
            salt: Random salt from credentials database
        """
        key = self.derive_key_from_password(password, salt)
        self._fernet = Fernet(key)
        self._salt = salt
        logger.debug("Encryption service initialized")

    def encrypt(self, plaintext: str) -> str:
        """
        Encrypt string using Fernet.

        Args:
            plaintext: Data to encrypt (e.g., API key)

        Returns:
            Base64-encoded Fernet token (ciphertext)

        Raises:
            RuntimeError: If encryption service not initialized
        """
        if not self._fernet:
            raise RuntimeError("Encryption service not initialized - check server startup")
        token = self._fernet.encrypt(plaintext.encode())
        return token.decode()  # Fernet tokens are already base64

    def decrypt(self, ciphertext: str) -> str:
        """
        Decrypt Fernet token to plaintext string.

        Args:
            ciphertext: Base64-encoded Fernet token

        Returns:
            Decrypted plaintext string

        Raises:
            RuntimeError: If encryption service not initialized
            ValueError: If decryption fails (wrong key or corrupted data)
        """
        if not self._fernet:
            raise RuntimeError("Encryption service not initialized - check server startup")
        try:
            plaintext = self._fernet.decrypt(ciphertext.encode())
            return plaintext.decode()
        except InvalidToken as e:
            logger.error("Decryption failed - invalid key or corrupted data")
            raise ValueError("Decryption failed - invalid key or corrupted data") from e

    def is_initialized(self) -> bool:
        """Check if encryption service is ready for use."""
        return self._fernet is not None

    def clear(self) -> None:
        """
        Clear encryption key from memory.

        Generally not needed in normal operation since the key persists
        for the application lifecycle (server-scoped, not session-scoped).
        """
        self._fernet = None
        self._salt = None
        logger.debug("Encryption service cleared")

    @staticmethod
    def generate_salt() -> bytes:
        """
        Generate cryptographically secure random salt.

        Returns:
            32-byte random salt for PBKDF2 key derivation
        """
        return os.urandom(EncryptionService.SALT_LENGTH)
