"""Integration tests for end-to-end ingestion, idempotency, and reporting."""
import csv
import json
import subprocess
import sys
from pathlib import Path
import pytest


def test_end_to_end_pipeline(tmp_path):
    db_path = tmp_path / "test_pipeline.db"
    reports_dir = tmp_path / "reports"

    data_dir = tmp_path / "data"
    subprocess.run([sys.executable, "assignment-a-data/tools/generate_data.py",
                    "--seed", "42", "--scale", "small", "--out", str(data_dir)], check=True)

    # 1. Ingest drop_1
    cmd1 = [sys.executable, "-m", "solution", f"--db={db_path}", "ingest", str(data_dir / "drop_1")]
    res1 = subprocess.run(cmd1, capture_output=True, text=True)
    assert res1.returncode == 0, res1.stderr

    # 2. Re-ingest drop_1 (must be completely idempotent and skip all files)
    res1_re = subprocess.run(cmd1, capture_output=True, text=True)
    assert res1_re.returncode == 0
    assert "skipped: 4" in res1_re.stdout
    assert "skipped: 1" in res1_re.stdout

    # 3. Ingest drop_2
    cmd2 = [sys.executable, "-m", "solution", f"--db={db_path}", "ingest", str(data_dir / "drop_2")]
    res2 = subprocess.run(cmd2, capture_output=True, text=True)
    assert res2.returncode == 0, res2.stderr

    # 4. Generate reports
    cmd_rep = [sys.executable, "-m", "solution", f"--db={db_path}", "report", f"--out={reports_dir}"]
    res_rep = subprocess.run(cmd_rep, capture_output=True, text=True)
    assert res_rep.returncode == 0, res_rep.stderr

    # Verify all 5 report files exist
    assert (reports_dir / "batch_summary.csv").exists()
    assert (reports_dir / "daily_overview.csv").exists()
    assert (reports_dir / "lead_report.csv").exists()
    assert (reports_dir / "rejections.csv").exists()
    assert (reports_dir / "data_quality.json").exists()

    # Verify data_quality.json matches exact contract
    with open(reports_dir / "data_quality.json", "r", encoding="utf-8") as f:
        dq = json.load(f)

    assert dq["lead_rows_total"] == 2057
    assert dq["lead_rows_rejected"] == 108
    assert dq["duplicate_lead_rows_merged"] == 57
    assert dq["field_warnings"] == 122
    assert dq["events_total"] == 8745
    assert dq["malformed_events"] == 8
    assert dq["duplicate_events_dropped"] == 237
    assert dq["orphan_events"] == 462
    assert dq["unknown_disposition_events"] == 18

    # Verify batch_summary total leads sum equals 1892
    with open(reports_dir / "batch_summary.csv", "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        total_leads_sum = 0
        for row in reader:
            tot = int(row["total_leads"])
            buckets_sum = (
                int(row["yet_to_reach_out"])
                + int(row["in_progress_telephony_failed"])
                + int(row["in_progress_dialed"])
                + int(row["completed"])
            )
            assert tot == buckets_sum
            total_leads_sum += tot
        assert total_leads_sum == 1892

    # One-command ingestion must produce byte-identical reports.
    combined_db = tmp_path / "combined.db"
    combined_reports = tmp_path / "combined_reports"
    subprocess.run([sys.executable, "-m", "solution", "--db", str(combined_db),
                    "ingest", str(data_dir)], check=True)
    subprocess.run([sys.executable, "-m", "solution", "--db", str(combined_db),
                    "report", "--out", str(combined_reports)], check=True)
    for report in reports_dir.iterdir():
        assert report.read_bytes() == (combined_reports / report.name).read_bytes()

    # Re-ingestion must preserve every report and all quality counters.
    before = {p.name: p.read_bytes() for p in combined_reports.iterdir()}
    subprocess.run([sys.executable, "-m", "solution", "--db", str(combined_db),
                    "ingest", str(data_dir)], check=True)
    subprocess.run([sys.executable, "-m", "solution", "--db", str(combined_db),
                    "report", "--out", str(combined_reports)], check=True)
    assert before == {p.name: p.read_bytes() for p in combined_reports.iterdir()}
