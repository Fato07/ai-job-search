from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from datetime import date
from pathlib import Path
from typing import Mapping

from analytics.lock import analytics_lock
from analytics.events import event_id, merge_events, validate_event
from analytics.model import (
    EVENT_COLUMNS,
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    read_tracker_rows,
    stable_application_id,
    validate_tracker_rows,
    write_csv_atomic,
)
from analytics.transaction import commit_staged_files, recover_transaction

_TRANSITIONS = {
    "submitted": ("submitted", "applied"),
    "follow_up": (None, None),
    "interview": ("interview", "interview"),
    "offer": ("offer", "offer"),
    "rejected": ("closed", "rejected"),
    "hired": ("closed", "hired"),
    "no_response": ("closed", "no_response"),
    "offer_declined": ("closed", "offer_declined"),
    "withdrawn": ("closed", "withdrawn"),
}


def _iso_date(value: str) -> str:
    try:
        if date.fromisoformat(value).isoformat() != value:
            raise ValueError
    except ValueError:
        raise ValueError(f"date must use YYYY-MM-DD: {value!r}") from None
    return value


def _event(application_id: str, occurred_on: str, event_type: str, detail: str) -> dict[str, str]:
    occurred_at = f"{occurred_on}T00:00:00Z"
    source_ref = hash_source_ref(
        "\x1f".join(("record", application_id, event_type, occurred_on, detail))
    )
    return {
        "event_id": event_id(application_id, event_type, occurred_at, source_ref),
        "application_id": application_id,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "source": "workflow",
        "detail": detail,
        "source_ref": source_ref,
        "created_at": occurred_at,
    }


def _commit(root: Path, tracker_rows: list[dict[str, str]], events: list[dict[str, str]]) -> None:
    tracker = root / "job_search_tracker.csv"
    events_path = root / "analytics" / "application_events.csv"
    journal = root / ".analytics-record-transaction.json"
    recover_transaction(journal, (tracker, events_path))
    validate_tracker_rows(tracker_rows, context="recorded tracker")
    application_ids = {row["application_id"] for row in tracker_rows}
    for item in events:
        validate_event(item, application_ids)
    with tempfile.TemporaryDirectory(dir=root, prefix=".analytics-record-stage-") as tmp:
        stage = Path(tmp)
        tracker_stage = stage / tracker.name
        events_stage = stage / events_path.name
        write_csv_atomic(tracker_stage, TRACKER_COLUMNS, tracker_rows)
        write_csv_atomic(events_stage, EVENT_COLUMNS, events)
        commit_staged_files(journal, {tracker: tracker_stage, events_path: events_stage})


def _load(root: Path) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    tracker = root / "job_search_tracker.csv"
    events_path = root / "analytics" / "application_events.csv"
    journal = root / ".analytics-record-transaction.json"
    recover_transaction(journal, (tracker, events_path))
    return read_tracker_rows(tracker), read_csv_rows(events_path, EVENT_COLUMNS)

def _record_draft_locked(root: Path, values: Mapping[str, str]) -> str:
    discovered_at = _iso_date(values["discovered_at"])
    company = values["company"].strip()
    role = values["role"].strip()
    if not company or not role:
        raise ValueError("company and role are required")
    application_id = stable_application_id(discovered_at, company, role)
    rows, events = _load(root)
    existing = next((row for row in rows if row["application_id"] == application_id), None)
    if existing is None:
        row = {column: "" for column in TRACKER_COLUMNS}
        row.update(
            application_id=application_id,
            discovered_at=discovered_at,
            company=company,
            sector=values.get("sector", ""),
            role=role,
            role_family=values.get("role_family", "other") or "other",
            role_type=values.get("role_type", ""),
            geography=values.get("geography", "unknown") or "unknown",
            logistics_status=values.get("logistics_status", "unknown") or "unknown",
            channel=values.get("channel", ""),
            screening_decision=values.get("screening_decision", "pending") or "pending",
            screening_reason=values.get("screening_reason", ""),
            stage="drafting",
            status="drafted",
            status_updated_at=discovered_at,
            contact_person=values.get("contact_person", ""),
            fit_score=values.get("fit_score", ""),
            fit_label=values.get("fit_label", ""),
            notes=values.get("notes", ""),
            cv_file=values.get("cv_file", ""),
            cover_letter_file=values.get("cover_letter_file", ""),
            source=values.get("source", ""),
            deadline=values.get("deadline", ""),
        )
        rows.append(row)
    else:
        if existing["company"] != company or existing["role"] != role:
            raise ValueError("stable application_id collision")
        if existing["stage"] == "closed":
            raise ValueError("cannot redraft a closed application")
        for field in (
            "cv_file", "cover_letter_file", "fit_score", "fit_label", "source"
        ):
            value = values.get(field, "")
            if value:
                existing[field] = value
        if values.get("deadline", ""):
            existing["deadline"] = values["deadline"]
        notes = values.get("notes", "").strip()
        additions = [item for item in (notes, "redrafted") if item]
        for addition in additions:
            if addition not in existing["notes"].splitlines():
                existing["notes"] = "\n".join(
                    item for item in (existing["notes"].rstrip(), addition) if item
                )
        if existing["stage"] == "drafting":
            if existing["status_updated_at"] > discovered_at:
                raise ValueError("redraft date precedes current drafting state")
            existing["status_updated_at"] = discovered_at
    incoming = [
        _event(application_id, discovered_at, "discovered", "Application discovered"),
        _event(application_id, discovered_at, "drafting", "Application drafting started"),
    ]
    merged = merge_events(events, incoming, {row["application_id"] for row in rows})
    _commit(root, rows, merged)
    return application_id


def _record_transition_locked(
    root: Path,
    application_id: str,
    event_type: str,
    occurred_on: str,
    *,
    detail: str = "",
) -> None:
    occurred_on = _iso_date(occurred_on)
    if event_type not in _TRANSITIONS:
        raise ValueError(f"unsupported transition: {event_type!r}")
    rows, events = _load(root)
    target = next((row for row in rows if row["application_id"] == application_id), None)
    if target is None:
        raise ValueError(f"unknown application_id: {application_id!r}")
    if target["status_updated_at"] and occurred_on < target["status_updated_at"]:
        raise ValueError("transition date precedes current tracker state")
    stage, status = _TRANSITIONS[event_type]
    if stage is not None:
        target["stage"] = stage
        target["status"] = status
    target["status_updated_at"] = occurred_on
    if event_type == "submitted" and not target["submitted_at"]:
        target["submitted_at"] = occurred_on
    event_detail = detail.strip() or event_type.replace("_", " ").title()
    merged = merge_events(
        events,
        [_event(application_id, occurred_on, event_type, event_detail)],
        {row["application_id"] for row in rows},
    )
    _commit(root, rows, merged)
def record_draft(root: Path, values: Mapping[str, str]) -> str:
    with analytics_lock(root):
        return _record_draft_locked(root, values)


def record_transition(
    root: Path,
    application_id: str,
    event_type: str,
    occurred_on: str,
    *,
    detail: str = "",
) -> None:
    with analytics_lock(root):
        _record_transition_locked(
            root, application_id, event_type, occurred_on, detail=detail
        )




def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atomically record tracker lifecycle changes")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    sub = parser.add_subparsers(dest="command", required=True)
    draft = sub.add_parser("draft")
    for field in ("discovered_at", "company", "role"):
        draft.add_argument(f"--{field.replace('_', '-')}", required=True)
    for field in TRACKER_COLUMNS:
        if field not in {"application_id", "discovered_at", "company", "role", "submitted_at", "stage", "status", "status_updated_at"}:
            draft.add_argument(f"--{field.replace('_', '-')}", default="")
    transition = sub.add_parser("transition")
    transition.add_argument("--application-id", required=True)
    transition.add_argument("--event-type", choices=tuple(_TRANSITIONS), required=True)
    transition.add_argument("--occurred-on", required=True)
    transition.add_argument("--detail", default="")
    return parser


def main() -> None:
    args = _parser().parse_args()
    root = args.root
    if args.command == "draft":
        values = {key: value for key, value in vars(args).items() if isinstance(value, str)}
        print(json.dumps({"application_id": record_draft(root, values)}, sort_keys=True))
    else:
        record_transition(root, args.application_id, args.event_type, args.occurred_on, detail=args.detail)
        print(json.dumps({"application_id": args.application_id, "event_type": args.event_type}, sort_keys=True))


if __name__ == "__main__":
    main()
