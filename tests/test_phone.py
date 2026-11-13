"""Unit tests for phone validation, normalization, and masking."""
import pytest
from solution.schemas import normalize_phone
from solution.security import SecurityService


def test_valid_indian_phones():
    assert normalize_phone("9876543210") == "+919876543210"
    assert normalize_phone("+91 98765 43210") == "+919876543210"
    assert normalize_phone("09876543210") == "+919876543210"
    assert normalize_phone("91-98765-43210") == "+919876543210"
    assert normalize_phone("(+91) 9876543210") == "+919876543210"
    assert normalize_phone("+919876543210") == "+919876543210"


def test_valid_phone_starting_digits():
    for digit in ("6", "7", "8", "9"):
        p = f"{digit}123456789"
        assert normalize_phone(p) == f"+91{p}"


def test_invalid_phones():
    # Bad prefixes (starting with 0-5)
    assert normalize_phone("5123456789") is None
    assert normalize_phone("1123456789") is None
    # 9 digits
    assert normalize_phone("987654321") is None
    # Landline
    assert normalize_phone("022-2847-1928") is None
    # International / US
    assert normalize_phone("+1 415 555 0134") is None
    # Non-digit text
    assert normalize_phone("N/A") is None
    assert normalize_phone("") is None
    assert normalize_phone(None) is None


def test_phone_masking():
    sec = SecurityService()
    assert sec.mask_phone("+919876543210") == "+91******3210"
    assert sec.mask_phone("+917718750036") == "+91******0036"
    assert sec.mask_phone("") == ""
    assert sec.mask_phone(None) == ""
