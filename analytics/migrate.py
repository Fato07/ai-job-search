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
    redact_email_addresses,
    read_csv_rows,
    slugify,
    stable_application_id,
    validate_tracker_rows,
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
LEGACY_DEADLINE_COLUMNS = (*LEGACY_COLUMNS, "deadline")
PRE_DEADLINE_TRACKER_COLUMNS = TRACKER_COLUMNS[:-1]


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
    normalized = re.sub(r"[-_]+", " ", status.casefold())
    if any(
        final in normalized
        for final in (
            "rejected",
            "closed",
            "no response",
            "offer declined",
            "withdrawn",
        )
    ):
        return "closed"
    if "offer" in normalized:
        return "offer"
    if "interview" in normalized or "intro call" in normalized:
        return "interview"
    if any(
        keyword in normalized
        for keyword in ("submitted", "confirmed", "outreach", "form submitted", "viewed")
    ):
        return "submitted"
    if any(keyword in normalized for keyword in ("qualified", "ready", "queued")):
        return "qualified"
    return "prospect"
def _canonical_status(status: str) -> str:
    normalized = status.strip().casefold()
    if normalized in {"no response", "offer declined"}:
        return normalized.replace(" ", "_")
    return status



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
    return stage in {"submitted", "interview", "offer", "closed"} or any(
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


def _legacy_screening_reason(status: str, decision: str) -> str:
    if decision != "rejected":
        return ""
    normalized = re.sub(r"[-_]+", " ", status.casefold())
    if "sponsor" in normalized:
        category = "sponsorship_required"
    elif "spam blocked" in normalized:
        category = "ats_spam_blocked"
    elif any(
        phrase in normalized
        for phrase in ("login required", "sign in required", "account required", "call required")
    ):
        category = "application_access_blocked"
    elif any(
        phrase in normalized
        for phrase in ("location", "finland", "finnish")
    ):
        category = "location_blocked"
    elif "closed" in normalized or "replaced requisition" in normalized:
        category = "closed_without_submission"
    elif "not applied" in normalized:
        category = "not_applied"
    elif "hold" in normalized:
        category = "hold"
    elif "skip" in normalized or "underlevelled" in normalized:
        category = "strategic_skip"
    else:
        category = slugify(status)[:80] or "unspecified"
    return f"legacy_screening:{category}"


def migrate_row(row: Mapping[str, str]) -> dict[str, str]:
    columns = set(row)
    if columns in (set(TRACKER_COLUMNS), set(PRE_DEADLINE_TRACKER_COLUMNS)):
        normalized = {
            column: row[column] if column in row else ""
            for column in TRACKER_COLUMNS
        }
        normalized["status"] = _canonical_status(normalized["status"])
        if normalized["status"].casefold() in {
            "hired",
            "rejected",
            "no_response",
            "offer_declined",
            "withdrawn",
        }:
            normalized["stage"] = "closed"
        normalized["geography"] = normalized["geography"].strip() or "unknown"
        normalized["logistics_status"] = (
            normalized["logistics_status"].strip() or "unknown"
        )
        normalized["screening_reason"] = (
            normalized["screening_reason"].strip()
            or _legacy_screening_reason(
                normalized["status"], normalized["screening_decision"]
            )
        )
        return normalized
    if columns not in (set(LEGACY_COLUMNS), set(LEGACY_DEADLINE_COLUMNS)):
        raise ValueError("tracker row columns differ from legacy and normalized schemas")

    discovered_at = row["date"]
    status = _canonical_status(row["status"])
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
        "geography": "unknown",
        "logistics_status": "unknown",
        "channel": row["channel"],
        "screening_decision": _infer_screening_decision(status, stage),
        "screening_reason": _legacy_screening_reason(
            status, _infer_screening_decision(status, stage)
        ),
        "submitted_at": submitted_at,
        "stage": stage,
        "status": status,
        "status_updated_at": max(status_dates, default=discovered_at),
        "contact_person": redact_email_addresses(row["contact_person"]),
        "fit_score": fit_score,
        "fit_label": fit_label,
        "notes": redact_email_addresses(row["notes"]),
        "cv_file": row["cv_file"],
        "cover_letter_file": row["cover_letter_file"],
        "source": row["source"],
        "deadline": row.get("deadline", ""),
    }


def migrate_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    migrated = [migrate_row(row) for row in rows]
    return validate_tracker_rows(migrated, context="normalized migration output")


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
