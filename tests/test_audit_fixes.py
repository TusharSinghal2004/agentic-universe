"""Regressions reproduced through actual drop ingestion and reports."""
import csv
import json
import os
import sqlite3
import subprocess
import sys

import pytest

from solution.config import DEFAULT_SCHEMAS_DIR
from solution.db import Database
from solution.pipeline.events import EventIngestor, parse_event_timestamp
from solution.pipeline.leads import LeadIngestor
from solution.reports.generator import ReportGenerator
from solution.schemas import SchemaCatalog
from solution.security import SecurityService


def make_drop(tmp_path, events):
    drop = tmp_path / "drop_1"
    lead = drop / "leads/motor_renewal/batch_001.csv"
    lead.parent.mkdir(parents=True)
    lead.write_text("customer_name,phone,policy_number\nAlice,9876543210,POL1\n")
    event_file = drop / "events/events.jsonl"
    event_file.parent.mkdir()
    event_file.write_text("\n".join(json.dumps(e) for e in events))
    return drop, lead, event_file


def event(event_id, event_type="call.initiated", **kwargs):
    return dict(event_id=event_id, event_type=event_type, workflow_id="motor_renewal",
                call_id="call1", occurred_at="2026-10-03T10:00:00Z",
                phone="9876543210", payload={}, **kwargs)


def test_invalid_phone_is_orphan_but_override_links(tmp_path):
    events = [event("start"), event("invalid", "call.connected"),
              event("override", "disposition.override")]
    events[1]["phone"] = "invalid"
    events[2].pop("phone")
    events[2]["payload"] = {"disposition_code": "renewed"}
    _, lead, ef = make_drop(tmp_path, events)
    db = Database(tmp_path / "ledger.db")
    catalog = SchemaCatalog(DEFAULT_SCHEMAS_DIR)
    db.sync_disposition_catalogue(catalog.dispositions)
    LeadIngestor(db, catalog).ingest_lead_file(lead, "drop_1")
    EventIngestor(db, catalog).ingest_event_file(ef, "drop_1")
    reports = tmp_path / "reports"
    ReportGenerator(db).generate_all(reports)
    with (reports / "lead_report.csv").open() as fp:
        row = next(csv.DictReader(fp))
    assert row["connected_calls"] == "0"
    assert row["current_disposition"] == "renewed"
    assert json.loads((reports / "data_quality.json").read_text())["orphan_events"] == 1


def test_fractional_utc_timestamp():
    assert parse_event_timestamp("2026-10-03T20:00:00.123Z") == (
        "2026-10-04T01:30:00.123000+05:30", "2026-10-04")


def test_bad_records_do_not_abort_file_and_replay_is_stable(tmp_path):
    good = event("good")
    good["occurred_at"] = "2026-10-03T10:00:00.123Z"
    bad_time = event("bad-time")
    bad_time["occurred_at"] = "bad-date"
    null_payload = event("bad-payload", "disposition.set")
    null_payload["payload"] = None
    bad_code = event("bad-code", "disposition.set")
    bad_code["payload"] = {"disposition_code": ["renewed"]}
    _, _, ef = make_drop(tmp_path, [good, bad_time, null_payload, bad_code, event("after", "call.connected")])
    db = Database(tmp_path / "ledger.db")
    ingestor = EventIngestor(db, SchemaCatalog(DEFAULT_SCHEMAS_DIR))
    assert ingestor.ingest_event_file(ef, "drop_1")
    assert not ingestor.ingest_event_file(ef, "drop_1")
    assert db.get_counter("events_total") == 5
    assert db.get_counter("malformed_events") == 3
    with db.get_connection() as conn:
        assert conn.execute("SELECT COUNT(*) FROM interaction_ledger").fetchone()[0] == 2


@pytest.mark.parametrize("kind", ["empty", "file", "missing", "empty-child"])
def test_invalid_input_cli_does_not_create_database(tmp_path, kind):
    source = tmp_path / "input"
    if kind == "file":
        source.write_text("input")
    elif kind != "missing":
        source.mkdir()
        if kind == "empty-child":
            (source / "drop_1").mkdir()
    db = tmp_path / "ledger.db"
    result = subprocess.run([sys.executable, "-m", "solution", "--db", str(db),
                             "ingest", str(source)], text=True, capture_output=True)
    assert result.returncode == 1
    assert "Error:" in result.stderr
    assert not db.exists()


@pytest.mark.parametrize("key", ["PII_SECRET_KEY", "HMAC_SALT"])
def test_changed_key_rejected_even_for_skipped_file(tmp_path, monkeypatch, key):
    drop, lead, _ = make_drop(tmp_path, [event("start")])
    db = Database(tmp_path / "ledger.db")
    catalog = SchemaCatalog(DEFAULT_SCHEMAS_DIR)
    LeadIngestor(db, catalog).ingest_lead_file(lead, "drop_1")
    with db.get_connection() as conn:
        before = list(conn.iterdump())
    monkeypatch.setenv(key, "wrong-key")
    result = subprocess.run([sys.executable, "-m", "solution", "--db", str(db.db_path),
                             "ingest", str(drop)], capture_output=True, text=True)
    assert result.returncode == 1
    assert "Database keys do not match" in result.stderr
    assert "Traceback" not in result.stderr
    with db.get_connection() as conn:
        assert before == list(conn.iterdump())


@pytest.mark.parametrize("correct_keys", [True, False])
def test_legacy_database_keys_verified_before_adoption(tmp_path, correct_keys):
    _, lead, _ = make_drop(tmp_path, [])
    db = Database(tmp_path / "ledger.db")
    catalog = SchemaCatalog(DEFAULT_SCHEMAS_DIR)
    LeadIngestor(db, catalog).ingest_lead_file(lead, "drop_1")
    with db.get_connection() as conn:
        conn.execute("DELETE FROM security_metadata")
    if correct_keys:
        LeadIngestor(db, catalog)
        with db.get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM security_metadata").fetchone()[0] == 2
    else:
        with pytest.raises(ValueError, match="Cannot verify legacy"):
            LeadIngestor(db, catalog, SecurityService("wrong", "wrong"))
        with db.get_connection() as conn:
            assert conn.execute("SELECT COUNT(*) FROM security_metadata").fetchone()[0] == 0
