from __future__ import annotations

import argparse
import csv
import json
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from analytics.config import load_local_config
from analytics.events import backfill_events, validate_event
from analytics.feedback import seed_inception_feedback, validate_feedback
from analytics.migrate import (
    LEGACY_COLUMNS, LEGACY_DEADLINE_COLUMNS, PRE_DEADLINE_TRACKER_COLUMNS,
    migrate_rows,
)
from analytics.lock import analytics_lock
from analytics.model import (
    EVENT_COLUMNS, FEEDBACK_COLUMNS, REVIEW_COLUMNS, TRACKER_COLUMNS,
    read_csv_rows, read_tracker_rows, validate_rows, validate_tracker_rows,
    write_csv_atomic,
)
from analytics.refresh import _read_checkpoint, _validate_review
from analytics.rules import build_rules, validate_rules
from analytics.transaction import commit_staged_files, recover_transaction


def _json(path: Path, expected: type) -> object:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path}") from exc
    if not isinstance(value, expected):
        raise ValueError(f"{path} has unexpected JSON shape")
    return value


def initialize(root: Path, *, now: datetime | None = None) -> dict[str, int]:
    with analytics_lock(root):
        return _initialize_locked(root, now=now)


def _initialize_locked(root: Path, *, now: datetime | None) -> dict[str, int]:
    analytics = root / "analytics"
    example = analytics / "config.example.json"
    if not example.is_file():
        raise ValueError(f"missing tracked config example: {example}")
    paths = {
        "config": analytics / "config.json",
        "tracker": root / "job_search_tracker.csv",
        "events": analytics / "application_events.csv",
        "feedback": analytics / "application_feedback.csv",
        "review": analytics / "reconciliation_review.csv",
        "checkpoint": analytics / "gmail_checkpoint.json",
        "rules": analytics / "feedback_rules.json",
    }
    journal = root / ".analytics-init-transaction.json"
    recover_transaction(journal, paths.values())
    timestamp = now or datetime.now(timezone.utc)

    if paths["tracker"].is_file():
        with paths["tracker"].open(newline="", encoding="utf-8") as handle:
            header = tuple(next(csv.reader(handle), ()))
        if header == TRACKER_COLUMNS:
            applications = read_tracker_rows(paths["tracker"])
        elif header in (
            LEGACY_COLUMNS,
            LEGACY_DEADLINE_COLUMNS,
            PRE_DEADLINE_TRACKER_COLUMNS,
        ):
            applications = migrate_rows(read_csv_rows(paths["tracker"], set()))
        else:
            raise ValueError(
                f"{paths['tracker']}: tracker header differs from recognized schemas"
            )
    else:
        applications = []
    validate_tracker_rows(applications, context=str(paths["tracker"]))
    application_ids = {row["application_id"] for row in applications}

    if paths["config"].is_file():
        config = load_local_config(paths["config"])
    else:
        config = load_local_config(example)

    if paths["events"].is_file():
        events = read_csv_rows(paths["events"], EVENT_COLUMNS)
        validate_rows(events, EVENT_COLUMNS, unique_key="event_id")
        for event in events:
            validate_event(event, application_ids)
    else:
        events = backfill_events(applications, timestamp)

    if paths["feedback"].is_file():
        feedback = read_csv_rows(paths["feedback"], FEEDBACK_COLUMNS)
        validate_rows(feedback, FEEDBACK_COLUMNS, unique_key="feedback_id")
        for item in feedback:
            validate_feedback(item, application_ids)
    else:
        feedback = seed_inception_feedback(applications, timestamp)

    if paths["review"].is_file():
        review = read_csv_rows(paths["review"], REVIEW_COLUMNS)
        validate_rows(review, REVIEW_COLUMNS, unique_key="review_id")
        for item in review:
            _validate_review(item, application_ids)
    else:
        review = []

    checkpoint = (
        _read_checkpoint(paths["checkpoint"])
        if paths["checkpoint"].is_file()
        else {"last_successful_at": None}
    )
    if paths["rules"].is_file():
        rules = _json(paths["rules"], list)
        validate_rules(rules, feedback)
    else:
        rules = build_rules(feedback)
        validate_rules(rules, feedback)

    with tempfile.TemporaryDirectory(dir=root, prefix=".analytics-init-stage-") as tmp:
        stage = Path(tmp)
        staged = {key: stage / path.name for key, path in paths.items()}
        write_csv_atomic(staged["tracker"], TRACKER_COLUMNS, applications)
        write_csv_atomic(staged["events"], EVENT_COLUMNS, events)
        write_csv_atomic(staged["feedback"], FEEDBACK_COLUMNS, feedback)
        write_csv_atomic(staged["review"], REVIEW_COLUMNS, review)
        staged["checkpoint"].write_text(json.dumps(checkpoint, sort_keys=True) + "\n", encoding="utf-8")
        staged["rules"].write_text(json.dumps(rules, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        if paths["config"].is_file():
            shutil.copyfile(paths["config"], staged["config"])
        else:
            shutil.copyfile(example, staged["config"])
        commit_staged_files(journal, {paths[key]: staged[key] for key in paths})

    return {"applications": len(applications), "events": len(events), "feedback": len(feedback), "review": len(review), "rules": len(rules)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Initialize local analytics state")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    print(json.dumps(initialize(args.root), sort_keys=True))


if __name__ == "__main__":
    main()
