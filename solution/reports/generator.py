"""High-performance report generator producing all 5 contract files via SQL aggregation."""
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any, Dict, List

from solution.db import Database


class ReportGenerator:
    def __init__(self, db: Database):
        self.db = db

    def _prepare_temp_tables(self, conn):
        """Build indexed temporary tables for blazingly fast aggregations on large scale."""
        conn.executescript("""
        DROP TABLE IF EXISTS temp_call_events;
        CREATE TEMP TABLE temp_call_events AS
        SELECT
            e.event_id,
            e.event_type,
            e.workflow_id,
            e.call_id,
            e.occurred_at,
            e.occurred_date,
            e.disposition_code,
            CASE WHEN e.event_type = 'disposition.override' THEN cpm.phone_hash ELSE e.phone_hash END AS phone_hash
        FROM interaction_ledger e
        LEFT JOIN call_phone_map cpm ON e.workflow_id = cpm.workflow_id AND e.call_id = cpm.call_id
        INNER JOIN leads l ON e.workflow_id = l.workflow_id
            AND CASE WHEN e.event_type = 'disposition.override' THEN cpm.phone_hash ELSE e.phone_hash END = l.phone_hash;

        CREATE INDEX IF NOT EXISTS idx_tce_call ON temp_call_events(call_id);
        CREATE INDEX IF NOT EXISTS idx_tce_lead ON temp_call_events(workflow_id, phone_hash);

        DROP TABLE IF EXISTS temp_calls;
        CREATE TEMP TABLE temp_calls AS
        WITH initiated AS (
            SELECT
                t.workflow_id,
                t.call_id,
                t.phone_hash,
                t.occurred_at AS initiated_at,
                t.occurred_date AS call_date
            FROM temp_call_events t
            INNER JOIN leads l ON t.workflow_id = l.workflow_id AND t.phone_hash = l.phone_hash
            WHERE t.event_type = 'call.initiated'
        ),
        overrides AS (
            SELECT
                t.workflow_id,
                t.call_id,
                t.disposition_code,
                ROW_NUMBER() OVER (PARTITION BY t.workflow_id, t.call_id ORDER BY t.occurred_at DESC) as rn
            FROM temp_call_events t
            INNER JOIN disposition_catalogue dc ON t.workflow_id = dc.workflow_id AND t.disposition_code = dc.code
            WHERE t.event_type = 'disposition.override'
        ),
        sets AS (
            SELECT
                t.workflow_id,
                t.call_id,
                t.disposition_code,
                ROW_NUMBER() OVER (PARTITION BY t.workflow_id, t.call_id ORDER BY t.occurred_at DESC) as rn
            FROM temp_call_events t
            INNER JOIN disposition_catalogue dc ON t.workflow_id = dc.workflow_id AND t.disposition_code = dc.code
            WHERE t.event_type = 'disposition.set'
        )
        SELECT
            i.workflow_id,
            i.call_id,
            i.phone_hash,
            i.initiated_at,
            i.call_date,
            MAX(CASE WHEN t.event_type = 'call.connected' THEN 1 ELSE 0 END) AS is_connected,
            MAX(CASE WHEN t.event_type = 'call.telephony_failed' THEN 1 ELSE 0 END) AS has_telephony_failed,
            COALESCE(o.disposition_code, s.disposition_code) AS effective_disp,
            COALESCE(dc.is_terminal, 0) AS is_terminal,
            COALESCE(dc.is_success, 0) AS is_success
        FROM initiated i
        LEFT JOIN temp_call_events t ON i.workflow_id = t.workflow_id AND i.call_id = t.call_id
        LEFT JOIN overrides o ON i.workflow_id = o.workflow_id AND i.call_id = o.call_id AND o.rn = 1
        LEFT JOIN sets s ON i.workflow_id = s.workflow_id AND i.call_id = s.call_id AND s.rn = 1
        LEFT JOIN disposition_catalogue dc ON i.workflow_id = dc.workflow_id AND COALESCE(o.disposition_code, s.disposition_code) = dc.code
        GROUP BY i.workflow_id, i.call_id, i.phone_hash, i.initiated_at, i.call_date;

        CREATE INDEX IF NOT EXISTS idx_tc_lead ON temp_calls(workflow_id, phone_hash);
        CREATE INDEX IF NOT EXISTS idx_tc_date ON temp_calls(workflow_id, call_date);

        DROP TABLE IF EXISTS temp_lead_summary;
        CREATE TEMP TABLE temp_lead_summary AS
        WITH lead_last_disp AS (
            SELECT
                workflow_id,
                phone_hash,
                effective_disp,
                ROW_NUMBER() OVER (PARTITION BY workflow_id, phone_hash ORDER BY initiated_at DESC) as rn
            FROM temp_calls
            WHERE effective_disp IS NOT NULL
        )
        SELECT
            l.workflow_id,
            l.phone_hash,
            l.external_ref,
            l.batch_id,
            l.phone_masked,
            COUNT(tc.call_id) AS total_calls,
            COALESCE(SUM(tc.is_connected), 0) AS connected_calls,
            COALESCE(MAX(tc.initiated_at), '') AS last_call_at,
            COALESCE(lld.effective_disp, '') AS current_disposition,
            CASE WHEN MAX(tc.is_success) = 1 THEN 'true' ELSE 'false' END AS ever_success,
            CASE
                WHEN COUNT(tc.call_id) = 0 THEN 'yet_to_reach_out'
                WHEN MAX(tc.is_terminal) = 1 THEN 'completed'
                WHEN COUNT(tc.call_id) > 0 AND COUNT(tc.call_id) = SUM(tc.has_telephony_failed) THEN 'in_progress_telephony_failed'
                ELSE 'in_progress_dialed'
            END AS bucket
        FROM leads l
        LEFT JOIN temp_calls tc ON l.workflow_id = tc.workflow_id AND l.phone_hash = tc.phone_hash
        LEFT JOIN lead_last_disp lld ON l.workflow_id = lld.workflow_id AND l.phone_hash = lld.phone_hash AND lld.rn = 1
        GROUP BY l.workflow_id, l.phone_hash, l.external_ref, l.batch_id, l.phone_masked;

        CREATE INDEX IF NOT EXISTS idx_tls_batch ON temp_lead_summary(workflow_id, batch_id);
        CREATE INDEX IF NOT EXISTS idx_tls_ref ON temp_lead_summary(workflow_id, external_ref);
        """)

    def generate_all(self, out_dir: Path):
        """Generate all five report files into out_dir."""
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        conn = self.db.get_connection()
        try:
            self._prepare_temp_tables(conn)
            self._write_batch_summary(conn, out_dir / "batch_summary.csv")
            self._write_daily_overview(conn, out_dir / "daily_overview.csv")
            self._write_lead_report(conn, out_dir / "lead_report.csv")
            self._write_rejections(conn, out_dir / "rejections.csv")
            self._write_data_quality(conn, out_dir / "data_quality.json")
        finally:
            conn.close()

    def _write_batch_summary(self, conn, path: Path):
        query = """
        SELECT
            workflow_id,
            batch_id,
            COUNT(*) AS total_leads,
            SUM(CASE WHEN bucket = 'yet_to_reach_out' THEN 1 ELSE 0 END) AS yet_to_reach_out,
            SUM(CASE WHEN bucket = 'in_progress_telephony_failed' THEN 1 ELSE 0 END) AS in_progress_telephony_failed,
            SUM(CASE WHEN bucket = 'in_progress_dialed' THEN 1 ELSE 0 END) AS in_progress_dialed,
            SUM(CASE WHEN bucket = 'completed' THEN 1 ELSE 0 END) AS completed
        FROM temp_lead_summary
        GROUP BY workflow_id, batch_id
        ORDER BY workflow_id, batch_id;
        """
        cur = conn.execute(query)
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "batch_id", "total_leads",
                "yet_to_reach_out", "in_progress_telephony_failed",
                "in_progress_dialed", "completed"
            ])
            for r in rows:
                writer.writerow([
                    r["workflow_id"], r["batch_id"], r["total_leads"],
                    r["yet_to_reach_out"], r["in_progress_telephony_failed"],
                    r["in_progress_dialed"], r["completed"]
                ])

    def _write_daily_overview(self, conn, path: Path):
        query = """
        SELECT
            workflow_id,
            call_date AS date,
            COUNT(DISTINCT call_id) AS total_calls,
            COUNT(DISTINCT CASE WHEN is_connected = 1 THEN call_id END) AS connected_calls,
            COUNT(DISTINCT CASE WHEN is_success = 1 THEN phone_hash END) AS success_leads
        FROM temp_calls
        GROUP BY workflow_id, call_date
        ORDER BY workflow_id, call_date;
        """
        cur = conn.execute(query)
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "date", "total_calls",
                "connected_calls", "connectivity_rate", "success_leads"
            ])
            for r in rows:
                tot = r["total_calls"]
                conn_calls = r["connected_calls"]
                rate_str = f"{(conn_calls / tot):.4f}" if tot > 0 else "0.0000"
                writer.writerow([
                    r["workflow_id"], r["date"], tot,
                    conn_calls, rate_str, r["success_leads"]
                ])

    def _write_lead_report(self, conn, path: Path):
        query = """
        SELECT
            workflow_id,
            external_ref,
            batch_id,
            phone_masked,
            bucket,
            total_calls,
            connected_calls,
            last_call_at,
            current_disposition,
            ever_success
        FROM temp_lead_summary
        ORDER BY workflow_id, external_ref;
        """
        cur = conn.execute(query)
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                "workflow_id", "external_ref", "batch_id", "phone_masked",
                "bucket", "total_calls", "connected_calls", "last_call_at",
                "current_disposition", "ever_success"
            ])
            for r in rows:
                writer.writerow([
                    r["workflow_id"], r["external_ref"], r["batch_id"],
                    r["phone_masked"], r["bucket"], r["total_calls"],
                    r["connected_calls"], r["last_call_at"],
                    r["current_disposition"], r["ever_success"]
                ])

    def _write_rejections(self, conn, path: Path):
        query = """
        SELECT workflow_id, batch_id, row_number, reason
        FROM lead_rejections
        ORDER BY workflow_id, batch_id, row_number;
        """
        cur = conn.execute(query)
        rows = cur.fetchall()
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["workflow_id", "batch_id", "row_number", "reason"])
            for r in rows:
                writer.writerow([r["workflow_id"], r["batch_id"], r["row_number"], r["reason"]])

    def _write_data_quality(self, conn, path: Path):
        lead_rows_total = self.db.get_counter("lead_rows_total")
        duplicate_lead_rows_merged = self.db.get_counter("duplicate_lead_rows_merged")
        field_warnings = self.db.get_counter("field_warnings")
        events_total = self.db.get_counter("events_total")
        malformed_events = self.db.get_counter("malformed_events")
        duplicate_events_dropped = self.db.get_counter("duplicate_events_dropped")

        cur = conn.execute("SELECT COUNT(*) AS c FROM lead_rejections")
        lead_rows_rejected = cur.fetchone()["c"]

        cur = conn.execute("SELECT reason, COUNT(*) AS cnt FROM lead_rejections GROUP BY reason ORDER BY reason")
        rejections_by_reason = {row["reason"]: row["cnt"] for row in cur.fetchall()}

        cur = conn.execute("""
        SELECT COUNT(*) AS c
        FROM interaction_ledger e
        LEFT JOIN call_phone_map cpm ON e.workflow_id = cpm.workflow_id AND e.call_id = cpm.call_id
        LEFT JOIN leads l ON e.workflow_id = l.workflow_id AND CASE WHEN e.event_type = 'disposition.override' THEN cpm.phone_hash ELSE e.phone_hash END = l.phone_hash
        WHERE l.phone_hash IS NULL;
        """)
        orphan_events = cur.fetchone()["c"]

        cur = conn.execute("""
        SELECT COUNT(*) AS c
        FROM interaction_ledger e
        LEFT JOIN call_phone_map cpm ON e.workflow_id = cpm.workflow_id AND e.call_id = cpm.call_id
        INNER JOIN leads l ON e.workflow_id = l.workflow_id AND CASE WHEN e.event_type = 'disposition.override' THEN cpm.phone_hash ELSE e.phone_hash END = l.phone_hash
        LEFT JOIN disposition_catalogue dc ON e.workflow_id = dc.workflow_id AND e.disposition_code = dc.code
        WHERE e.event_type IN ('disposition.set', 'disposition.override')
          AND dc.code IS NULL;
        """)
        unknown_disposition_events = cur.fetchone()["c"]

        data_quality = {
            "lead_rows_total": lead_rows_total,
            "lead_rows_rejected": lead_rows_rejected,
            "rejections_by_reason": rejections_by_reason,
            "duplicate_lead_rows_merged": duplicate_lead_rows_merged,
            "field_warnings": field_warnings,
            "events_total": events_total,
            "malformed_events": malformed_events,
            "duplicate_events_dropped": duplicate_events_dropped,
            "orphan_events": orphan_events,
            "unknown_disposition_events": unknown_disposition_events
        }

        with open(path, "w", encoding="utf-8") as f:
            json.dump(data_quality, f, indent=2)
