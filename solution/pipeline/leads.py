"""Lead ingestion pipeline with header mapping, validation, rejection, and deduplication."""
from __future__ import annotations

import csv
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from solution.db import Database
from solution.schemas import (
    SchemaCatalog,
    normalize_header,
    normalize_phone,
    parse_date,
    parse_decimal,
    parse_enum,
)
from solution.security import SecurityService


def compute_file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


class LeadIngestor:
    def __init__(self, db: Database, catalog: SchemaCatalog, security: SecurityService | None = None):
        self.db = db
        self.catalog = catalog
        self.security = security or SecurityService()
        self.db.verify_security(self.security)

    def ingest_lead_file(self, file_path: Path, drop_name: str) -> bool:
        """Ingest a single CSV lead file.
        
        Returns True if ingested, False if skipped (already ingested).
        """
        file_path = Path(file_path).resolve()
        file_hash = compute_file_hash(file_path)
        rel_path = str(file_path)
        
        if self.db.is_file_ingested(rel_path, file_hash):
            return False

        # Extract workflow_id and batch_id from path structure:
        # e.g., .../leads/<workflow_id>/<batch_id>.csv
        wf_id = file_path.parent.name
        batch_id = file_path.stem

        schema = self.catalog.get_schema(wf_id)
        if not schema:
            raise ValueError(f"Unknown workflow schema for workflow_id='{wf_id}' in file {file_path}")

        reference_field_name = self.catalog.get_reference_field(wf_id)

        conn = self.db.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            
            with open(file_path, "r", encoding="utf-8") as fp:
                reader = csv.reader(fp)
                try:
                    raw_headers = next(reader)
                except StopIteration:
                    # Empty file
                    raw_headers = []

                # Build header mapping: col_index -> field_def
                header_map: Dict[int, dict] = {}
                for idx, h in enumerate(raw_headers):
                    nh = normalize_header(h)
                    for field in schema["fields"]:
                        targets = [normalize_header(field["name"])] + [
                            normalize_header(a) for a in field.get("aliases", [])
                        ]
                        if nh in targets:
                            header_map[idx] = field
                            break

                row_number = 0
                lead_rows_in_file = 0
                field_warnings_in_file = 0
                duplicate_rows_in_file = 0

                # Pre-fetch existing leads in this workflow for quick in-memory deduplication & merging
                cur = conn.execute(
                    "SELECT phone_hash, batch_id, external_ref, data_json, created_at FROM leads WHERE workflow_id = ?",
                    (wf_id,)
                )
                existing_leads: Dict[str, dict] = {}
                for row in cur.fetchall():
                    existing_leads[row["phone_hash"]] = {
                        "batch_id": row["batch_id"],
                        "external_ref": row["external_ref"],
                        "data": json.loads(row["data_json"]),
                        "created_at": row["created_at"]
                    }

                rejections_to_insert = []
                leads_to_upsert: Dict[str, dict] = {}

                for row in reader:
                    # Blank rows: every cell empty or whitespace is skipped and not counted
                    if not any(cell.strip() for cell in row):
                        continue

                    row_number += 1
                    lead_rows_in_file += 1

                    # Extract mapped raw values
                    raw_values: Dict[str, Optional[str]] = {}
                    for idx, cell in enumerate(row):
                        if idx in header_map:
                            fdef = header_map[idx]
                            trimmed = cell.strip()
                            raw_values[fdef["name"]] = trimmed if trimmed else None

                    # Walk fields in schema order
                    rejection_reason = None
                    parsed_values: Dict[str, Any] = {}
                    row_warnings = 0

                    for field in schema["fields"]:
                        fname = field["name"]
                        ftype = field["type"]
                        freq = field.get("required", False)
                        raw_val = raw_values.get(fname)

                        if raw_val is None:
                            if freq:
                                rejection_reason = f"missing_required:{fname}"
                                break
                            else:
                                parsed_values[fname] = None
                                continue

                        # Present value validation
                        if ftype == "string":
                            parsed_values[fname] = raw_val
                        elif ftype == "phone":
                            cp = normalize_phone(raw_val)
                            if not cp:
                                rejection_reason = "invalid_phone"
                                break
                            parsed_values[fname] = cp
                        elif ftype == "date":
                            parsed_date = parse_date(raw_val)
                            if parsed_date is None:
                                row_warnings += 1
                                parsed_values[fname] = None
                            else:
                                parsed_values[fname] = parsed_date
                        elif ftype == "decimal":
                            parsed_dec = parse_decimal(raw_val)
                            if parsed_dec is None:
                                row_warnings += 1
                                parsed_values[fname] = None
                            else:
                                parsed_values[fname] = parsed_dec
                        elif ftype == "enum":
                            allowed_values = field.get("values", [])
                            parsed_enum_val = parse_enum(raw_val, allowed_values)
                            if parsed_enum_val is None:
                                row_warnings += 1
                                parsed_values[fname] = None
                            else:
                                parsed_values[fname] = parsed_enum_val

                    if rejection_reason:
                        rejections_to_insert.append((wf_id, batch_id, row_number, rejection_reason, rel_path))
                        continue

                    # Valid row
                    field_warnings_in_file += row_warnings

                    canonical_phone = parsed_values["phone"]
                    phone_hash = self.security.compute_blind_index(canonical_phone)
                    phone_masked = self.security.mask_phone(canonical_phone)
                    now_iso = datetime.now(timezone.utc).isoformat()

                    if phone_hash in existing_leads:
                        duplicate_rows_in_file += 1
                        existing = existing_leads[phone_hash]
                        # Overwrite only fields where new value is non-null (last write wins)
                        for k, v in parsed_values.items():
                            if v is not None:
                                existing["data"][k] = v
                        if parsed_values.get(reference_field_name):
                            existing["external_ref"] = parsed_values[reference_field_name]

                        leads_to_upsert[phone_hash] = {
                            "phone_hash": phone_hash,
                            "phone_masked": phone_masked,
                            "canonical_phone": canonical_phone,
                            "customer_name": existing["data"].get("customer_name"),
                            "external_ref": existing["external_ref"],
                            "batch_id": existing["batch_id"],  # Permanent batch_id preserved!
                            "data": existing["data"],
                            "created_at": existing["created_at"],
                            "updated_at": now_iso
                        }
                    else:
                        ref_val = parsed_values.get(reference_field_name, "")
                        lead_data = {
                            "phone_hash": phone_hash,
                            "phone_masked": phone_masked,
                            "canonical_phone": canonical_phone,
                            "customer_name": parsed_values.get("customer_name"),
                            "external_ref": ref_val,
                            "batch_id": batch_id,  # First valid row sets batch_id permanently
                            "data": parsed_values,
                            "created_at": now_iso,
                            "updated_at": now_iso
                        }
                        existing_leads[phone_hash] = lead_data
                        leads_to_upsert[phone_hash] = lead_data

            # Batch write rejections
            if rejections_to_insert:
                conn.executemany("""
                INSERT OR REPLACE INTO lead_rejections (workflow_id, batch_id, row_number, reason, file_path)
                VALUES (?, ?, ?, ?, ?)
                """, rejections_to_insert)

            # Batch write leads with encrypted PII
            if leads_to_upsert:
                upsert_rows = []
                for linfo in leads_to_upsert.values():
                    # Apply defaults after merging to fields that are still null
                    merged_data = dict(linfo["data"])
                    for field in schema["fields"]:
                        fname = field["name"]
                        if field.get("default") and merged_data.get(fname) is None:
                            merged_data[fname] = field["default"]

                    enc_phone = self.security.encrypt_field(linfo["canonical_phone"])
                    enc_name = self.security.encrypt_field(merged_data.get("customer_name"))

                    # PII protection: don't store plaintext name and phone in data_json
                    safe_data = dict(merged_data)
                    safe_data["customer_name"] = "[PROTECTED_PII]"
                    safe_data["phone"] = linfo["phone_masked"]

                    upsert_rows.append((
                        wf_id,
                        linfo["phone_hash"],
                        linfo["phone_masked"],
                        enc_phone,
                        enc_name,
                        linfo["external_ref"],
                        linfo["batch_id"],
                        json.dumps(safe_data, ensure_ascii=False),
                        linfo["created_at"],
                        linfo["updated_at"]
                    ))

                conn.executemany("""
                INSERT INTO leads (
                    workflow_id, phone_hash, phone_masked, phone_encrypted,
                    customer_name_encrypted, external_ref, batch_id, data_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, phone_hash) DO UPDATE SET
                    external_ref = excluded.external_ref,
                    customer_name_encrypted = excluded.customer_name_encrypted,
                    data_json = excluded.data_json,
                    updated_at = excluded.updated_at;
                """, upsert_rows)

            # Record file ingestion
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute("""
            INSERT OR REPLACE INTO ingested_files (file_path, file_hash, file_type, workflow_id, batch_id, drop_name, ingested_at)
            VALUES (?, ?, 'leads', ?, ?, ?, ?)
            """, (rel_path, file_hash, wf_id, batch_id, drop_name, now_iso))

            # Update counters
            conn.execute("""
            INSERT INTO counters (name, value) VALUES ('lead_rows_total', ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value;
            """, (lead_rows_in_file,))
            conn.execute("""
            INSERT INTO counters (name, value) VALUES ('field_warnings', ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value;
            """, (field_warnings_in_file,))
            conn.execute("""
            INSERT INTO counters (name, value) VALUES ('duplicate_lead_rows_merged', ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value;
            """, (duplicate_rows_in_file,))

            conn.execute("COMMIT;")
            return True
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()
