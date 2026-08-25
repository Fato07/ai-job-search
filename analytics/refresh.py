from __future__ import annotations

import argparse
import dataclasses
import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping

from analytics.lock import analytics_lock
from analytics.config import load_local_config
from analytics.events import merge_events, validate_event
from analytics.feedback import merge_feedback, validate_feedback
from analytics.gmail_sync import (
    ComposioClient,
    MailboxDiscovery,
    SyncProposal,
    discover_mailbox,
)
from analytics.model import (
    EVENT_COLUMNS,
    FEEDBACK_COLUMNS,
    REVIEW_COLUMNS,
    REVIEW_STATUSES,
    TRACKER_COLUMNS,
    read_csv_rows,
    read_tracker_rows,
    validate_rows,
    validate_tracker_rows,
    write_csv_atomic,
)
from analytics.rules import build_rules, validate_rules
from analytics.transaction import commit_staged_files, recover_transaction

_EMAIL = re.compile(r"(?i)\b[\w.!#$%&'*+/=?^`{|}~-]+@[\w.-]+\.[a-z]{2,}\b")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REVIEW_ID = re.compile(r"review-[0-9a-f]{64}")
_ALLOWED_TRACKER_UPDATE_FIELDS = frozenset(
    {"application_id", "stage", "status", "status_updated_at", "submitted_at"}
)
_STAGE_RANK = {"prospect": 0, "qualified": 1, "submitted": 2, "interview": 3, "closed": 4}


@dataclass(frozen=True)
class RefreshPaths:
    root: Path
    tracker: Path
    events: Path
    feedback: Path
    rules: Path
    review: Path
    checkpoint: Path
    config: Path

    @classmethod
    def for_root(cls, root: Path) -> "RefreshPaths":
        analytics = root / "analytics"
        return cls(
            root=root,
            tracker=root / "job_search_tracker.csv",
            events=analytics / "application_events.csv",
            feedback=analytics / "application_feedback.csv",
            rules=analytics / "feedback_rules.json",
            review=analytics / "reconciliation_review.csv",
            checkpoint=analytics / "gmail_checkpoint.json",
            config=analytics / "config.json",
        )

    @property
    def journal(self) -> Path:
        return self.root / ".analytics-refresh-transaction.json"

    def mutable_files(self) -> tuple[Path, ...]:
        return (
            self.tracker,
            self.events,
            self.feedback,
            self.rules,
            self.review,
            self.checkpoint,
        )


@dataclass(frozen=True)
class RefreshSummary:
    scanned: int
    matched: int
    events_added: int
    feedback_added: int
    tracker_updates: int
    review_items: int
    checkpoint: str | None


@dataclass(frozen=True)
class _RefreshState:
    tracker: tuple[Mapping[str, str], ...]
    events: tuple[Mapping[str, str], ...]
    feedback: tuple[Mapping[str, str], ...]
    rules: tuple[Mapping[str, object], ...]
    review: tuple[Mapping[str, str], ...]
    checkpoint: Mapping[str, object]


def _read_checkpoint(path: Path) -> dict[str, object]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid Gmail checkpoint") from exc
    if not isinstance(value, dict) or set(value) != {"last_successful_at"}:
        raise ValueError("Gmail checkpoint schema differs")
    timestamp = value["last_successful_at"]
    if timestamp is not None:
        _parse_timestamp("checkpoint last_successful_at", timestamp)
    return value


def _parse_timestamp(name: str, value: object) -> datetime:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc
    return parsed


def _read_rules(path: Path) -> list[dict[str, object]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("invalid feedback rules JSON") from exc
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("feedback rules must be a JSON array of objects")
    return value


def _load_state(paths: RefreshPaths) -> _RefreshState:
    missing = [str(path) for path in paths.mutable_files() if not path.is_file()]
    if missing:
        raise ValueError(f"refresh mutable files are missing: {missing}")
    return _RefreshState(
        tracker=tuple(read_tracker_rows(paths.tracker)),
        events=tuple(read_csv_rows(paths.events, EVENT_COLUMNS)),
        feedback=tuple(read_csv_rows(paths.feedback, FEEDBACK_COLUMNS)),
        rules=tuple(_read_rules(paths.rules)),
        review=tuple(read_csv_rows(paths.review, REVIEW_COLUMNS)),
        checkpoint=_read_checkpoint(paths.checkpoint),
    )


def _empty_proposal(checkpoint: Mapping[str, object]) -> SyncProposal:
    return SyncProposal((), (), (), (), dict(checkpoint))


def _apply_tracker_updates(
    rows: Iterable[Mapping[str, str]],
    updates: Iterable[Mapping[str, object]],
) -> tuple[list[dict[str, str]], int]:
    materialized = [dict(row) for row in rows]
    by_id = {row["application_id"]: row for row in materialized}
    applied = 0
    for frozen_update in updates:
        update = dict(frozen_update)
        if set(update) - _ALLOWED_TRACKER_UPDATE_FIELDS or "application_id" not in update:
            raise ValueError("tracker update contains unsupported fields")
        if any(not isinstance(value, str) for value in update.values()):
            raise ValueError("tracker update values must be strings")
        application_id = update["application_id"]
        target = by_id.get(application_id)
        if target is None:
            raise ValueError(f"tracker update has unknown application_id: {application_id!r}")
        incoming_date = update.get("status_updated_at", "")
        current_date = target.get("status_updated_at", "")
        if incoming_date and current_date:
            if incoming_date < current_date:
                continue
            if incoming_date == current_date:
                incoming_rank = _STAGE_RANK.get(update.get("stage", ""), -1)
                current_rank = _STAGE_RANK.get(target.get("stage", ""), -1)
                if incoming_rank < current_rank:
                    continue
        changed = False
        for field, value in update.items():
            if field == "application_id":
                continue
            if field == "submitted_at" and target.get(field):
                continue
            if target.get(field) != value:
                target[field] = value
                changed = True
        if changed:
            applied += 1
    validate_tracker_rows(materialized, context="refreshed tracker")
    return materialized, applied


def _validate_review(
    row: Mapping[str, str],
    application_ids: set[str],
    *,
    incoming: bool = False,
) -> None:
    if set(row) != set(REVIEW_COLUMNS):
        raise ValueError("review columns differ from schema")
    if any(not isinstance(row[column], str) for column in REVIEW_COLUMNS):
        raise ValueError("review fields must be strings")
    if not _REVIEW_ID.fullmatch(row["review_id"]):
        raise ValueError("review_id must be a stable SHA-256 identifier")
    _parse_timestamp("review occurred_at", row["occurred_at"])
    if not _SHA256.fullmatch(row["source_ref"]):
        raise ValueError("review source_ref must be a SHA-256 hash")
    if row["status"] not in REVIEW_STATUSES:
        raise ValueError(f"invalid persisted review status: {row['status']!r}")
    if incoming and row["status"] != "pending":
        raise ValueError("incoming review status must be pending")
    if any(_EMAIL.search(row[field]) for field in ("sender", "subject", "reason")):
        raise ValueError("review text contains an email address")
    if any(len(row[field]) > 280 for field in ("sender", "subject", "company", "role", "reason")):
        raise ValueError("review text exceeds privacy limit")
    try:
        candidates = json.loads(row["candidate_application_ids"])
    except json.JSONDecodeError as exc:
        raise ValueError("review candidates must be a JSON array") from exc
    if not isinstance(candidates, list) or any(
        not isinstance(value, str) or value not in application_ids for value in candidates
    ):
        raise ValueError("review candidates contain an unknown application_id")


def _merge_review(
    existing: Iterable[Mapping[str, str]],
    incoming: Iterable[Mapping[str, object]],
    application_ids: set[str],
) -> list[dict[str, str]]:
    rows = [dict(row) for row in existing]
    for row in rows:
        _validate_review(row, application_ids)
    by_id = {row["review_id"]: row for row in rows}
    by_source = {row["source_ref"]: row for row in rows}
    for candidate in incoming:
        row = {column: str(candidate.get(column, "")) for column in REVIEW_COLUMNS}
        _validate_review(row, application_ids, incoming=True)
        source_match = by_source.get(row["source_ref"])
        if source_match is not None:
            if source_match["status"] != "pending":
                continue
            index = rows.index(source_match)
            previous_id = source_match["review_id"]
            id_match = by_id.get(row["review_id"])
            if id_match is not None and id_match is not source_match:
                raise ValueError("conflicting duplicate review identity")
            rows[index] = row
            by_id.pop(previous_id, None)
            by_id[row["review_id"]] = row
            by_source[row["source_ref"]] = row
            continue
        if row["review_id"] in by_id:
            raise ValueError("conflicting duplicate review identity")
        rows.append(row)
        by_id[row["review_id"]] = row
        by_source[row["source_ref"]] = row
    return sorted(rows, key=lambda row: (row["occurred_at"], row["review_id"]))


def _validate_checkpoint(
    previous: Mapping[str, object],
    proposed: Mapping[str, object],
) -> None:
    if set(proposed) != {"last_successful_at"}:
        raise ValueError("Gmail checkpoint schema differs")
    old = previous["last_successful_at"]
    new = proposed["last_successful_at"]
    if old is None and new is None:
        return
    if old is not None and new is None:
        raise ValueError("Gmail checkpoint cannot move backwards")
    new_value = _parse_timestamp("checkpoint last_successful_at", new)
    if old is not None and new_value < _parse_timestamp("checkpoint last_successful_at", old):
        raise ValueError("Gmail checkpoint cannot move backwards")


def _build_state(current: _RefreshState, proposal: SyncProposal) -> tuple[_RefreshState, RefreshSummary]:
    tracker, tracker_updates = _apply_tracker_updates(current.tracker, proposal.tracker_updates)
    application_ids = {row["application_id"] for row in tracker}
    for event in proposal.events:
        validate_event(dict(event), application_ids)
    events = merge_events(current.events, proposal.events, application_ids)
    feedback = merge_feedback(current.feedback, proposal.feedback, application_ids)
    review = _merge_review(current.review, proposal.review_items, application_ids)
    matched_sources = {
        str(event.get("source_ref") or "")
        for event in proposal.events
        if event.get("source_ref")
    }
    review = [
        row
        for row in review
        if not (
            row["status"] == "pending"
            and row["source_ref"] in matched_sources
        )
    ]
    rules = build_rules(feedback)
    validate_rules(rules, feedback)
    _validate_checkpoint(current.checkpoint, proposal.checkpoint)
    state = _RefreshState(
        tracker=tuple(tracker),
        events=tuple(events),
        feedback=tuple(feedback),
        rules=tuple(rules),
        review=tuple(review),
        checkpoint=proposal.checkpoint,
    )
    summary = RefreshSummary(
        scanned=0,
        matched=0,
        events_added=len(events) - len(current.events),
        feedback_added=len(feedback) - len(current.feedback),
        tracker_updates=tracker_updates,
        review_items=len(review) - len(current.review),
        checkpoint=proposal.checkpoint["last_successful_at"],
    )
    return state, summary


def _validate_state(state: _RefreshState, previous_checkpoint: Mapping[str, object]) -> None:
    tracker = [dict(row) for row in state.tracker]
    validate_tracker_rows(tracker, context="staged tracker")
    application_ids = {row["application_id"] for row in tracker}
    events = [dict(row) for row in state.events]
    validate_rows(events, EVENT_COLUMNS, unique_key="event_id")
    for event in events:
        validate_event(event, application_ids)
    feedback = [dict(row) for row in state.feedback]
    validate_rows(feedback, FEEDBACK_COLUMNS, unique_key="feedback_id")
    seen_feedback_sources: set[str] = set()
    for item in feedback:
        validate_feedback(item, application_ids)
        if item["source_ref"] in seen_feedback_sources:
            raise ValueError("duplicate feedback source_ref")
        seen_feedback_sources.add(item["source_ref"])
    review = [dict(row) for row in state.review]
    validate_rows(review, REVIEW_COLUMNS, unique_key="review_id")
    for item in review:
        _validate_review(item, application_ids)
    validate_rules(state.rules, feedback)
    _validate_checkpoint(previous_checkpoint, state.checkpoint)


def _write_state(paths: RefreshPaths, state: _RefreshState) -> None:
    paths.events.parent.mkdir(parents=True, exist_ok=True)
    write_csv_atomic(paths.tracker, TRACKER_COLUMNS, state.tracker)
    write_csv_atomic(paths.events, EVENT_COLUMNS, state.events)
    write_csv_atomic(paths.feedback, FEEDBACK_COLUMNS, state.feedback)
    write_csv_atomic(paths.review, REVIEW_COLUMNS, state.review)
    paths.rules.write_text(
        json.dumps(state.rules, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    paths.checkpoint.write_text(
        json.dumps(dict(state.checkpoint), sort_keys=True) + "\n",
        encoding="utf-8",
    )


def validate_staged_files(paths: RefreshPaths, previous_checkpoint: Mapping[str, object]) -> None:
    _validate_state(_load_state(paths), previous_checkpoint)


def _refresh_locked(
    paths: RefreshPaths,
    client: ComposioClient | None,
    sync_gmail: bool,
    now: datetime,
    *,
    dry_run: bool = False,
) -> RefreshSummary:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if dry_run and paths.journal.exists():
        raise RuntimeError("dry-run transaction recovery required")
    if not dry_run:
        recover_transaction(paths.journal, paths.mutable_files())
    current = _load_state(paths)

    discovery = MailboxDiscovery(
        proposal=_empty_proposal(current.checkpoint), scanned=0, matched=0
    )
    if sync_gmail:
        config = load_local_config(paths.config, require_gmail=True)
        account_alias = str(config["gmail_account_alias"])
        expected_address = str(config["gmail_expected_address"])
        discovery = discover_mailbox(
            client if client is not None else ComposioClient(account_alias),
            current.tracker,
            current.checkpoint,
            now,
            expected_address=expected_address,
            expected_account_alias=account_alias,
            company_aliases=dict(config["company_aliases"]),
        )
    state, partial = _build_state(current, discovery.proposal)
    summary = dataclasses.replace(
        partial,
        scanned=discovery.scanned,
        matched=discovery.matched,
    )
    _validate_state(state, current.checkpoint)
    if dry_run:
        return summary

    paths.root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=paths.root, prefix=".analytics-refresh-stage-"
    ) as directory:
        staged = RefreshPaths.for_root(Path(directory))
        for source, destination in zip(paths.mutable_files(), staged.mutable_files(), strict=True):
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, destination)
        _write_state(staged, state)
        validate_staged_files(staged, current.checkpoint)
        commit_staged_files(
            paths.journal,
            dict(zip(paths.mutable_files(), staged.mutable_files(), strict=True)),
        )
    return summary
def refresh(
    paths: RefreshPaths,
    client: ComposioClient | None,
    sync_gmail: bool,
    now: datetime,
    *,
    dry_run: bool = False,
) -> RefreshSummary:
    with analytics_lock(paths.root):
        return _refresh_locked(paths, client, sync_gmail, now, dry_run=dry_run)




def _update_review_status_locked(
    paths: RefreshPaths,
    review_id: str,
    status: str,
) -> dict[str, str]:
    if status not in {"resolved", "ignored"}:
        raise ValueError("review status must be resolved or ignored")
    recover_transaction(paths.journal, paths.mutable_files())
    tracker = read_tracker_rows(paths.tracker)
    application_ids = {row["application_id"] for row in tracker}
    rows = read_csv_rows(paths.review, REVIEW_COLUMNS)
    target: dict[str, str] | None = None
    for row in rows:
        _validate_review(row, application_ids)
        if row["review_id"] == review_id:
            target = row
    if target is None:
        raise ValueError(f"unknown review_id: {review_id!r}")
    target["status"] = status
    validate_rows(rows, REVIEW_COLUMNS, unique_key="review_id")
    _validate_review(target, application_ids)
    write_csv_atomic(paths.review, REVIEW_COLUMNS, rows)
    return dict(target)
def update_review_status(
    paths: RefreshPaths,
    review_id: str,
    status: str,
) -> dict[str, str]:
    with analytics_lock(paths.root):
        return _update_review_status_locked(paths, review_id, status)




def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Atomically refresh job analytics")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--sync-gmail", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--review-id")
    parser.add_argument("--review-status", choices=("resolved", "ignored"))
    return parser


def main() -> None:
    parser = _parser()
    args = parser.parse_args()
    paths = RefreshPaths.for_root(args.root)
    if bool(args.review_id) != bool(args.review_status):
        parser.error("--review-id and --review-status must be used together")
    if args.review_id:
        if args.sync_gmail or args.dry_run:
            parser.error("review resolution cannot be combined with refresh options")
        print(json.dumps(
            update_review_status(paths, args.review_id, args.review_status),
            sort_keys=True,
        ))
        return
    summary = refresh(
        paths,
        client=None,
        sync_gmail=args.sync_gmail,
        now=datetime.now(timezone.utc),
        dry_run=args.dry_run,
    )
    output = dataclasses.asdict(summary)
    output["account"] = (
        load_local_config(paths.config, require_gmail=True)["gmail_expected_address"]
        if args.sync_gmail
        else None
    )
    print(json.dumps(output, sort_keys=True))


if __name__ == "__main__":
    main()
