"""Database connection, schema setup, and transactions for the solution pipeline."""
from __future__ import annotations

import sqlite3
import hmac
from pathlib import Path
from typing import Generator, Optional


class Database:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.init_schema()

    def get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path),
            timeout=60.0,
            isolation_level=None  # Enable autocommit / explicit transaction control
        )
        conn.row_factory = sqlite3.Row
        # Performance optimizations for SQLite
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.execute("PRAGMA cache_size = -64000;")  # 64MB cache
        conn.execute("PRAGMA temp_store = MEMORY;")
        conn.execute("PRAGMA foreign_keys = ON;")
        return conn

    def init_schema(self):
        """Create tables and indexes if they don't exist."""
        conn = self.get_connection()
        try:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS security_metadata (
                name TEXT PRIMARY KEY,
                fingerprint TEXT NOT NULL
            );

            -- Tracking ingested files for complete idempotency
            CREATE TABLE IF NOT EXISTS ingested_files (
                file_path TEXT PRIMARY KEY,
                file_hash TEXT NOT NULL,
                file_type TEXT NOT NULL,
                workflow_id TEXT,
                batch_id TEXT,
                drop_name TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            -- Disposition catalogue
            CREATE TABLE IF NOT EXISTS disposition_catalogue (
                workflow_id TEXT NOT NULL,
                code TEXT NOT NULL,
                label TEXT NOT NULL,
                is_terminal INTEGER NOT NULL,
                is_success INTEGER NOT NULL,
                PRIMARY KEY (workflow_id, code)
            );

            -- Deduplicated, validated leads table with protected PII
            CREATE TABLE IF NOT EXISTS leads (
                workflow_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                phone_masked TEXT NOT NULL,
                phone_encrypted BLOB NOT NULL,
                customer_name_encrypted BLOB NOT NULL,
                external_ref TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                data_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (workflow_id, phone_hash)
            );

            -- Rejected rows (no PII)
            CREATE TABLE IF NOT EXISTS lead_rejections (
                workflow_id TEXT NOT NULL,
                batch_id TEXT NOT NULL,
                row_number INTEGER NOT NULL,
                reason TEXT NOT NULL,
                file_path TEXT NOT NULL,
                PRIMARY KEY (workflow_id, batch_id, row_number)
            );

            -- Append-only interaction ledger
            CREATE TABLE IF NOT EXISTS interaction_ledger (
                event_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                occurred_date TEXT NOT NULL,
                phone_hash TEXT,
                phone_masked TEXT,
                phone_encrypted BLOB,
                disposition_code TEXT,
                payload_json TEXT NOT NULL,
                drop_name TEXT NOT NULL,
                ingested_at TEXT NOT NULL
            );

            -- Call to phone mapping table for linking overrides
            CREATE TABLE IF NOT EXISTS call_phone_map (
                workflow_id TEXT NOT NULL,
                call_id TEXT NOT NULL,
                phone_hash TEXT NOT NULL,
                PRIMARY KEY (workflow_id, call_id)
            );

            -- Counters for metrics
            CREATE TABLE IF NOT EXISTS counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0
            );

            -- Indexes for fast joining and reporting
            CREATE INDEX IF NOT EXISTS idx_ledger_call ON interaction_ledger(call_id);
            CREATE INDEX IF NOT EXISTS idx_ledger_wf_phone ON interaction_ledger(workflow_id, phone_hash);
            CREATE INDEX IF NOT EXISTS idx_ledger_wf_date ON interaction_ledger(workflow_id, occurred_date);
            CREATE INDEX IF NOT EXISTS idx_ledger_type ON interaction_ledger(event_type);
            CREATE INDEX IF NOT EXISTS idx_leads_wf_ref ON leads(workflow_id, external_ref);
            CREATE INDEX IF NOT EXISTS idx_leads_wf_batch ON leads(workflow_id, batch_id);
            """)
        finally:
            conn.close()

    def verify_security(self, security):
        """Fail before ingestion if either key differs, including for skipped files.

        Older populated databases are adopted only after checking an encrypted phone
        and its matching hash. No stored PII or keys are printed in error messages.
        """
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            expected = security.key_fingerprints()
            stored = dict(conn.execute("SELECT name, fingerprint FROM security_metadata"))
            if stored:
                if set(stored) != set(expected) or any(
                    not hmac.compare_digest(stored[name], value)
                    for name, value in expected.items()
                ):
                    raise ValueError("Database keys do not match. Restore the original PII_SECRET_KEY and HMAC_SALT or use a new database.")
            else:
                # Compatibility with databases created before key metadata existed.
                sample = conn.execute("""
                    SELECT phone_encrypted, phone_hash FROM leads
                    UNION ALL
                    SELECT phone_encrypted, phone_hash FROM interaction_ledger
                    WHERE phone_encrypted IS NOT NULL
                    LIMIT 1
                """).fetchone()
                if sample is not None:
                    try:
                        phone = security.decrypt_field(sample["phone_encrypted"])
                        if not phone or security.compute_blind_index(phone) != sample["phone_hash"]:
                            raise ValueError("Invalid matching key")
                    except Exception as exc:
                        raise ValueError("Cannot verify legacy database keys. Restore the original keys or use a new database.") from exc
                elif conn.execute("SELECT 1 FROM interaction_ledger LIMIT 1").fetchone():
                    raise ValueError("Cannot verify keys for a legacy ledger without encrypted phones; use a new database.")
                conn.executemany(
                    "INSERT INTO security_metadata (name, fingerprint) VALUES (?, ?)",
                    expected.items(),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def sync_disposition_catalogue(self, dispositions: dict):
        """Populate or update disposition catalogue from schema."""
        conn = self.get_connection()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            for (wf, code), info in dispositions.items():
                conn.execute("""
                INSERT INTO disposition_catalogue (workflow_id, code, label, is_terminal, is_success)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, code) DO UPDATE SET
                    label = excluded.label,
                    is_terminal = excluded.is_terminal,
                    is_success = excluded.is_success;
                """, (wf, code, info["label"], 1 if info["is_terminal"] else 0, 1 if info["is_success"] else 0))
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def is_file_ingested(self, file_path: str, file_hash: str) -> bool:
        """Check if file has already been ingested with the same hash."""
        conn = self.get_connection()
        try:
            cur = conn.execute(
                "SELECT 1 FROM ingested_files WHERE file_path = ? AND file_hash = ?",
                (file_path, file_hash)
            )
            return cur.fetchone() is not None
        finally:
            conn.close()

    def record_ingested_file(self, file_path: str, file_hash: str, file_type: str,
                             workflow_id: Optional[str], batch_id: Optional[str],
                             drop_name: str, ingested_at: str):
        conn = self.get_connection()
        try:
            conn.execute("""
            INSERT OR REPLACE INTO ingested_files (file_path, file_hash, file_type, workflow_id, batch_id, drop_name, ingested_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (file_path, file_hash, file_type, workflow_id, batch_id, drop_name, ingested_at))
        finally:
            conn.close()

    def increment_counter(self, name: str, amount: int = 1):
        if amount <= 0:
            return
        conn = self.get_connection()
        try:
            conn.execute("""
            INSERT INTO counters (name, value) VALUES (?, ?)
            ON CONFLICT(name) DO UPDATE SET value = value + excluded.value
            """, (name, amount))
        finally:
            conn.close()

    def get_counter(self, name: str) -> int:
        conn = self.get_connection()
        try:
            cur = conn.execute("SELECT value FROM counters WHERE name = ?", (name,))
            row = cur.fetchone()
            return row["value"] if row else 0
        finally:
            conn.close()
