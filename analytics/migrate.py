from __future__ import annotations

import argparse
import dataclasses
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from analytics.model import (
    TRACKER_COLUMNS,
    read_csv_rows,
    stable_application_id,
    validate_rows,
    write_csv_atomic,
)

LEGACY_COLUMNS = (
    "date",
    "company",
    "sector",
    "role",
    "role_type",
    "channel",
    "status",
    "contact_person",
    "fit_rating",
    "notes",
    "cv_file",
    "cover_letter_file",
    "source",
)

_FIT_RATING = re.compile(r"(\d{1,3})(?:/100)?\s*(.*)")
_ISO_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_SUBMITTED_DATE = re.compile(r"\bSUBMITTED\s+(\d{4}-\d{2}-\d{2})\b", re.IGNORECASE)

_ROLE_FAMILIES = (
    (("forward deployed", "deployment"), "forward_deployed"),
    (("security",), "ai_security"),
    (("platform", "infrastructure", "sdk", "developer tooling"), "ai_platform"),
    (("applied ai", "ai engineer", "agent"), "applied_ai"),
)


@dataclass(frozen=True)
class MigrationReport:
    row_count: int
    warnings: Sequence[str]


def _infer_role_family(role: str) -> str:
    normalized = role.casefold()
    for keywords, family in _ROLE_FAMILIES:
        if any(keyword in normalized for keyword in keywords):
            return family
    return "other"


def _infer_stage(status: str) -> str:
    normalized = status.casefold()
    if "offer" in normalized:
        return "offer"
    if "interview" in normalized or "intro call" in normalized:
        return "interview"
    if "rejected" in normalized or "closed" in normalized:
        return "closed"
    if any(
        keyword in normalized
        for keyword in ("submitted", "confirmed", "outreach", "form submitted", "viewed")
    ):
        return "submitted"
    if any(keyword in normalized for keyword in ("qualified", "ready", "queued")):
        return "qualified"
    return "prospect"


def _is_blocked_status(status: str) -> bool:
    normalized = re.sub(r"[-_]+", " ", status.casefold())
    blocked = (
        "hold",
        "skip",
        "not applied",
        "not submitted",
        "location blocked",
        "manual apply",
        "manual submit",
        "manual blocked",
        "spam blocked",
        "login required",
    )
    return any(keyword in normalized for keyword in blocked)


def _proves_submission(status: str, stage: str) -> bool:
    if _is_blocked_status(status):
        return False
    normalized = status.casefold()
    evidence = ("submitted", "confirmed", "interview", "offer", "rejected", "outreach", "viewed")
    return stage in {"submitted", "interview", "offer"} or any(
        keyword in normalized for keyword in evidence
    )


def _infer_screening_decision(status: str, stage: str) -> str:
    if _is_blocked_status(status):
        return "rejected"
    if _proves_submission(status, stage):
        return "qualified"
    return "pending"


def _split_fit_rating(value: str) -> tuple[str, str]:
    match = _FIT_RATING.match(value)
    if match is None:
        return "", value
    return match.group(1), match.group(2)


def migrate_row(row: Mapping[str, str]) -> dict[str, str]:
    columns = set(row)
    if columns == set(TRACKER_COLUMNS):
        return {column: row[column] for column in TRACKER_COLUMNS}
    if columns != set(LEGACY_COLUMNS):
        raise ValueError("tracker row columns differ from legacy and normalized schemas")

    discovered_at = row["date"]
    status = row["status"]
    stage = _infer_stage(status)
    fit_score, fit_label = _split_fit_rating(row["fit_rating"])
    submitted_match = _SUBMITTED_DATE.search(status)
    submitted_at = ""
    if not _is_blocked_status(status):
        submitted_at = (
            submitted_match.group(1)
            if submitted_match is not None
            else discovered_at if _proves_submission(status, stage) else ""
        )
    status_dates = _ISO_DATE.findall(status)

    return {
        "application_id": stable_application_id(
            discovered_at, row["company"], row["role"]
        ),
        "discovered_at": discovered_at,
        "company": row["company"],
        "sector": row["sector"],
        "role": row["role"],
        "role_family": _infer_role_family(row["role"]),
        "role_type": row["role_type"],
        "geography": "",
        "logistics_status": "",
        "channel": row["channel"],
        "screening_decision": _infer_screening_decision(status, stage),
        "screening_reason": "",
        "submitted_at": submitted_at,
        "stage": stage,
        "status": status,
        "status_updated_at": max(status_dates, default=discovered_at),
        "contact_person": row["contact_person"],
        "fit_score": fit_score,
        "fit_label": fit_label,
        "notes": row["notes"],
        "cv_file": row["cv_file"],
        "cover_letter_file": row["cover_letter_file"],
        "source": row["source"],
    }


def migrate_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    migrated = [migrate_row(row) for row in rows]
    validate_rows(migrated, TRACKER_COLUMNS, unique_key="application_id")
    return migrated


def migrate_tracker(path: Path, apply: bool) -> MigrationReport:
    migrated = migrate_rows(read_csv_rows(path, set()))
    if apply:
        write_csv_atomic(path, TRACKER_COLUMNS, migrated)
    return MigrationReport(row_count=len(migrated), warnings=())


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("tracker", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = migrate_tracker(args.tracker, apply=args.apply)
    print(json.dumps(dataclasses.asdict(report), indent=2))


if __name__ == "__main__":
    main()
