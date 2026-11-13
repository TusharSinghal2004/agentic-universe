"""Security and PII protection utilities.

Implements:
1. Deterministic blind indexing via HMAC-SHA256: Allows exact matches on phone
   numbers between lead records and incoming call events without storing or indexing
   plaintext phone numbers.
2. Authenticated encryption at rest via AES-256-GCM: Ensures confidentiality
   and integrity for sensitive customer data (name, phone).
3. Canonical masking: Standardized masked display for reports (+91******XXXX).
"""
from __future__ import annotations

import hashlib
import hmac
import os
from typing import Optional

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecurityService:
    def __init__(self, secret_key: str | None = None, hmac_salt: str | None = None):
        secret_key = secret_key or os.environ.get("PII_SECRET_KEY")
        hmac_salt = hmac_salt or os.environ.get("HMAC_SALT")
        if not secret_key or not hmac_salt:
            raise ValueError("Set PII_SECRET_KEY and HMAC_SALT before ingestion; keep both stable for this database.")
        self._aes_key = hashlib.sha256(secret_key.encode("utf-8")).digest()
        self._hmac_key = hashlib.sha256(hmac_salt.encode("utf-8")).digest()
        self._aesgcm = AESGCM(self._aes_key)

    def key_fingerprints(self) -> dict[str, str]:
        """Non-secret verifiers bind a database to its encryption and matching keys."""
        return {
            "encryption": hmac.new(self._aes_key, b"ledger-encryption-key-v1", hashlib.sha256).hexdigest(),
            "matching": hmac.new(self._hmac_key, b"ledger-matching-key-v1", hashlib.sha256).hexdigest(),
        }

    def compute_blind_index(self, canonical_phone: Optional[str]) -> Optional[str]:
        """Compute deterministic HMAC-SHA256 hash of a canonical phone number.
        
        Returns None if canonical_phone is None or empty.
        """
        if not canonical_phone:
            return None
        return hmac.new(
            self._hmac_key,
            canonical_phone.strip().encode("utf-8"),
            hashlib.sha256
        ).hexdigest()

    def encrypt_field(self, plaintext: Optional[str]) -> Optional[bytes]:
        """Encrypt plaintext using AES-256-GCM.
        
        Returns raw bytes containing 12-byte nonce followed by ciphertext + auth tag.
        """
        if plaintext is None:
            return None
        data = plaintext.encode("utf-8")
        nonce = os.urandom(12)
        return nonce + self._aesgcm.encrypt(nonce, data, None)

    def decrypt_field(self, encrypted_bytes: Optional[bytes]) -> Optional[str]:
        if encrypted_bytes is None:
            return None
        return self._aesgcm.decrypt(encrypted_bytes[:12], encrypted_bytes[12:], None).decode("utf-8")

    @staticmethod
    def mask_phone(canonical_phone: Optional[str]) -> str:
        """Mask phone number as +91****** followed by last 4 digits.
        
        Example: '+919876543210' -> '+91******3210'
        """
        if not canonical_phone or len(canonical_phone) < 4:
            return ""
        return f"+91******{canonical_phone[-4:]}"


