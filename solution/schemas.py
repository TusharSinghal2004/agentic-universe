"""Schema loading, header mapping, and type validation logic."""
from __future__ import annotations

import csv
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


def normalize_header(h: str) -> str:
    """Normalize a header: lower-case, trim, replace runs of _ or whitespace with single space."""
    return re.sub(r"[\s_]+", " ", h.lower().strip())


def normalize_phone(val: Optional[str]) -> Optional[str]:
    """Validate and normalize an Indian mobile phone number.
    
    Rules:
    - Strip every non-digit.
    - If 12 digits starting with '91', drop the '91'.
    - If 11 digits starting with '0', drop the '0'.
    - The result is valid only if it is exactly 10 digits and starts with 6, 7, 8 or 9.
    - Canonical format: '+91XXXXXXXXXX'.
    """
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    digits = re.sub(r"\D", "", val)
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    if len(digits) == 10 and digits[0] in "6789":
        return f"+91{digits}"
    return None


def parse_date(val: Optional[str]) -> Optional[str]:
    """Parse date in accepted formats: YYYY-MM-DD, DD/MM/YYYY, DD-MM-YYYY, DD-Mon-YYYY.
    
    Dates are day-first. Month name is case-insensitive. Impossible dates are invalid.
    Returns ISO-8601 string 'YYYY-MM-DD' or None.
    """
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    
    # Try standard day-first and ISO formats
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(val, fmt).date().isoformat()
        except ValueError:
            pass

    # Try DD-Mon-YYYY (case-insensitive month, e.g. '01-Dec-2026')
    parts = val.split("-")
    if len(parts) == 3 and len(parts[1]) == 3 and parts[1].isalpha():
        try:
            norm = f"{parts[0]}-{parts[1].capitalize()}-{parts[2]}"
            return datetime.strptime(norm, "%d-%b-%Y").date().isoformat()
        except ValueError:
            pass

    return None


def parse_decimal(val: Optional[str]) -> Optional[float]:
    """Parse decimal value.
    
    Optionally prefixed by ₹, Rs, Rs. or INR (case-insensitive, optional space).
    Commas are thousands separators. Must be >= 0. Round to 2 places.
    """
    if not val:
        return None
    val = val.strip()
    if not val:
        return None
    
    # Match currency prefix and numbers with commas/periods
    m = re.match(r"^(?:₹|rs\.?|inr)?\s*([0-9,]+(?:\.[0-9]+)?)$", val, flags=re.IGNORECASE)
    if not m:
        return None
    num_str = m.group(1).replace(",", "")
    try:
        fval = float(num_str)
        if fval < 0:
            return None
        return round(fval, 2)
    except ValueError:
        return None


def parse_enum(val: Optional[str], allowed_values: List[str]) -> Optional[str]:
    """Compare case-insensitively against allowed values and return lower-case."""
    if not val:
        return None
    val = val.strip().lower()
    if not val:
        return None
    lookup = {v.lower(): v.lower() for v in allowed_values}
    return lookup.get(val)


class SchemaCatalog:
    """Loads and caches workflow lead schemas and disposition catalogue."""
    
    def __init__(self, schemas_dir: Path):
        self.schemas_dir = Path(schemas_dir)
        self.workflow_schemas: Dict[str, dict] = {}
        self.dispositions: Dict[Tuple[str, str], dict] = {} # (workflow_id, code) -> info
        self._load()

    def _load(self):
        # Load workflow JSON schemas
        for p in self.schemas_dir.glob("*.json"):
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
                wf_id = data["workflow_id"]
                self.workflow_schemas[wf_id] = data

        # Load dispositions CSV
        disp_file = self.schemas_dir / "dispositions.csv"
        if disp_file.exists():
            with open(disp_file, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    wf = row["workflow_id"].strip()
                    code = row["code"].strip()
                    self.dispositions[(wf, code)] = {
                        "workflow_id": wf,
                        "code": code,
                        "label": row["label"].strip(),
                        "is_terminal": row["is_terminal"].strip().lower() == "true",
                        "is_success": row["is_success"].strip().lower() == "true"
                    }

    def get_schema(self, workflow_id: str) -> Optional[dict]:
        return self.workflow_schemas.get(workflow_id)

    def get_reference_field(self, workflow_id: str) -> str:
        """Find the schema field marked reference: true (e.g. policy_number or order_id)."""
        sc = self.get_schema(workflow_id)
        if sc:
            for f in sc["fields"]:
                if f.get("reference"):
                    return f["name"]
        return "external_ref"

    def is_valid_disposition(self, workflow_id: str, code: str) -> bool:
        return (workflow_id, code) in self.dispositions
