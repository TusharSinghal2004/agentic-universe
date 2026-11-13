"""Configuration settings for the solution pipeline."""
from __future__ import annotations

from pathlib import Path

# Base paths
ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SCHEMAS_DIR = ROOT_DIR / "assignment-a-data" / "schemas"
DEFAULT_DB_PATH = ROOT_DIR / "data" / "ledger.db"

