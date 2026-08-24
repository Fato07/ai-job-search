from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from analytics.model import (
    EVENT_COLUMNS,
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    validate_rows,
    write_csv_atomic,
)

_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?])(?:\s+|$)|\n+")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_REJECTION_EVIDENCE = re.compile(
    r"\brejected by\b|\brejection (?:email )?(?:was )?received\b",
    re.IGNORECASE,
)
_VIEWED_EVIDENCE = re.compile(
    r"\bapplication (?:was )?viewed\b"
    r"|\bviewed (?:the |your )?application\b"
    r"|\bviewed notification (?:was )?received\b",
    re.IGNORECASE,
)
_FOLLOW_UP_EVIDENCE = re.compile(
    r"\bfollow[- ]up (?:email )?(?:was )?sent\b"
    r"|\bsent (?:a |the )?follow[- ]up(?: email)?\b"
    r"|\bfollowed up\b",
    re.IGNORECASE,
)
_INTERVIEW_EVIDENCE = re.compile(
    r"\b(?:video |phone )?interview (?:invite|request) (?:was )?received\b"
    r"|\binvited to interview\b"
    r"|\b(?:video |phone )?interview (?:was )?"
    r"(?:done|scheduled|held|happened|completed|conducted|took place)\b"
    r"|\bintro[- ]call (?:was )?"
    r"(?:done|scheduled|held|happened|completed|conducted|took place)\b"
    r"|\b(?:phone|video) screen\b"
    r"|\b(?:after|following) the \d{4}-\d{2}-\d{2}"
    r"(?: \d{2}:\d{2})? (?:video |phone )?interview\b",
    re.IGNORECASE,
)
_RECEIVED_EVIDENCE = re.compile(
    r"\bconfirmation\b.{0,80}\breceived\b"
    r"|\b(?:application|submission) (?:was |successfully )?received\b"
    r"|\breceived (?:the |your )?(?:application|submission)\b"
    r"|\bconfirming receipt\b",
    re.IGNORECASE,
)
_STATUS_INTERVIEW_EVIDENCE = re.compile(
    r"\b(?:interview|intro[- ]call|phone screen|video screen)\b.*"
    r"\b(?:done|scheduled|held|happened|completed|conducted)\b",
    re.IGNORECASE,
)
_NOTE_EVENT_PATTERNS = (
    ("rejected", _REJECTION_EVIDENCE),
    ("viewed", _VIEWED_EVIDENCE),
    ("follow_up", _FOLLOW_UP_EVIDENCE),
    ("interview", _INTERVIEW_EVIDENCE),
    ("received", _RECEIVED_EVIDENCE),
)


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _date_timestamp(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid ISO date: {value!r}")
    return f"{value}T00:00:00Z"


def event_id(
    application_id: str, event_type: str, occurred_at: str, source_ref: str
) -> str:
    material = "\x1f".join((application_id, event_type, occurred_at, source_ref))
    return f"evt-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _event(
    application_id: str,
    event_type: str,
    occurred_date: str,
    detail: str,
    source_text: str,
    created_at: str,
) -> dict[str, str]:
    occurred_at = _date_timestamp(occurred_date)
    source_ref = hash_source_ref(f"{application_id}\x1f{source_text}")
    return {
        "event_id": event_id(application_id, event_type, occurred_at, source_ref),
        "application_id": application_id,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "source": "tracker_backfill",
        "detail": detail,
        "source_ref": source_ref,
        "created_at": created_at,
    }


def _nearest_date(sentence: str, phrase: re.Match[str]) -> str | None:
    dates = list(_ISO_DATE.finditer(sentence))
    if not dates:
        return None

    def distance(candidate: re.Match[str]) -> int:
        if candidate.end() <= phrase.start():
            return phrase.start() - candidate.end()
        if candidate.start() >= phrase.end():
            return candidate.start() - phrase.end()
        return 0

    return min(dates, key=distance).group(0)


def _note_events(
    application_id: str, notes: str, created_at: str
) -> Iterable[dict[str, str]]:
    for sentence in _SENTENCE_BOUNDARY.split(notes):
        sentence = sentence.strip()
        if not sentence or _ISO_DATE.search(sentence) is None:
            continue
        sentence_is_rejection = _REJECTION_EVIDENCE.search(sentence) is not None
        for event_type, pattern in _NOTE_EVENT_PATTERNS:
            if event_type == "received" and sentence_is_rejection:
                continue
            phrase = pattern.search(sentence)
            if phrase is None:
                continue
            occurred_date = _nearest_date(sentence, phrase)
            if occurred_date is not None:
                yield _event(
                    application_id,
                    event_type,
                    occurred_date,
                    sentence[:280],
                    f"notes:{sentence}",
                    created_at,
                )


def backfill_events(
    applications: Iterable[Mapping[str, str]], now: datetime
) -> list[dict[str, str]]:
    created_at = _utc_timestamp(now)
    events: list[dict[str, str]] = []
    logical_keys: set[tuple[str, str, str]] = set()

    def add(event: dict[str, str]) -> None:
        key = (event["application_id"], event["event_type"], event["occurred_at"])
        if key not in logical_keys:
            logical_keys.add(key)
            events.append(event)

    for application in applications:
        application_id = application.get("application_id", "")
        if not application_id:
            raise ValueError("application_id is required")
        discovered_at = application.get("discovered_at", "")
        add(
            _event(
                application_id,
                "discovered",
                discovered_at,
                "Discovered",
                f"discovered_at:{discovered_at}",
                created_at,
            )
        )

        submitted_at = application.get("submitted_at", "").strip()
        if submitted_at:
            add(
                _event(
                    application_id,
                    "submitted",
                    submitted_at,
                    "Submitted",
                    f"submitted_at:{submitted_at}",
                    created_at,
                )
            )

        status = application.get("status", "")
        normalized_status = status.casefold()
        stage = application.get("stage", "").casefold()
        status_date = application.get("status_updated_at", "") or discovered_at
        status_source = f"status:{status}"
        if "confirmed" in normalized_status or "received" in normalized_status:
            add(
                _event(
                    application_id,
                    "received",
                    status_date,
                    status or "Received",
                    status_source,
                    created_at,
                )
            )
        if (
            (stage == "interview" and "rejected" not in normalized_status)
            or _STATUS_INTERVIEW_EVIDENCE.search(status) is not None
        ):
            add(
                _event(
                    application_id,
                    "interview",
                    status_date,
                    status or "Interview",
                    status_source,
                    created_at,
                )
            )
        if "rejected" in normalized_status:
            add(
                _event(
                    application_id,
                    "rejected",
                    status_date,
                    status,
                    status_source,
                    created_at,
                )
            )
        if "offer" in normalized_status:
            add(
                _event(
                    application_id,
                    "offer",
                    status_date,
                    status,
                    status_source,
                    created_at,
                )
            )

        for event in _note_events(
            application_id, application.get("notes", ""), created_at
        ):
            add(event)

    validate_rows(events, EVENT_COLUMNS, unique_key="event_id")
    return sorted(
        events,
        key=lambda event: (
            event["occurred_at"],
            event["application_id"],
            event["event_type"],
            event["event_id"],
        ),
    )


def merge_events(
    existing: Iterable[Mapping[str, str]],
    incoming: Iterable[Mapping[str, str]],
    application_ids: set[str],
) -> list[dict[str, str]]:
    existing_rows = [dict(row) for row in existing]
    incoming_rows = [dict(row) for row in incoming]
    validate_rows(existing_rows, EVENT_COLUMNS, unique_key="event_id")
    validate_rows(incoming_rows, EVENT_COLUMNS)
    for row in (*existing_rows, *incoming_rows):
        if row["application_id"] not in application_ids:
            raise ValueError(
                f"event {row['event_id']!r} has unknown application_id "
                f"{row['application_id']!r}"
            )

    by_id = {row["event_id"]: row for row in existing_rows}
    for row in incoming_rows:
        by_id.setdefault(row["event_id"], row)
    merged = sorted(
        by_id.values(),
        key=lambda event: (
            event["occurred_at"],
            event["application_id"],
            event["event_type"],
            event["event_id"],
        ),
    )
    validate_rows(merged, EVENT_COLUMNS, unique_key="event_id")
    return merged


def _backfill_command(tracker_path: Path, events_path: Path) -> None:
    applications = read_csv_rows(tracker_path, TRACKER_COLUMNS)
    existing = (
        read_csv_rows(events_path, EVENT_COLUMNS)
        if events_path.exists() and events_path.stat().st_size
        else []
    )
    generated = backfill_events(applications, datetime.now(timezone.utc))
    merged = merge_events(
        existing,
        generated,
        {application["application_id"] for application in applications},
    )
    write_csv_atomic(events_path, EVENT_COLUMNS, merged)
    counts = Counter(event["event_type"] for event in merged)
    print(json.dumps({"events": len(merged), "event_counts": dict(sorted(counts.items()))}))


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill_parser = subparsers.add_parser("backfill")
    backfill_parser.add_argument("--tracker", type=Path, required=True)
    backfill_parser.add_argument("--events", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backfill":
        _backfill_command(args.tracker, args.events)


if __name__ == "__main__":
    main()
