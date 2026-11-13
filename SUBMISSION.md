# Submission

## Candidate

- Name: Tushar Singhal
- Assignment chosen: A — Interaction Ledger & Reporting Pipeline.
- Time spent: 7 hours total including design, implementation, and verification.

## Setup and run

Python 3.11+ is required. From the repository root:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Generate two independent secrets once; persist them securely outside Git.
$env:PII_SECRET_KEY = python -c "import secrets; print(secrets.token_hex(32))"
$env:HMAC_SALT = python -c "import secrets; print(secrets.token_hex(32))"

python -m pytest
python assignment-a-data/tools/generate_data.py --seed 42 --scale small --out data
python -m solution --db data/reviewed-ledger.db ingest data/drop_1
python -m solution --db data/reviewed-ledger.db ingest data/drop_2
python -m solution --db data/reviewed-ledger.db report --out reports/
```

On Linux/macOS, use these activation and secret-generation commands, then run the same
installation, test, and pipeline commands shown above:

```bash
source .venv/bin/activate
export PII_SECRET_KEY="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export HMAC_SALT="$(python -c 'import secrets; print(secrets.token_hex(32))')"
```

Keep both secrets stable for a database's entire lifetime. In later shells restore the same
values; do not generate replacements for an existing database. There is no key rotation.
Ingestion rejects mismatched encryption or matching keys using stored key verifiers. Legacy
populated databases require successful phone decryption and hash verification before adoption;
otherwise use a fresh database. The application reads environment variables directly; it does
not load `.env` files. Tests supply isolated credentials and generate their own synthetic input.

Alternatively, `python -m solution --db data/reviewed-ledger.db ingest data` processes all
`drop_*` directories in name order. Individual drops follow the user's invocation order; files
are processed in filename order. Commands use paths relative to the repo root.

The output is `batch_summary.csv`, `daily_overview.csv`, `lead_report.csv`, `rejections.csv`,
and `data_quality.json`. Reports contain masked phones, not full names or phone numbers.
Generated inputs, databases, and output reports are excluded from Git.

## Design

```text
Lead CSVs -> schema validation and merging -> SQLite leads
Event JSONL -> event-ID deduplication -> append-only interaction ledger
SQL staging tables -> call facts -> lead summaries -> five contract reports
```

SQLite uses WAL mode and per-file transactions. Each transaction includes data writes, file
tracking, and counters, so a failed file rolls back together. Earlier successful files remain
committed. Ledger rows are not updated by ingestion or overrides; this is an application
convention, not a database permission or trigger restriction.

Tables:

- `leads`: primary key `(workflow_id, phone_hash)`, original batch, current reference, encrypted
  name and phone, masked phone, non-sensitive attributes, timestamps.
- `interaction_ledger`: unique event IDs, IST timestamps, call and workflow IDs, encrypted
  phone, phone hash, disposition, and event payload.
- `call_phone_map`: `(workflow_id, call_id)` to phone hash for phone-less overrides.
- `lead_rejections`: workflow, batch, nonblank row number, and rejection reason; no PII.
- `ingested_files`: absolute path and SHA-256 file hash for unchanged-file skipping.
- `disposition_catalogue`: workflow-specific terminal and success flags.
- `counters`: accumulated ingestion quality metrics.
- `security_metadata`: keyed fingerprints binding the database to both supplied keys; actual key
  material is not stored.

## Decisions and assumptions

- Identity is workflow plus canonical Indian mobile number. First batch wins; later valid
  non-null fields overwrite earlier fields. Defaults fill remaining nulls.
- HMAC-SHA256 permits exact phone matching without plaintext phone indexes. AES-256-GCM uses
  fresh random 12-byte nonces and authenticates encrypted values. `cryptography` is required;
  there is no custom cipher fallback. Secrets are supplied separately from the database. Someone
  with the keys can decrypt the stored PII.
- PII handling targets the two supplied schemas' `customer_name` and `phone` fields. Adding new
  PII fields requires extending storage and sanitization. Event payloads follow the supplied
  contract and are stored as JSON; arbitrary PII in payload text is not automatically detected
  or scrubbed.
- Events without leads are retained and linked dynamically when reporting. Orphan and
  unknown-disposition counts reflect current database state, in spec priority order.
- Overrides outrank sets regardless of their relative timestamps. Within either kind, the latest
  occurrence wins. Unknown codes do not replace known dispositions.
- Calls require a linked initiation. Reporting joins use both workflow and call ID. A call ID
  identifies one call within a workflow; conflicting phone mappings are not reconciled. Exact
  timestamp ties have no additional business ordering rule.
- Only `disposition.override` events may link through call ID without a valid phone. Other
  events with invalid or missing phones remain orphans.
- Additional malformed-input policy: invalid timestamps, missing/invalid structural fields,
  non-object payloads, and non-string disposition codes are counted as malformed and skipped.
  Structurally rejected records are not reserved as seen event IDs. This extends the spec beyond
  its JSON/missing-ID definition so dirty input does not abort a file. No raw malformed data is
  stored or logged.
- Naive timestamps mean IST; fractional-second ISO timestamps are supported. All report dates
  and times are converted to IST.
- Same-path, same-content ingestion is idempotent. Relocated or edited lead files are treated as
  fresh input; replacement-file correction and content-only dedupe are not implemented. Use a
  new database for a full corrected backfill.

## Validation

Verified across the full test suite: **32 tests passed**, both in the working repository and in
a source-only copy assembled from Git-visible files without generated inputs or databases. This
used the installed Python environment, not a fresh dependency install.

The test suite includes regressions for invalid-phone event isolation, fractional UTC
timestamps, malformed records followed by valid records, invalid input paths, both changed keys
(including skipped files), and legacy database key adoption/rejection. Fresh CLI runs for seeds
7, 123, and 2026 checked 5,700 lead-report rows and daily metrics against a separate Python
aggregation from raw events and stored lead identities: no mismatches. Replay preserved all five
reports byte-for-byte. This independently checks event aggregation, not lead validation. Small
runs took roughly 3–4 seconds.

The test suite also covers phone, date, decimal and enum parsing, encrypted round trips,
authentication failure, required keys, disposition precedence, lead buckets, workflow isolation
for shared call IDs, and delayed lead linkage including phone-less overrides. The seed-42
integration test generates its own input, verifies quality counts and bucket totals, compares
all five reports byte-for-byte between sequential and root ingestion, and checks report
stability after replaying both drops.

Expected seed-42 small data: 2,057 lead rows, 108 rejections, 57 merged duplicates, 1,892
distinct leads; 8,745 event lines, 8 malformed, 237 duplicates, 462 final orphans, and 18 linked
unknown dispositions. These are regression expectations, not an independent reference
implementation proving every output on unseen data.

## Performance and limitations

On larger datasets, single-process ingestion and full SQL report rebuilds take several minutes.
The under-two-minute performance stretch goal was not targeted in this iteration; the current
version has not been re-benchmarked at large scale. Reporting rebuilds derived SQL tables from
the ledger. Ingestion skips old files but loads existing workflow leads/event IDs into memory;
it is not a bounded-memory stream. Single-writer operation is intended. Concurrent ingestion and
consistent reports while another process writes have not been validated. Malformed JSON, missing
IDs, invalid timestamps, and invalid event structures are counted and skipped per record.
Empty/unrecognized input directories return an error before database creation. Root ingestion
commits each valid file independently; an invalid later drop returns an error without rolling
back earlier committed drops.

## Production and next steps

Run ingestion as a scheduled single-writer job, retry failed files, alert on rejection and
orphan rates, and generate reports after ingestion completes. Keep input manifests for
reproducible backfills into a new database. At higher throughput, profile first, then replace
full-workflow preloads with indexed lookups/staging and move to PostgreSQL if multiple writers
are needed. Add managed key storage/rotation, schema-driven PII handling, and broader
independent unseen-seed report checks.
