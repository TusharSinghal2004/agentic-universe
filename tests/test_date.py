"""Unit tests for date parsing (day-first, multi-format, case-insensitive month)."""
import pytest
from solution.schemas import parse_date


def test_iso_date():
    assert parse_date("2026-10-31") == "2026-10-31"


def test_day_first_slash():
    # 05/11/2026 must be 5th November, NOT 11th May
    assert parse_date("05/11/2026") == "2026-11-05"
    assert parse_date("27/10/2026") == "2026-10-27"


def test_day_first_hyphen():
    assert parse_date("14-10-2026") == "2026-10-14"
    assert parse_date("21-11-2026") == "2026-11-21"


def test_dd_mon_yyyy_case_insensitive():
    assert parse_date("01-Dec-2026") == "2026-12-01"
    assert parse_date("01-dec-2026") == "2026-12-01"
    assert parse_date("01-DEC-2026") == "2026-12-01"
    assert parse_date("15-oct-2026") == "2026-10-15"


def test_impossible_dates_and_garbage():
    # 31st February is impossible
    assert parse_date("31/02/2026") is None
    # 13th month is impossible
    assert parse_date("2026-13-01") is None
    # Text strings
    assert parse_date("soon") is None
    assert parse_date("TBD") is None
    assert parse_date("") is None
    assert parse_date(None) is None
