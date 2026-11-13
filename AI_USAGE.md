# AI Usage Disclosure

## Tools used

- **Google Antigravity:** Used for initial Python CLI implementation, ingestion pipelines, PII
  utilities, and draft SQL queries based on my schema.
- **OpenAI Codex:** Used for code review, security hardening, fixing edge cases, refactoring SQL
  joins, and expanding the test suite.

## What AI contributed

- **Implementation & Boilerplate (Antigravity):** Translated my architecture and database schema
  into the initial Python CLI, schema parsers, lead/event ingestion scripts, PII utilities, and
  initial SQL report generation.
- **Hardening, Fixes & Tests (Codex):** Audited the codebase against requirements, removed
  hardcoded secrets and a custom cipher fallback, isolated workflow joins in reporting queries,
  added IST timezone handling for naive timestamps, implemented fractional-second parsing and
  per-record error handling, fixed `.gitignore` and test-data isolation issues, and expanded the
  existing unit and integration test suite.

## What I wrote or decided myself

- **System Architecture:** Designed the overall project structure and chose a lightweight Python
  CLI + SQLite stack to keep local execution fast, self-contained, and straightforward to
  evaluate.
- **Database Schema Design:** Wrote and reviewed the SQLite database schema, defining the core
  tables, relationships, indexing strategy, and how leads, calls, and events link together.
- **Code & Schema Review:** Reviewed the generated implementation against my schema design and
  verified that the final reporting logic, security controls, and test coverage aligned with the
  intended architecture.

## Where AI output needed correction

- **Security & Secrets:** Initial AI-generated code included public default encryption/matching
  secrets and an undocumented custom cipher fallback; these were removed in favor of proper
  database key binding.
- **Workflow Isolation in Queries:** Early reporting queries joined events by `call_id` without
  scoping by workflow, which was corrected to prevent cross-workflow data leakage.
- **Repository & Test Hygiene:** A broad `reports/` rule in `.gitignore` accidentally excluded
  reporting source code, and early tests depended on untracked local datasets. Both were fixed
  for clean reproducibility.
- **Timestamp & Input Edge Cases:** Adjusted timestamp parsing to treat naive ISO strings as
  IST, support fractional seconds, and skip individual malformed records instead of failing
  entire batches.

## Verification

- Reviewed the database schema implementation and core modules to ensure they matched my
  architectural design.
- Ran the full unit and integration test suite covering schema parsing, encryption key binding,
  late event linkage, replay stability, and sequential vs. root-drop report consistency.
