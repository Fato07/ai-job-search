from __future__ import annotations

import argparse
import csv
import dataclasses
import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Iterable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from analytics.events import event_id, merge_events
from analytics.model import (
    EVENT_COLUMNS,
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    slugify,
    stable_application_id,
    validate_rows,
)

SCREENING_COLUMNS = (
    "discovered_at",
    "company",
    "sector",
    "role",
    "role_family",
    "role_type",
    "geography",
    "logistics_status",
    "channel",
    "screening_decision",
    "screening_reason",
    "fit_score",
    "fit_label",
    "source",
)
_VALID_DECISIONS = frozenset(("pending", "rejected", "qualified"))
_ACTIVE_STAGES = frozenset(("qualified", "submitted", "interview", "offer"))
_APPROVED_ROLE_FAMILIES = frozenset(
    ("forward_deployed", "ai_security", "ai_platform", "applied_ai")
)
_TRACKING_QUERY_KEYS = frozenset(
    ("fbclid", "gclid", "mc_cid", "mc_eid", "ref", "referrer")
)
_DEFAULT_CONFIG_PATH = Path(__file__).with_name("config.json")


@dataclass(frozen=True)
class HardGateResult:
    passed: bool
    reason: str = ""


@dataclass(frozen=True)
class IngestSummary:
    imported: int
    duplicates: int
    qualified: int
    rejected: int


def _load_config(path: Path = _DEFAULT_CONFIG_PATH) -> dict[str, object]:
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config, dict):
        raise ValueError("screening config must be a JSON object")
    return config


def _canonical_url(value: str) -> str:
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError:
        return ""
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        return ""
    if parsed.username is not None or parsed.password is not None:
        return ""

    scheme = parsed.scheme.casefold()
    hostname = parsed.hostname.casefold()
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    default_port = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    netloc = hostname if port is None or default_port else f"{hostname}:{port}"
    path = parsed.path.rstrip("/")
    query_items = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_")
        and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    query = urlencode(sorted(query_items))
    return urlunsplit((scheme, netloc, path, query, ""))


def _company_role_key(row: Mapping[str, str]) -> tuple[str, str]:
    return slugify(row.get("company", "")), slugify(row.get("role", ""))


def _is_active(application: Mapping[str, str]) -> bool:
    return application.get("stage", "").strip().casefold() in _ACTIVE_STAGES


def evaluate_hard_gates(
    candidate: Mapping[str, str],
    applications: Iterable[Mapping[str, str]],
    config: Mapping[str, object],
) -> HardGateResult:
    applications = list(applications)
    candidate_url = _canonical_url(candidate.get("source", ""))
    if candidate_url and any(
        _canonical_url(application.get("source", "")) == candidate_url
        for application in applications
    ):
        return HardGateResult(False, "hard_gate:duplicate_source_url")

    candidate_company_role = _company_role_key(candidate)
    if all(candidate_company_role) and any(
        _company_role_key(application) == candidate_company_role
        for application in applications
    ):
        return HardGateResult(False, "hard_gate:duplicate_company_role")

    if not candidate_url:
        return HardGateResult(False, "hard_gate:source_unverified")
    if candidate.get("logistics_status", "").strip().casefold() == "blocked":
        return HardGateResult(False, "hard_gate:logistics_blocked")

    strategic_override = candidate.get("screening_reason", "").startswith(
        "strategic_override:"
    )
    role_family = candidate.get("role_family", "").strip().casefold()
    if role_family not in _APPROVED_ROLE_FAMILIES and not strategic_override:
        reason = (
            "hard_gate:role_family_other"
            if role_family == "other"
            else "hard_gate:role_family_not_approved"
        )
        return HardGateResult(False, reason)

    try:
        company_cap = int(config["max_active_applications_per_company"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("config requires integer max_active_applications_per_company") from exc
    if company_cap < 1:
        raise ValueError("max_active_applications_per_company must be positive")
    company_key = slugify(candidate.get("company", ""))
    active_at_company = sum(
        _is_active(application)
        and slugify(application.get("company", "")) == company_key
        for application in applications
    )
    if active_at_company >= company_cap and not strategic_override:
        return HardGateResult(False, "hard_gate:company_active_application_cap")
    return HardGateResult(True)


def _timestamp_for_date(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid discovered_at: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid discovered_at: {value!r}")
    return f"{value}T00:00:00Z"


def _created_at(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _screening_event(
    application_id: str,
    event_type: str,
    occurred_at: str,
    detail: str,
    source_ref: str,
    created_at: str,
) -> dict[str, str]:
    return {
        "event_id": event_id(application_id, event_type, occurred_at, source_ref),
        "application_id": application_id,
        "occurred_at": occurred_at,
        "event_type": event_type,
        "source": "screening_batch",
        "detail": detail,
        "source_ref": source_ref,
        "created_at": created_at,
    }


def _tracker_row(
    candidate: Mapping[str, str], decision: str, screening_reason: str
) -> dict[str, str]:
    discovered_at = candidate["discovered_at"]
    company = candidate["company"].strip()
    role = candidate["role"].strip()
    if not company or not role:
        raise ValueError("company and role are required")
    application_id = stable_application_id(discovered_at, company, role)
    if decision == "qualified":
        stage = "qualified"
        status = f"QUALIFIED {discovered_at}"
    elif decision == "rejected":
        stage = "prospect"
        status = f"SCREENING REJECTED {discovered_at}"
    else:
        stage = "prospect"
        status = f"SCREENED {discovered_at} - PENDING"
    return {
        "application_id": application_id,
        "discovered_at": discovered_at,
        "company": company,
        "sector": candidate["sector"],
        "role": role,
        "role_family": candidate["role_family"],
        "role_type": candidate["role_type"],
        "geography": candidate["geography"],
        "logistics_status": candidate["logistics_status"],
        "channel": candidate["channel"],
        "screening_decision": decision,
        "screening_reason": screening_reason,
        "submitted_at": "",
        "stage": stage,
        "status": status,
        "status_updated_at": discovered_at,
        "contact_person": "",
        "fit_score": candidate["fit_score"],
        "fit_label": candidate["fit_label"],
        "notes": "",
        "cv_file": "",
        "cover_letter_file": "",
        "source": candidate["source"].strip(),
    }


def ingest_screening_rows(
    candidates: Iterable[Mapping[str, str]],
    applications: Iterable[Mapping[str, str]],
    events: Iterable[Mapping[str, str]],
    now: datetime,
    *,
    config: Mapping[str, object] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, str]], IngestSummary]:
    config = _load_config() if config is None else config
    updated_applications = [dict(application) for application in applications]
    existing_events = [dict(event) for event in events]
    incoming_events: list[dict[str, str]] = []
    imported = duplicates = qualified = rejected = 0
    created_at = _created_at(now)

    for index, candidate_mapping in enumerate(candidates, start=2):
        candidate = dict(candidate_mapping)
        if set(candidate) != set(SCREENING_COLUMNS):
            raise ValueError(f"screening row {index} columns differ from schema")
        decision = candidate["screening_decision"].strip().casefold()
        if decision not in _VALID_DECISIONS:
            raise ValueError(
                f"screening row {index} invalid screening_decision: "
                f"{candidate['screening_decision']!r}"
            )
        _timestamp_for_date(candidate["discovered_at"])
        if not candidate["company"].strip() or not candidate["role"].strip():
            raise ValueError(f"screening row {index} requires company and role")

        gate = evaluate_hard_gates(candidate, updated_applications, config)
        if gate.reason in {
            "hard_gate:duplicate_source_url",
            "hard_gate:duplicate_company_role",
        }:
            duplicates += 1
            continue

        screening_reason = candidate["screening_reason"]
        if decision == "qualified" and not gate.passed:
            decision = "rejected"
            screening_reason = gate.reason

        application = _tracker_row(candidate, decision, screening_reason)
        updated_applications.append(application)
        imported += 1
        if decision == "qualified":
            qualified += 1
        elif decision == "rejected":
            rejected += 1

        occurred_at = _timestamp_for_date(application["discovered_at"])
        source_ref = hash_source_ref(
            f"{application['application_id']}\x1fscreening:{candidate['source'].strip()}"
        )
        incoming_events.extend(
            (
                _screening_event(
                    application["application_id"],
                    "discovered",
                    occurred_at,
                    "Discovered in screening batch",
                    source_ref,
                    created_at,
                ),
                _screening_event(
                    application["application_id"],
                    "screened",
                    occurred_at,
                    f"Screening decision: {decision}",
                    source_ref,
                    created_at,
                ),
            )
        )
        if decision in {"qualified", "rejected"}:
            incoming_events.append(
                _screening_event(
                    application["application_id"],
                    decision,
                    occurred_at,
                    screening_reason or decision.title(),
                    source_ref,
                    created_at,
                )
            )

    application_ids = {
        application["application_id"] for application in updated_applications
    }
    merged_events = merge_events(existing_events, incoming_events, application_ids)
    return (
        updated_applications,
        merged_events,
        IngestSummary(imported, duplicates, qualified, rejected),
    )


def _read_screening_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != SCREENING_COLUMNS:
            raise ValueError(f"{path}: screening header differs from required schema")
        return [dict(row) for row in reader]


def _stage_csv(
    destination: Path,
    columns: tuple[str, ...],
    rows: Iterable[Mapping[str, object]],
) -> Path:
    materialized = [
        {column: str(row.get(column, "")) for column in columns} for row in rows
    ]
    validate_rows(materialized, columns)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staged_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            newline="",
            encoding="utf-8",
            dir=destination.parent,
            prefix=".screening-stage-",
            suffix=".csv",
            delete=False,
        ) as handle:
            staged_path = Path(handle.name)
            writer = csv.DictWriter(handle, fieldnames=columns)
            writer.writeheader()
            writer.writerows(materialized)
            handle.flush()
            os.fsync(handle.fileno())
        return staged_path
    except BaseException:
        if staged_path is not None:
            staged_path.unlink(missing_ok=True)
        raise


def _backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    with tempfile.NamedTemporaryFile(
        "wb",
        dir=path.parent,
        prefix=".screening-backup-",
        delete=False,
    ) as handle:
        backup_path = Path(handle.name)
    try:
        shutil.copyfile(path, backup_path)
        with backup_path.open("rb") as handle:
            os.fsync(handle.fileno())
        return backup_path
    except BaseException:
        backup_path.unlink(missing_ok=True)
        raise


def _journal_path(tracker_path: Path) -> Path:
    return tracker_path.parent / f".{tracker_path.name}.screening-transaction.json"


def _write_journal(path: Path, payload: Mapping[str, object]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=".screening-journal-",
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _restore_original(
    destination: Path, backup_value: object, existed: bool
) -> None:
    if not existed:
        destination.unlink(missing_ok=True)
        return
    backup = Path(str(backup_value))
    if not backup.is_file():
        raise RuntimeError(f"transaction backup missing for {destination}")
    restore_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "wb",
            dir=destination.parent,
            prefix=".screening-restore-",
            delete=False,
        ) as handle:
            restore_path = Path(handle.name)
        shutil.copyfile(backup, restore_path)
        os.replace(restore_path, destination)
    finally:
        if restore_path is not None:
            restore_path.unlink(missing_ok=True)


def _cleanup_transaction(journal_path: Path, journal: Mapping[str, object]) -> None:
    journal_path.unlink(missing_ok=True)
    for key in (
        "tracker_stage",
        "events_stage",
        "tracker_backup",
        "events_backup",
    ):
        value = journal.get(key)
        if value:
            Path(str(value)).unlink(missing_ok=True)


def _rollback_transaction(journal_path: Path, journal: Mapping[str, object]) -> None:
    _restore_original(
        Path(str(journal["tracker"])),
        journal.get("tracker_backup"),
        bool(journal["tracker_existed"]),
    )
    _restore_original(
        Path(str(journal["events"])),
        journal.get("events_backup"),
        bool(journal["events_existed"]),
    )
    _cleanup_transaction(journal_path, journal)


def _recover_transaction(tracker_path: Path, events_path: Path) -> None:
    journal_path = _journal_path(tracker_path)
    if not journal_path.exists():
        return
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot recover screening transaction: {journal_path}") from exc
    if not isinstance(journal, dict) or journal.get("version") != 1:
        raise RuntimeError(f"invalid screening transaction journal: {journal_path}")
    expected = (str(tracker_path.resolve()), str(events_path.resolve()))
    recorded = (str(journal.get("tracker")), str(journal.get("events")))
    if recorded != expected:
        raise RuntimeError(
            f"screening transaction journal destinations differ: {journal_path}"
        )
    _rollback_transaction(journal_path, journal)


def _write_ledgers_transaction(
    tracker_path: Path,
    applications: Iterable[Mapping[str, object]],
    events_path: Path,
    events: Iterable[Mapping[str, object]],
) -> None:
    tracker_rows = list(applications)
    event_rows = list(events)
    validate_rows(tracker_rows, TRACKER_COLUMNS, unique_key="application_id")
    validate_rows(event_rows, EVENT_COLUMNS, unique_key="event_id")
    tracker_stage: Path | None = None
    events_stage: Path | None = None
    tracker_backup: Path | None = None
    events_backup: Path | None = None
    journal_path = _journal_path(tracker_path)
    journal: dict[str, object] | None = None
    try:
        tracker_stage = _stage_csv(tracker_path, TRACKER_COLUMNS, tracker_rows)
        events_stage = _stage_csv(events_path, EVENT_COLUMNS, event_rows)
        tracker_backup = _backup_file(tracker_path)
        events_backup = _backup_file(events_path)
        journal = {
            "version": 1,
            "tracker": str(tracker_path.resolve()),
            "events": str(events_path.resolve()),
            "tracker_stage": str(tracker_stage),
            "events_stage": str(events_stage),
            "tracker_backup": str(tracker_backup) if tracker_backup else "",
            "events_backup": str(events_backup) if events_backup else "",
            "tracker_existed": tracker_path.exists(),
            "events_existed": events_path.exists(),
        }
        _write_journal(journal_path, journal)
        try:
            os.replace(tracker_stage, tracker_path)
            os.replace(events_stage, events_path)
        except Exception:
            _rollback_transaction(journal_path, journal)
            raise
        _cleanup_transaction(journal_path, journal)
    except BaseException:
        if journal is None:
            for path in (
                tracker_stage,
                events_stage,
                tracker_backup,
                events_backup,
            ):
                if path is not None:
                    path.unlink(missing_ok=True)
        raise


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("--tracker", type=Path, required=True)
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--config", type=Path, default=_DEFAULT_CONFIG_PATH)
    args = parser.parse_args()
    _recover_transaction(args.tracker, args.events)

    applications = read_csv_rows(args.tracker, TRACKER_COLUMNS)
    existing_events = (
        read_csv_rows(args.events, EVENT_COLUMNS)
        if args.events.exists() and args.events.stat().st_size
        else []
    )
    updated_applications, updated_events, summary = ingest_screening_rows(
        _read_screening_rows(args.input),
        applications,
        existing_events,
        datetime.now(timezone.utc),
        config=_load_config(args.config),
    )
    validate_rows(updated_applications, TRACKER_COLUMNS, unique_key="application_id")
    validate_rows(updated_events, EVENT_COLUMNS, unique_key="event_id")
    _write_ledgers_transaction(
        args.tracker, updated_applications, args.events, updated_events
    )
    print(json.dumps(dataclasses.asdict(summary), sort_keys=True))


if __name__ == "__main__":
    main()
