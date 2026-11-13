# Assignment A: Interaction Ledger & Reporting Pipeline

This solution turns messy customer CSVs and call-event JSONL files into reliable reports.
It runs as a Python CLI and stores its data in SQLite.

## Documentation

- [Setup, design, and validation](../SUBMISSION.md)
- [Pipeline walkthrough](../docs/SOLUTION_GUIDE.md)
- [Assignment rules and output contracts](../assignment-a-data/README.md)
- [AI usage disclosure](../AI_USAGE.md)

## Repository map

| Path | Purpose |
|---|---|
| `assignment-a-data/` | Assignment A requirements, schemas, catalogue, and generator |
| `assignment-b-workflow/` | Assignment B requirements, fixtures, and mock provider; not implemented |
| `solution/cli.py` | Ingest, report, and reset commands |
| `solution/schemas.py` | Header mapping, phone normalization, and value parsing |
| `solution/pipeline/leads.py` | Lead validation, rejection, and duplicate merging |
| `solution/pipeline/events.py` | Event validation, deduplication, and ledger writes |
| `solution/db.py` | SQLite schema, transactions, and database key verification |
| `solution/security.py` | AES-GCM encryption, HMAC matching, and phone masking |
| `solution/reports/generator.py` | SQL aggregation and report export |
| `tests/` | Unit, regression, and end-to-end tests |
| `docs/` | Glossary and pipeline explanation |
| `data/`, `data_large/` | Generated inputs and local databases; ignored by Git |
| `reports/`, `reports_large/` | Generated report outputs; ignored by Git |

## Run the pipeline

Run commands from the repository root. Install dependencies and configure the two
secrets using the [setup instructions](../SUBMISSION.md#setup-and-run) first.

```powershell
# Tests generate their own input and use isolated test credentials.
python -m pytest

# Generate small synthetic input.
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data

# Ingest in delivery order, then generate reports.
python -m solution --db data/reviewed-ledger.db ingest data/drop_1
python -m solution --db data/reviewed-ledger.db ingest data/drop_2
python -m solution --db data/reviewed-ledger.db report --out reports/
```

To ingest both drops in one command:

```powershell
python -m solution --db data/reviewed-ledger.db ingest data
```

Replaying unchanged input leaves the reports and quality counters unchanged. Use a new
database when testing a different seed or a corrected full dataset.

## Reports

| File | What it contains |
|---|---|
| `batch_summary.csv` | Lead counts in each progress bucket, grouped by original batch |
| `daily_overview.csv` | IST-day call totals, connections, connectivity rate, and successful leads |
| `lead_report.csv` | One row per lead, with masked phone, call counts, and current outcome |
| `rejections.csv` | Invalid lead rows and their first rejection reason; no PII |
| `data_quality.json` | Input, rejection, warning, duplicate, orphan, and unknown-outcome counts |

## Common errors

| Message or situation | Action |
|---|---|
| Missing `PII_SECRET_KEY` or `HMAC_SALT` | Set both environment variables before ingestion. The CLI does not load `.env`. |
| Database keys do not match | Restore the original keys or choose a fresh database path. |
| Legacy database keys cannot be verified | Supply the original keys or rebuild in a fresh database. |
| No lead CSV or event JSONL files found | Point to a drop containing `leads/<workflow>/*.csv` or `events/*.jsonl`, or a root containing `drop_*` folders. |
| Some events remain orphans | Their lead is missing or rejected, their phone is invalid, or an override's parent call cannot be resolved. Later drops may resolve them. |
| New input produces unexpected accumulated totals | Check whether the database already contains an earlier dataset. |

Malformed event records are counted and skipped. Each input file commits atomically;
a failure in a later file does not undo earlier successful files.

## Other commands

```powershell
python -m solution --help
python -m solution ingest --help
python -m solution report --help
```

The `reset` command deletes the selected database and its SQLite sidecar files. Use it
only for a disposable local dataset whose contents you intend to remove.
