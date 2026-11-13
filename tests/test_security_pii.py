"""Unit tests for PII protection (encryption at rest and blind indexing)."""
import pytest
from solution.security import SecurityService


def test_encryption_roundtrip():
    sec = SecurityService()
    plaintext = "Rahul Sharma"
    encrypted = sec.encrypt_field(plaintext)
    assert encrypted != plaintext.encode("utf-8")
    assert plaintext.encode("utf-8") not in encrypted

    decrypted = sec.decrypt_field(encrypted)
    assert decrypted == plaintext


def test_blind_index_deterministic():
    sec = SecurityService()
    p1 = "+919876543210"
    p2 = "+919876543210"
    p3 = "+919876543211"

    # Same phone produces exact same hash
    assert sec.compute_blind_index(p1) == sec.compute_blind_index(p2)
    # Different phone produces different hash
    assert sec.compute_blind_index(p1) != sec.compute_blind_index(p3)
    # Phone number string is not present in hash
    assert "9876543210" not in sec.compute_blind_index(p1)
