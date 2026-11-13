"""Event ingestion pipeline into an append-only interaction ledger."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from solution.db import Database
from solution.schemas import SchemaCatalog, normalize_phone
from solution.security import SecurityService

IST = timezone(timedelta(hours=5, minutes=30))


def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def parse_event_timestamp(ts_str: str) -> Tuple[str, str]:
    """Parse event timestamp into (ist_iso_datetime, ist_date_str).
    
    Timestamps come in three forms:
    1. UTC: '...Z'
    2. Offset: '...+05:30'
    3. Naive: '2026-10-03 10:01:00' (Naive means IST)
    """
    if not isinstance(ts_str, str) or not ts_str.strip():
        raise ValueError("Event timestamp must be a non-empty ISO-8601 string")
    s = ts_str.strip()
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    ist_dt = dt.astimezone(IST)
    return ist_dt.isoformat(), ist_dt.date().isoformat()


class EventIngestor:
    def __init__(self, db: Database, catalog: SchemaCatalog, security: SecurityService | None = None):
        self.db = db
        self.catalog = catalog
        self.security = security or SecurityService()
        self.db.verify_security(self.security)

    def ingest_event_file(self, file_path: Path, drop_name: str) -> bool:
        """Ingest a single JSONL event file.
        
        Returns True if ingested, False if skipped (already ingested).
        """
        file_path = Path(file_path).resolve()
        file_hash = compute_file_hash(file_path)
        rel_path = str(file_path)

        if self.db.is_file_ingested(rel_path, file_hash):
            return False

        conn = self.db.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")

            # Pre-fetch seen event IDs to detect duplicates across drops
            cur = conn.execute("SELECT event_id FROM interaction_ledger")
            seen_event_ids: Set[str] = {row["event_id"] for row in cur.fetchall()}

            events_total = 0
            malformed_events = 0
            duplicate_events_dropped = 0

            ledger_rows = []
            call_phone_maps = []

            now_iso = datetime.now(timezone.utc).isoformat()

            with open(file_path, "r", encoding="utf-8") as fp:
                for line in fp:
                    line = line.strip()
                    if not line:
                        continue
                    events_total += 1

                    # 1. Malformed check
                    try:
                        ev = json.loads(line)
                        if not isinstance(ev, dict):
                            malformed_events += 1
                            continue
                        event_id = ev.get("event_id")
                        if not event_id or not isinstance(event_id, str) or not event_id.strip():
                            malformed_events += 1
                            continue
                    except Exception:
                        malformed_events += 1
                        continue

                    # 2. Duplicate check
                    if event_id in seen_event_ids:
                        duplicate_events_dropped += 1
                        continue

                    # Extract fields
                    event_type = ev.get("event_type", "")
                    workflow_id = ev.get("workflow_id", "")
                    call_id = ev.get("call_id", "")
                    occurred_at_raw = ev.get("occurred_at", "")
                    phone_raw = ev.get("phone")
                    payload = ev.get("payload", {})

                    # Additional structural validation is a documented extension of
                    # the malformed-record rule. One bad record never aborts a file.
                    if (any(not isinstance(v, str) or not v.strip()
                            for v in (event_type, workflow_id, call_id))
                            or not isinstance(payload, dict)):
                        malformed_events += 1
                        continue
                    if event_type.startswith("disposition.") and (
                        payload.get("disposition_code") is not None
                        and not isinstance(payload["disposition_code"], str)
                    ):
                        malformed_events += 1
                        continue
                    try:
                        occurred_at_ist, occurred_date_ist = parse_event_timestamp(occurred_at_raw)
                    except (ValueError, TypeError, OverflowError):
                        malformed_events += 1
                        continue

                    seen_event_ids.add(event_id)

                    # Phone handling & PII protection
                    # Only overrides may link without a valid phone, via call_id.
                    canonical_phone = (normalize_phone(phone_raw) if isinstance(phone_raw, str) else None)
                    if event_type == "disposition.override":
                        canonical_phone = None
                    phone_hash = self.security.compute_blind_index(canonical_phone) if canonical_phone else None
                    phone_masked = self.security.mask_phone(canonical_phone) if canonical_phone else None
                    phone_enc = self.security.encrypt_field(canonical_phone) if canonical_phone else None

                    # If this event has a valid call_id, workflow_id, and phone_hash, map it
                    if call_id and workflow_id and phone_hash:
                        call_phone_maps.append((workflow_id, call_id, phone_hash))

                    # Extract disposition code if disposition event
                    disposition_code = None
                    if event_type in ("disposition.set", "disposition.override"):
                        disposition_code = payload.get("disposition_code")

                    safe_payload = dict(payload) if isinstance(payload, dict) else {}

                    ledger_rows.append((
                        event_id,
                        event_type,
                        workflow_id,
                        call_id,
                        occurred_at_ist,
                        occurred_date_ist,
                        phone_hash,
                        phone_masked,
                        phone_enc,
                        disposition_code,
                        json.dumps(safe_payload, ensure_ascii=False),
                        drop_name,
                        now_iso
                    ))

            # Batch insert call phone mappings
            if call_phone_maps:
                conn.executemany("""
                INSERT OR IGNORE INTO call_phone_map (workflow_id, call_id, phone_hash)
                VALUES (?, ?, ?)
                """, call_phone_maps)

            # Batch insert events into ledger
            if ledger_rows:
                conn.executemany("""
                INSERT INTO interaction_ledger (
                    event_id, event_type, workflow_id, call_id, occurred_at,
                    occurred_date, phone_hash, phone_masked, phone_encrypted,
                    disposition_code, payload_json, drop_name, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, ledger_rows)

            # Record file ingestion
            conn.execute("""
            INSERT OR REPLACE INTO ingested_files (file_path, file_hash, file_type, workflow_id, batch_id, drop_name, ingested_at)
            VALUES (?, ?, 'events', NULL, NULL, ?, ?)
            """, (rel_path, file_hash, drop_name, now_iso))

            # Update metrics counters
            conn.execute("""
            INSERT INTO counters (name, value) VALUES ('events_total', ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value;
            """, (events_total,))
            conn.execute("""
            INSERT INTO counters (name, value) VALUES ('malformed_events', ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value;
            """, (malformed_events,))
            conn.execute("""
            INSERT INTO counters (name, value) VALUES ('duplicate_events_dropped', ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value;
            """, (duplicate_events_dropped,))

            conn.execute("COMMIT;")
            return True
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()
