"""Unit tests for decimal and enum parsing."""
import pytest
from solution.schemas import parse_decimal, parse_enum


def test_decimal_parsing_with_currency_symbols():
    assert parse_decimal("₹27,554") == 27554.0
    assert parse_decimal("Rs. 24787") == 24787.0
    assert parse_decimal("INR 18678.3") == 18678.3
    assert parse_decimal("Rs 100") == 100.0
    assert parse_decimal("rs.50.556") == 50.56
    assert parse_decimal("4737.14") == 4737.14
    assert parse_decimal("6850") == 6850.0


def test_invalid_decimals():
    # Negative numbers invalid
    assert parse_decimal("-10") is None
    # Text / N/A
    assert parse_decimal("twelve thousand") is None
    assert parse_decimal("N/A") is None
    assert parse_decimal("-") is None
    assert parse_decimal("") is None
    assert parse_decimal(None) is None


def test_enum_case_insensitive():
    allowed = ["en", "hi", "ta", "kn", "te", "mr"]
    assert parse_enum("EN", allowed) == "en"
    assert parse_enum("  Hi  ", allowed) == "hi"
    assert parse_enum("ta", allowed) == "ta"
    # Invalid values
    assert parse_enum("Hindi", allowed) is None
    assert parse_enum("english", allowed) is None
    assert parse_enum("fr", allowed) is None
