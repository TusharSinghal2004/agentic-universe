"""Command-line interface for the pipeline."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.config import DEFAULT_DB_PATH, DEFAULT_SCHEMAS_DIR
from solution.db import Database
from solution.pipeline.events import EventIngestor
from solution.pipeline.leads import LeadIngestor
from solution.reports.generator import ReportGenerator
from solution.schemas import SchemaCatalog


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m solution",
        description="Agentic Universe - Interaction Ledger & Reporting Pipeline"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database file (default: {DEFAULT_DB_PATH})"
    )
    parser.add_argument(
        "--schemas",
        type=Path,
        default=DEFAULT_SCHEMAS_DIR,
        help=f"Path to schemas directory (default: {DEFAULT_SCHEMAS_DIR})"
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Ingest command
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a data drop folder (leads and events)")
    ingest_parser.add_argument("drop_path", type=Path, help="Path to drop folder (e.g. data/drop_1)")

    # Report command
    report_parser = subparsers.add_parser("report", help="Generate all report files")
    report_parser.add_argument("--out", type=Path, default=Path("reports"), help="Output directory for reports")

    # Reset command
    reset_parser = subparsers.add_parser("reset", help="Reset/delete existing database")

    return parser


def handle_ingest(args) -> int:
    drop_path = Path(args.drop_path).resolve()
    if not drop_path.is_dir():
        print(f"Error: Drop path is not a directory: {drop_path}", file=sys.stderr)
        return 1

    # A generated root can be ingested in one command, in drop-name order.
    children = sorted(p for p in drop_path.glob("drop_*") if p.is_dir())
    if children and not (drop_path / "leads").exists():
        for child in children:
            child_args = argparse.Namespace(**vars(args))
            child_args.drop_path = child
            result = handle_ingest(child_args)
            if result:
                return result
        return 0

    lead_files = sorted((p for p in drop_path.glob("leads/*/*.csv") if p.is_file()), key=lambda p: p.name)
    event_files = sorted((p for p in drop_path.glob("events/*.jsonl") if p.is_file()), key=lambda p: p.name)
    if not lead_files and not event_files:
        print(f"Error: No lead CSV or event JSONL files found in {drop_path}", file=sys.stderr)
        return 1

    drop_name = drop_path.name
    print(f"Starting ingestion for drop: '{drop_name}' from {drop_path}")

    catalog = SchemaCatalog(args.schemas)
    db = Database(args.db)

    lead_ingestor = LeadIngestor(db, catalog)
    event_ingestor = EventIngestor(db, catalog)
    db.sync_disposition_catalogue(catalog.dispositions)

    # 1. Ingest leads: process drops in the order ingested, files in filename order
    ingested_leads_count = 0
    skipped_leads_count = 0

    for lf in lead_files:
        if lead_ingestor.ingest_lead_file(lf, drop_name):
            ingested_leads_count += 1
        else:
            skipped_leads_count += 1

    # 2. Ingest events: files in filename order
    ingested_events_count = 0
    skipped_events_count = 0

    for ef in event_files:
        if event_ingestor.ingest_event_file(ef, drop_name):
            ingested_events_count += 1
        else:
            skipped_events_count += 1

    print(
        f"Completed '{drop_name}': "
        f"Leads files (ingested: {ingested_leads_count}, skipped: {skipped_leads_count}) | "
        f"Event files (ingested: {ingested_events_count}, skipped: {skipped_events_count})"
    )
    return 0


def handle_report(args) -> int:
    out_dir = Path(args.out).resolve()
    print(f"Generating reports into: {out_dir}")

    db = Database(args.db)
    generator = ReportGenerator(db)
    generator.generate_all(out_dir)

    print("Successfully generated all reports:")
    for f in [
        "batch_summary.csv",
        "daily_overview.csv",
        "lead_report.csv",
        "rejections.csv",
        "data_quality.json"
    ]:
        print(f"  - {out_dir / f}")
    return 0


def handle_reset(args) -> int:
    db_path = Path(args.db).resolve()
    if db_path.exists():
        db_path.unlink()
        wal = db_path.with_name(db_path.name + "-wal")
        shm = db_path.with_name(db_path.name + "-shm")
        if wal.exists():
            wal.unlink()
        if shm.exists():
            shm.unlink()
        print(f"Reset database at: {db_path}")
    else:
        print(f"Database at {db_path} does not exist.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "ingest":
            return handle_ingest(args)
        elif args.command == "report":
            return handle_report(args)
        elif args.command == "reset":
            return handle_reset(args)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0
