"""Unit tests for effective disposition resolution and lead bucket state machine."""
import pytest
from datetime import datetime, timezone, timedelta
from solution.db import Database
from solution.reports.generator import ReportGenerator
from solution.security import SecurityService

IST = timezone(timedelta(hours=5, minutes=30))


def test_effective_disposition_precedence(tmp_path):
    db_file = tmp_path / "test_disp.db"
    db = Database(db_file)
    sec = SecurityService()

    # Setup catalogue
    disps = {
        ("motor_renewal", "renewed"): {"label": "Renewed", "is_terminal": True, "is_success": True},
        ("motor_renewal", "callback_requested"): {"label": "Callback", "is_terminal": False, "is_success": False},
        ("motor_renewal", "not_interested"): {"label": "Not Interested", "is_terminal": True, "is_success": False},
    }
    db.sync_disposition_catalogue(disps)

    conn = db.get_connection()
    # Insert a lead
    phone = "+919876543210"
    phash = sec.compute_blind_index(phone)
    pmask = sec.mask_phone(phone)
    penc = sec.encrypt_field(phone)
    nenc = sec.encrypt_field("Test User")

    conn.execute("""
    INSERT INTO leads VALUES ('motor_renewal', ?, ?, ?, ?, 'POL123', 'batch_001', '{}', '2026-10-01', '2026-10-01')
    """, (phash, pmask, penc, nenc))

    # Scenario 1: call.initiated + disposition.set ("callback_requested")
    # Followed by later disposition.override ("not_interested")
    # Effective disposition must be "not_interested"
    cid = "call_001"
    conn.execute("INSERT INTO call_phone_map VALUES ('motor_renewal', ?, ?)", (cid, phash))

    conn.execute("""
    INSERT INTO interaction_ledger (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, disposition_code, payload_json, drop_name, ingested_at)
    VALUES
      ('e1', 'call.initiated', 'motor_renewal', 'call_001', '2026-10-01T10:00:00+05:30', '2026-10-01', ?, NULL, '{}', 'drop_1', '2026-10-01'),
      ('e2', 'call.connected', 'motor_renewal', 'call_001', '2026-10-01T10:01:00+05:30', '2026-10-01', ?, NULL, '{}', 'drop_1', '2026-10-01'),
      ('e3', 'disposition.set', 'motor_renewal', 'call_001', '2026-10-01T10:05:00+05:30', '2026-10-01', ?, 'callback_requested', '{}', 'drop_1', '2026-10-01'),
      ('e4', 'disposition.override', 'motor_renewal', 'call_001', '2026-10-01T11:00:00+05:30', '2026-10-01', NULL, 'not_interested', '{}', 'drop_1', '2026-10-01')
    """, (phash, phash, phash))

    # Run report generator temp table preparation
    gen = ReportGenerator(db)
    gen._prepare_temp_tables(conn)

    cur = conn.execute("SELECT effective_disp, is_terminal, is_success FROM temp_calls WHERE call_id = 'call_001'")
    row = cur.fetchone()
    assert row["effective_disp"] == "not_interested"
    assert row["is_terminal"] == 1
    assert row["is_success"] == 0

    # Bucket should be completed because not_interested is terminal
    cur = conn.execute("SELECT bucket FROM temp_lead_summary WHERE phone_hash = ?", (phash,))
    assert cur.fetchone()["bucket"] == "completed"
    conn.close()


def test_bucket_telephony_failed(tmp_path):
    db_file = tmp_path / "test_failed.db"
    db = Database(db_file)
    sec = SecurityService()

    conn = db.get_connection()
    phone = "+919876543211"
    phash = sec.compute_blind_index(phone)
    conn.execute("""
    INSERT INTO leads VALUES ('motor_renewal', ?, '+91******3211', X'', X'', 'POL124', 'batch_001', '{}', '2026-10-01', '2026-10-01')
    """, (phash,))

    # Lead has 2 calls, both had call.telephony_failed
    conn.execute("INSERT INTO call_phone_map VALUES ('motor_renewal', 'c1', ?), ('motor_renewal', 'c2', ?)", (phash, phash))
    conn.execute("""
    INSERT INTO interaction_ledger (event_id, event_type, workflow_id, call_id, occurred_at, occurred_date, phone_hash, disposition_code, payload_json, drop_name, ingested_at)
    VALUES
      ('e1', 'call.initiated', 'motor_renewal', 'c1', '2026-10-01T10:00:00+05:30', '2026-10-01', ?, NULL, '{}', 'drop_1', '2026-10-01'),
      ('e2', 'call.telephony_failed', 'motor_renewal', 'c1', '2026-10-01T10:00:05+05:30', '2026-10-01', ?, NULL, '{}', 'drop_1', '2026-10-01'),
      ('e3', 'call.initiated', 'motor_renewal', 'c2', '2026-10-02T10:00:00+05:30', '2026-10-02', ?, NULL, '{}', 'drop_1', '2026-10-01'),
      ('e4', 'call.telephony_failed', 'motor_renewal', 'c2', '2026-10-02T10:00:05+05:30', '2026-10-02', ?, NULL, '{}', 'drop_1', '2026-10-01')
    """, (phash, phash, phash, phash))

    gen = ReportGenerator(db)
    gen._prepare_temp_tables(conn)

    cur = conn.execute("SELECT bucket FROM temp_lead_summary WHERE phone_hash = ?", (phash,))
    assert cur.fetchone()["bucket"] == "in_progress_telephony_failed"
    conn.close()


def test_bucket_yet_to_reach_out(tmp_path):
    db_file = tmp_path / "test_yet.db"
    db = Database(db_file)
    sec = SecurityService()

    conn = db.get_connection()
    phone = "+919876543212"
    phash = sec.compute_blind_index(phone)
    conn.execute("""
    INSERT INTO leads VALUES ('motor_renewal', ?, '+91******3212', X'', X'', 'POL125', 'batch_001', '{}', '2026-10-01', '2026-10-01')
    """, (phash,))

    gen = ReportGenerator(db)
    gen._prepare_temp_tables(conn)

    cur = conn.execute("SELECT bucket, total_calls FROM temp_lead_summary WHERE phone_hash = ?", (phash,))
    row = cur.fetchone()
    assert row["bucket"] == "yet_to_reach_out"
    assert row["total_calls"] == 0
    conn.close()
