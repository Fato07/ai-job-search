from __future__ import annotations

import csv
import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Collection, Iterable, Mapping, Sequence

TRACKER_COLUMNS = (
    "application_id", "discovered_at", "company", "sector", "role",
    "role_family", "role_type", "geography", "logistics_status", "channel",
    "screening_decision", "screening_reason", "submitted_at", "stage",
    "status", "status_updated_at", "contact_person", "fit_score", "fit_label",
    "notes", "cv_file", "cover_letter_file", "source",
)
EVENT_COLUMNS = (
    "event_id", "application_id", "occurred_at", "event_type", "source",
    "detail", "source_ref", "created_at",
)
FEEDBACK_COLUMNS = (
    "feedback_id", "application_id", "occurred_at", "stage", "source",
    "evidence_tier", "category", "signal", "evidence_excerpt",
    "required_action", "rule_effect", "resolves_feedback_id", "scope",
    "confidence", "source_ref", "created_at",
)
REVIEW_COLUMNS = (
    "review_id", "occurred_at", "sender", "subject", "company", "role",
    "candidate_application_ids", "reason", "source_ref", "status",
)

_EMAIL_ADDRESS = re.compile(
    r"(?i)(?<![\w.!#$%&'*+/=?^`{|}~-])"
    r"[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}\b"
)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normalized.lower())).strip("-")


def hash_source_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()

def redact_email_addresses(value: str, limit: int | None = None) -> str:
    redacted = _EMAIL_ADDRESS.sub("[address removed]", value)
    if limit is None or len(redacted) <= limit:
        return redacted
    if limit <= 3:
        return redacted[:limit]
    return redacted[: limit - 3].rstrip() + "..."


def stable_application_id(discovered_at: str, company: str, role: str) -> str:
    date_part = discovered_at.replace("-", "")
    base = f"{discovered_at}\x1f{company.strip().casefold()}\x1f{role.strip().casefold()}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"app-{date_part}-{slugify(company)}-{slugify(role)}-{digest}"


def validate_rows(rows, columns, unique_key=None) -> None:
    expected = set(columns)
    seen = set()
    for index, row in enumerate(rows, start=2):
        if set(row) != expected:
            raise ValueError(f"row {index} columns differ from schema")
        if unique_key:
            value = row[unique_key]
            if not value or value in seen:
                raise ValueError(f"row {index} invalid {unique_key}: {value!r}")
            seen.add(value)


def read_csv_rows(path: Path, required: Collection[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(required) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        return [dict(row) for row in reader]


def write_csv_atomic(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    materialized = [{column: str(row.get(column, "")) for column in columns} for row in rows]
    validate_rows(materialized, columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", newline="", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temp_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(materialized)
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
