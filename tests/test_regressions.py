"""Regression coverage for isolation, delayed linking, and supplied keys."""
import csv
import json

import pytest
from cryptography.exceptions import InvalidTag

from solution.config import DEFAULT_SCHEMAS_DIR
from solution.db import Database
from solution.pipeline.events import EventIngestor, parse_event_timestamp
from solution.pipeline.leads import LeadIngestor
from solution.reports.generator import ReportGenerator
from solution.schemas import SchemaCatalog
from solution.security import SecurityService


def test_keys_required_and_ciphertext_authenticated(monkeypatch):
    monkeypatch.delenv("PII_SECRET_KEY")
    monkeypatch.delenv("HMAC_SALT")
    with pytest.raises(ValueError, match="Set PII_SECRET_KEY"):
        SecurityService()
    sec = SecurityService("encryption", "matching")
    encrypted = sec.encrypt_field("Alice")
    assert encrypted != sec.encrypt_field("Alice")
    with pytest.raises(InvalidTag):
        sec.decrypt_field(encrypted[:-1] + bytes([encrypted[-1] ^ 1]))


def test_naive_iso_timestamp_is_ist():
    assert parse_event_timestamp("2026-10-03T10:01:00") == (
        "2026-10-03T10:01:00+05:30", "2026-10-03")
    assert parse_event_timestamp("2026-10-03T20:00:00Z")[1] == "2026-10-04"


def test_late_lead_and_shared_call_ids(tmp_path):
    db = Database(tmp_path / "ledger.db")
    catalog = SchemaCatalog(DEFAULT_SCHEMAS_DIR)
    db.sync_disposition_catalogue(catalog.dispositions)
    events_path = tmp_path / "events.jsonl"
    events = [
        {"event_id": "start-m", "workflow_id": "motor_renewal", "call_id": "shared",
         "event_type": "call.initiated", "phone": "9876543210", "payload": {}},
        {"event_id": "override", "workflow_id": "motor_renewal", "call_id": "shared",
         "event_type": "disposition.override", "payload": {"disposition_code": "renewed"}},
        {"event_id": "start-f", "workflow_id": "policy_feedback", "call_id": "shared",
         "event_type": "call.initiated", "phone": "9876543211", "payload": {}},
        {"event_id": "connect-f", "workflow_id": "policy_feedback", "call_id": "shared",
         "event_type": "call.connected", "phone": "9876543211", "payload": {}},
    ]
    for event in events:
        event["occurred_at"] = "2026-10-03T10:01:00Z"
    events_path.write_text("\n".join(json.dumps(e) for e in events), encoding="utf-8")
    EventIngestor(db, catalog).ingest_event_file(events_path, "drop_1")
    reports = tmp_path / "reports"
    ReportGenerator(db).generate_all(reports)
    assert json.loads((reports / "data_quality.json").read_text())["orphan_events"] == 4

    for workflow, reference, phone in [("motor_renewal", "policy_number", "9876543210"),
                                        ("policy_feedback", "order_id", "9876543211")]:
        lead_path = tmp_path / workflow / "batch_001.csv"
        lead_path.parent.mkdir()
        with lead_path.open("w", newline="", encoding="utf-8") as fp:
            writer = csv.writer(fp)
            writer.writerow(["customer_name", "phone", reference])
            writer.writerow(["Alice", phone, "REF"])
        LeadIngestor(db, catalog).ingest_lead_file(lead_path, "drop_2")
    ReportGenerator(db).generate_all(reports)
    assert json.loads((reports / "data_quality.json").read_text())["orphan_events"] == 0
    with (reports / "lead_report.csv").open() as fp:
        rows = {r["workflow_id"]: r for r in csv.DictReader(fp)}
    assert rows["motor_renewal"]["connected_calls"] == "0"
    assert rows["motor_renewal"]["current_disposition"] == "renewed"
    assert rows["policy_feedback"]["connected_calls"] == "1"
    assert rows["policy_feedback"]["current_disposition"] == ""
