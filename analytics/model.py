from __future__ import annotations

import csv
import hashlib
import math
import os
import re
import tempfile
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Collection, Iterable, Mapping, Sequence

TRACKER_COLUMNS = (
    "application_id", "discovered_at", "company", "sector", "role",
    "role_family", "role_type", "geography", "logistics_status", "channel",
    "screening_decision", "screening_reason", "submitted_at", "stage",
    "status", "status_updated_at", "contact_person", "fit_score", "fit_label",
    "notes", "cv_file", "cover_letter_file", "source", "deadline",
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

ROLE_FAMILIES = frozenset(
    {"applied_ai", "forward_deployed", "ai_platform", "ai_security", "other"}
)
LOGISTICS_STATUSES = frozenset(
    {"pass", "sponsorship_required", "relocation_required", "blocked", "unknown"}
)
SCREENING_DECISIONS = frozenset({"pending", "rejected", "qualified"})
TRACKER_STAGES = frozenset(
    {
        "prospect",
        "qualified",
        "drafting",
        "submitted",
        "response",
        "interview",
        "offer",
        "closed",
    }
)
LIFECYCLE_EVENT_TYPES = frozenset(
    {
        "discovered",
        "screened",
        "qualified",
        "drafting",
        "submitted",
        "received",
        "viewed",
        "follow_up",
        "interview",
        "rejected",
        "withdrawn",
        "offer",
        "hired",
        "no_response",
        "offer_declined",
    }
)
LIFECYCLE_EVENT_SOURCES = frozenset(
    {"tracker_backfill", "gmail", "user", "browser", "workflow"}
)
REVIEW_STATUSES = frozenset({"pending", "resolved", "ignored"})

_APPLICATION_ID = re.compile(r"app-[A-Za-z0-9][A-Za-z0-9-]*")

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


def _tracker_error(context: str, row: int, column: str, detail: str) -> ValueError:
    return ValueError(f"{context}: tracker row {row} column {column}: {detail}")


def _validate_iso_value(
    value: str,
    *,
    context: str,
    row: int,
    column: str,
    required: bool,
) -> None:
    if not value:
        if required:
            raise _tracker_error(context, row, column, "value is required")
        return
    try:
        if "T" in value:
            parsed = datetime.fromisoformat(
                value[:-1] + "+00:00" if value.endswith("Z") else value
            )
            if parsed.tzinfo is None:
                raise ValueError
        elif date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise _tracker_error(
            context, row, column, "must be an ISO date or timezone-aware timestamp"
        ) from None


def validate_tracker_rows(
    rows: Iterable[Mapping[str, object]],
    *,
    context: str = "tracker",
) -> list[dict[str, str]]:
    materialized = [dict(row) for row in rows]
    seen_ids: set[str] = set()
    for row_number, row in enumerate(materialized, start=2):
        if tuple(row) != TRACKER_COLUMNS:
            raise _tracker_error(
                context,
                row_number,
                "<schema>",
                "columns or order differ from the canonical tracker schema",
            )
        for column in TRACKER_COLUMNS:
            if not isinstance(row[column], str):
                raise _tracker_error(
                    context, row_number, column, "cell must be a string"
                )

        application_id = row["application_id"]
        if not _APPLICATION_ID.fullmatch(application_id):
            raise _tracker_error(
                context,
                row_number,
                "application_id",
                "must be a stable app-* identifier",
            )
        if application_id in seen_ids:
            raise _tracker_error(
                context, row_number, "application_id", "must be unique"
            )
        seen_ids.add(application_id)

        for column in ("company", "role", "status"):
            if not row[column].strip():
                raise _tracker_error(
                    context, row_number, column, "value is required"
                )
        _validate_iso_value(
            row["discovered_at"],
            context=context,
            row=row_number,
            column="discovered_at",
            required=True,
        )
        _validate_iso_value(
            row["submitted_at"],
            context=context,
            row=row_number,
            column="submitted_at",
            required=False,
        )
        _validate_iso_value(
            row["status_updated_at"],
            context=context,
            row=row_number,
            column="status_updated_at",
            required=True,
        )
        deadline = row["deadline"]
        if deadline:
            try:
                if date.fromisoformat(deadline).isoformat() != deadline:
                    raise ValueError
            except ValueError:
                raise _tracker_error(
                    context,
                    row_number,
                    "deadline",
                    "must be an ISO date in YYYY-MM-DD form",
                ) from None


        domains = {
            "role_family": ROLE_FAMILIES,
            "logistics_status": LOGISTICS_STATUSES,
            "screening_decision": SCREENING_DECISIONS,
            "stage": TRACKER_STAGES,
        }
        for column, allowed in domains.items():
            if row[column] not in allowed:
                raise _tracker_error(
                    context,
                    row_number,
                    column,
                    f"invalid value {row[column]!r}",
                )
        if not row["geography"].strip():
            raise _tracker_error(
                context, row_number, "geography", "value is required"
            )

        fit_score = row["fit_score"].strip()
        if fit_score:
            try:
                parsed_score = float(fit_score)
            except ValueError:
                parsed_score = math.nan
            if not math.isfinite(parsed_score) or not 0.0 <= parsed_score <= 100.0:
                raise _tracker_error(
                    context, row_number, "fit_score", "must be between 0 and 100"
                )
        if (
            row["screening_decision"] == "rejected"
            and not row["screening_reason"].strip()
        ):
            raise _tracker_error(
                context,
                row_number,
                "screening_reason",
                "is required for rejected screening decisions",
            )
    return materialized


def read_tracker_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != TRACKER_COLUMNS:
            raise ValueError(
                f"{path}: tracker header differs from the canonical header/order"
            )
        rows = [dict(row) for row in reader]
    return validate_tracker_rows(rows, context=str(path))


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
            "w", newline="", encoding="utf-8", dir=path.parent,
            prefix=f".{path.name}.write-", delete=False,
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
