"""Isolated test credentials; never used by the application as defaults."""
import pytest


@pytest.fixture(autouse=True)
def test_keys(monkeypatch):
    monkeypatch.setenv("PII_SECRET_KEY", "test-only-encryption-key")
    monkeypatch.setenv("HMAC_SALT", "test-only-matching-key")
