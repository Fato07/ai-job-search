from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Collection, Iterable, Mapping

from analytics.model import (
    FEEDBACK_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    read_tracker_rows,
    write_csv_atomic,
)


STAGES = frozenset({"application", "screen", "technical", "onsite", "offer", "post_process"})
SOURCES = frozenset(
    {
        "employer_email",
        "recruiter_message",
        "interview_transcript",
        "candidate_postmortem",
        "tracker_backfill",
    }
)
EVIDENCE_TIERS = frozenset({"explicit", "observed", "inferred", "boilerplate"})
CATEGORIES = frozenset(
    {
        "logistics_work_authorization",
        "role_seniority_alignment",
        "technical_depth",
        "ml_genai_evaluation",
        "metric_rigor_provenance",
        "leadership_people_evidence",
        "communication_decision_clarity",
        "company_domain_evidence",
        "portfolio_open_source_proof",
        "application_quality",
        "competition_no_specific_signal",
    }
)
RULE_EFFECTS = frozenset({"activate", "monitor", "resolve"})

_SHA256 = re.compile(r"[0-9a-f]{64}")
_FEEDBACK_ID = re.compile(r"fb-[0-9a-f]{64}")


def _utc_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _date_timestamp(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"invalid ISO date: {value!r}") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"invalid ISO date: {value!r}")
    return f"{value}T00:00:00Z"


def _validate_timestamp(name: str, value: str) -> None:
    if not value.endswith("Z"):
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp")
    try:
        datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 UTC timestamp") from exc


def feedback_id(application_id: str, occurred_at: str, category: str, source_ref: str) -> str:
    material = "\x1f".join((application_id, occurred_at, category, source_ref))
    return f"fb-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _employment_model(application: Mapping[str, str]) -> str:
    role_type = application.get("role_type", "").casefold()
    if "b2b" in role_type:
        return "b2b"
    if any(marker in role_type for marker in ("contract", "freelance")):
        return "contractor"
    if any(
        marker in role_type
        for marker in ("full-time", "part-time", "employee", "employment", "permanent")
    ):
        return "employee"
    return "unknown"


def mail_feedback(
    *,
    application: Mapping[str, str],
    occurred_at: str,
    evidence_tier: str,
    category: str,
    signal: str,
    evidence_excerpt: str,
    required_action: str,
    confidence: float,
    source_ref: str,
    created_at: str,
) -> dict[str, str]:
    stage_by_category = {
        "technical_depth": "technical",
        "ml_genai_evaluation": "technical",
        "leadership_people_evidence": "onsite",
        "communication_decision_clarity": "onsite",
    }
    stage = stage_by_category.get(category, "application")
    scope_values = {
        key: application.get(key, "")
        for key in ("role_family", "geography")
        if application.get(key, "")
    }
    if category == "logistics_work_authorization":
        scope_values["employment_model"] = _employment_model(application)
    scope_values["stage"] = stage
    rule_effect = (
        "monitor"
        if evidence_tier == "boilerplate" or not required_action.strip()
        else "activate"
    )
    application_id = application.get("application_id", "")
    row = {
        "feedback_id": feedback_id(
            application_id,
            occurred_at,
            category,
            source_ref,
        ),
        "application_id": application_id,
        "occurred_at": occurred_at,
        "stage": stage,
        "source": "employer_email",
        "evidence_tier": evidence_tier,
        "category": category,
        "signal": signal,
        "evidence_excerpt": evidence_excerpt,
        "required_action": required_action,
        "rule_effect": rule_effect,
        "resolves_feedback_id": "",
        "scope": json.dumps(scope_values, sort_keys=True, separators=(",", ":")),
        "confidence": f"{confidence:.2f}",
        "source_ref": source_ref,
        "created_at": created_at,
    }
    validate_feedback(row, {application_id})
    return row


def validate_feedback(event: Mapping[str, str], application_ids: Collection[str]) -> None:
    if set(event) != set(FEEDBACK_COLUMNS):
        raise ValueError("feedback columns differ from schema")
    if any(not isinstance(event[column], str) for column in FEEDBACK_COLUMNS):
        raise ValueError("feedback fields must be strings")
    if not _FEEDBACK_ID.fullmatch(event["feedback_id"]):
        raise ValueError("feedback_id must be a stable SHA-256 identifier")
    if event["application_id"] not in application_ids:
        raise ValueError(
            f"feedback {event['feedback_id']!r} has unknown application_id "
            f"{event['application_id']!r}"
        )
    _validate_timestamp("occurred_at", event["occurred_at"])
    _validate_timestamp("created_at", event["created_at"])
    for field, allowed in (
        ("stage", STAGES),
        ("source", SOURCES),
        ("evidence_tier", EVIDENCE_TIERS),
        ("category", CATEGORIES),
        ("rule_effect", RULE_EFFECTS),
    ):
        if event[field] not in allowed:
            raise ValueError(f"invalid {field}: {event[field]!r}")
    if len(event["evidence_excerpt"]) > 280:
        raise ValueError("evidence_excerpt exceeds 280 characters")
    if not event["signal"].strip():
        raise ValueError("signal must not be empty")
    if event["rule_effect"] == "resolve":
        if not _FEEDBACK_ID.fullmatch(event["resolves_feedback_id"]):
            raise ValueError("resolve requires a non-empty resolves_feedback_id")
    elif event["resolves_feedback_id"]:
        raise ValueError("resolves_feedback_id is only valid for resolve events")
    if event["evidence_tier"] == "boilerplate":
        if event["rule_effect"] != "monitor" or event["required_action"]:
            raise ValueError("boilerplate feedback cannot become actionable")
    elif event["rule_effect"] == "activate" and not event["required_action"].strip():
        raise ValueError("activate requires a non-empty required_action")
    try:
        confidence = float(event["confidence"])
    except ValueError as exc:
        raise ValueError("confidence must be a decimal from 0.0 to 1.0") from exc
    if not math.isfinite(confidence) or not 0.0 <= confidence <= 1.0:
        raise ValueError("confidence must be a decimal from 0.0 to 1.0")
    if not _SHA256.fullmatch(event["source_ref"]):
        raise ValueError("source_ref must be a SHA-256 hash")
    expected_feedback_id = feedback_id(
        event["application_id"],
        event["occurred_at"],
        event["category"],
        event["source_ref"],
    )
    if event["feedback_id"] != expected_feedback_id:
        raise ValueError("feedback_id does not match its identity fields")
    try:
        scope = json.loads(event["scope"])
    except json.JSONDecodeError as exc:
        raise ValueError("scope must be a JSON object") from exc
    if not isinstance(scope, dict):
        raise ValueError("scope must be a JSON object")


def merge_feedback(
    existing: Iterable[Mapping[str, str]],
    incoming: Iterable[Mapping[str, str]],
    application_ids: Collection[str],
) -> list[dict[str, str]]:
    merged: list[dict[str, str]] = []
    by_id: dict[str, dict[str, str]] = {}
    by_source_ref: dict[str, dict[str, str]] = {}

    for candidate in (*existing, *incoming):
        row = dict(candidate)
        validate_feedback(row, application_ids)
        id_match = by_id.get(row["feedback_id"])
        source_match = by_source_ref.get(row["source_ref"])
        for match in (id_match, source_match):
            if match is not None and any(
                match[column] != row[column]
                for column in FEEDBACK_COLUMNS
                if column != "created_at"
            ):
                raise ValueError(
                    "conflicting duplicate feedback_id or source_ref: "
                    f"{row['feedback_id']!r} / {row['source_ref']!r}"
                )
        if id_match is not None or source_match is not None:
            continue
        by_id[row["feedback_id"]] = row
        by_source_ref[row["source_ref"]] = row
        merged.append(row)
    return merged


def _scope(**values: str) -> str:
    return json.dumps(values, sort_keys=True, separators=(",", ":"))




def _feedback_event(
    application: Mapping[str, str],
    created_at: str,
    *,
    stage: str,
    source: str,
    evidence_tier: str,
    category: str,
    signal: str,
    evidence_excerpt: str,
    required_action: str,
    rule_effect: str,
    scope: str,
    confidence: str,
) -> dict[str, str]:
    application_id = application["application_id"]
    occurred_date = application.get("status_updated_at") or application.get("discovered_at", "")
    occurred_at = _date_timestamp(occurred_date)
    source_ref = hash_source_ref(
        "\x1f".join(
            (
                "tracker_feedback",
                application_id,
                occurred_at,
                category,
                signal,
            )
        )
    )
    event = {
        "feedback_id": feedback_id(application_id, occurred_at, category, source_ref),
        "application_id": application_id,
        "occurred_at": occurred_at,
        "stage": stage,
        "source": source,
        "evidence_tier": evidence_tier,
        "category": category,
        "signal": signal,
        "evidence_excerpt": evidence_excerpt,
        "required_action": required_action,
        "rule_effect": rule_effect,
        "resolves_feedback_id": "",
        "scope": scope,
        "confidence": confidence,
        "source_ref": source_ref,
        "created_at": created_at,
    }
    return {column: event[column] for column in FEEDBACK_COLUMNS}




def _boilerplate_event(
    application: Mapping[str, str], created_at: str
) -> dict[str, str]:
    return _feedback_event(
        application,
        created_at,
        stage="application",
        source="tracker_backfill",
        evidence_tier="boilerplate",
        category="competition_no_specific_signal",
        signal="The rejection contained no candidate-specific actionable signal.",
        evidence_excerpt="The recorded rejection contains no candidate-specific, actionable feedback.",
        required_action="",
        rule_effect="monitor",
        scope=_scope(stage="application"),
        confidence="1.0",
    )


def seed_inception_feedback(
    applications: Iterable[Mapping[str, str]], now: datetime
) -> list[dict[str, str]]:
    created_at = _utc_timestamp(now)
    application_rows = [dict(application) for application in applications]
    application_ids: set[str] = set()
    events: list[dict[str, str]] = []

    for application in application_rows:
        application_id = application.get("application_id", "")
        if not application_id or application_id in application_ids:
            raise ValueError(f"invalid application_id: {application_id!r}")
        application_ids.add(application_id)
        if "rejected" in application.get("status", "").casefold():
            events.append(_boilerplate_event(application, created_at))

    events.sort(
        key=lambda event: (
            event["occurred_at"],
            event["application_id"],
            event["category"],
            event["feedback_id"],
        )
    )
    return merge_feedback([], events, application_ids)


def _backfill_command(tracker_path: Path, output_path: Path) -> None:
    applications = read_tracker_rows(tracker_path)
    existing = (
        read_csv_rows(output_path, FEEDBACK_COLUMNS)
        if output_path.exists() and output_path.stat().st_size
        else []
    )
    generated = seed_inception_feedback(applications, datetime.now(timezone.utc))
    existing_by_id = {event["feedback_id"]: event for event in existing}
    existing_by_source = {event["source_ref"]: event for event in existing}
    for event in generated:
        id_match = existing_by_id.get(event["feedback_id"])
        source_match = existing_by_source.get(event["source_ref"])
        if id_match is not None and id_match is source_match:
            event["created_at"] = id_match["created_at"]
    application_ids = {application["application_id"] for application in applications}
    merged = merge_feedback(existing, generated, application_ids)
    write_csv_atomic(output_path, FEEDBACK_COLUMNS, merged)
    rejected_ids = {
        application["application_id"]
        for application in applications
        if "REJECTED" in application["status"].upper()
    }
    covered_ids = {event["application_id"] for event in merged}
    print(
        json.dumps(
            {
                "feedback_events": len(merged),
                "evidence_tiers": dict(sorted(Counter(event["evidence_tier"] for event in merged).items())),
                "categories": dict(sorted(Counter(event["category"] for event in merged).items())),
                "rule_effects": dict(sorted(Counter(event["rule_effect"] for event in merged).items())),
                "rejected_applications": len(rejected_ids),
                "rejected_covered": len(rejected_ids & covered_ids),
            },
            sort_keys=True,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    backfill_parser = subparsers.add_parser("backfill")
    backfill_parser.add_argument("--tracker", type=Path, required=True)
    backfill_parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "backfill":
        _backfill_command(args.tracker, args.output)


if __name__ == "__main__":
    main()
