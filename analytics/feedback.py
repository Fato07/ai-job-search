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
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
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
ACTIONS = (
    "ML/GenAI evaluation roles lead with hands-on experimentation and evaluation evidence.",
    "Every headline metric includes denominator, unit of analysis, provenance, and failure-cost interpretation.",
    "Lead-role evidence names team size, ownership boundary, decision, and outcome.",
    "Behavioral answers name a situation, action, disagreement, and result.",
    "Trade-off answers choose explicitly, state criteria, and reject an alternative.",
    "Task-specific evaluation evidence outranks public benchmarks.",
    "Logistics filters run before drafting and never reduce technical-fit calibration.",
)

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


def _has_evidence(notes: str, needles: tuple[str, ...]) -> bool:
    normalized = notes.casefold()
    return all(needle.casefold() in normalized for needle in needles)


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


def _specific_events(
    application: Mapping[str, str], created_at: str
) -> list[dict[str, str]]:
    company = application.get("company", "")
    notes = application.get("notes", "")
    role_family = application.get("role_family", "")
    events: list[dict[str, str]] = []

    def add(*, evidence_needles: tuple[str, ...], **fields: str) -> None:
        if _has_evidence(notes, evidence_needles):
            events.append(_feedback_event(application, created_at, **fields))

    if company == "CuspAI":
        add(
            evidence_needles=("come into the office regularly", "cannot support fully remote"),
            stage="application",
            source="employer_email",
            evidence_tier="explicit",
            category="logistics_work_authorization",
            signal="Regular office attendance was required and fully remote work was unavailable.",
            evidence_excerpt="CuspAI needed someone able to attend the office regularly and could not support fully remote work for this opening.",
            required_action=ACTIONS[6],
            rule_effect="activate",
            scope=_scope(geography="office-required", role_family=role_family, stage="application"),
            confidence="1.0",
        )
    elif company == "Zapier":
        add(
            evidence_needles=("legally eligible to work", "cannot sponsor work visas"),
            stage="application",
            source="employer_email",
            evidence_tier="explicit",
            category="logistics_work_authorization",
            signal="Country-of-residence work eligibility was required without visa sponsorship.",
            evidence_excerpt="Zapier required legal eligibility to work in the country of residence and could not provide visa sponsorship or immigration support.",
            required_action=ACTIONS[6],
            rule_effect="activate",
            scope=_scope(employment_model="employee", geography="country-of-residence", stage="application"),
            confidence="1.0",
        )
    elif company == "Dust":
        add(
            evidence_needles=("experience more closely matches the Solutions Engineer role",),
            stage="application",
            source="employer_email",
            evidence_tier="explicit",
            category="role_seniority_alignment",
            signal="The selected candidates had experience closer to the Solutions Engineer role.",
            evidence_excerpt="Dust was impressed by the background but moved forward with candidates whose experience more closely matched the Solutions Engineer role.",
            required_action="Target implementation-heavy Applied AI/FDE roles or add direct solutions-engineering proof.",
            rule_effect="activate",
            scope=_scope(role_family=role_family, stage="application"),
            confidence="0.95",
        )
    elif company == "Lobby":
        add(
            evidence_needles=("profile does not exactly match",),
            stage="application",
            source="recruiter_message",
            evidence_tier="explicit",
            category="role_seniority_alignment",
            signal="The profile did not exactly match the founding applied-AI role.",
            evidence_excerpt="Lobby described the AI and document-automation background as impressive but said the profile did not exactly match the opening.",
            required_action="Prefer document-AI, workflow, or evaluation-heavy applied roles unless founding-role evidence matches directly.",
            rule_effect="activate",
            scope=_scope(role_family=role_family, seniority="founding", stage="application"),
            confidence="0.95",
        )
    elif company == "Nordea":
        add(
            evidence_needles=("more extensive hands-on machine-learning", "GenAI evaluation/experimentation"),
            stage="technical",
            source="employer_email",
            evidence_tier="explicit",
            category="ml_genai_evaluation",
            signal="Hands-on machine-learning and GenAI evaluation evidence was less extensive than selected candidates'.",
            evidence_excerpt="Nordea chose candidates with more extensive hands-on machine-learning and GenAI evaluation or experimentation experience.",
            required_action=ACTIONS[0],
            rule_effect="activate",
            scope=_scope(role_family=role_family, stage="technical"),
            confidence="1.0",
        )
        add(
            evidence_needles=("headline CV metric could not be defended", "wrong denominator", "omitted provenance"),
            stage="technical",
            source="candidate_postmortem",
            evidence_tier="observed",
            category="metric_rigor_provenance",
            signal="The headline extraction metric lacked a defensible denominator, unit, and provenance.",
            evidence_excerpt="The interview repeatedly exposed that the headline extraction metric lacked a defensible denominator; the tracker later corrected the unit and provenance.",
            required_action=ACTIONS[1],
            rule_effect="activate",
            scope=_scope(role_family=role_family, stage="technical"),
            confidence="0.95",
        )
    elif company == "Wise":
        add(
            evidence_needles=("how big the team he led was", "never gave a clean"),
            stage="screen",
            source="candidate_postmortem",
            evidence_tier="observed",
            category="leadership_people_evidence",
            signal="Lead-level scope was not expressed as a clear team, ownership, decision, and outcome.",
            evidence_excerpt="Asked how big the led team was, the answer ranged across roles and never clearly stated the team size, ownership boundary, decision, and shipped outcome.",
            required_action=ACTIONS[2],
            rule_effect="activate",
            scope=_scope(role_family=role_family, seniority="lead", stage="screen"),
            confidence="0.95",
        )
        add(
            evidence_needles=("specific occasion coaching a disagreeing team member", "no person, situation or outcome"),
            stage="screen",
            source="candidate_postmortem",
            evidence_tier="observed",
            category="communication_decision_clarity",
            signal="A behavioral disagreement answer stayed theoretical instead of giving a concrete result.",
            evidence_excerpt="Asked for a specific coaching disagreement, the answer gave a theory of disagreement with no person, situation, action, or outcome.",
            required_action=ACTIONS[3],
            rule_effect="activate",
            scope=_scope(role_family=role_family, seniority="lead", stage="screen"),
            confidence="0.95",
        )
    elif company == "Dragonfly (askdragonfly.com)":
        add(
            evidence_needles=("deferred to public benchmarks", "never mentioning task-specific evals", "transformer attention mechanism"),
            stage="technical",
            source="candidate_postmortem",
            evidence_tier="observed",
            category="technical_depth",
            signal="Model selection and ML fundamentals lacked task-specific depth.",
            evidence_excerpt="The postmortem records relying on public benchmarks instead of task-specific evaluation and being unable to explain transformer attention at senior depth.",
            required_action=ACTIONS[5],
            rule_effect="activate",
            scope=_scope(role_family=role_family, seniority="senior", stage="technical"),
            confidence="0.95",
        )
        add(
            evidence_needles=("asked to CHOOSE", "instead of choosing"),
            stage="technical",
            source="candidate_postmortem",
            evidence_tier="observed",
            category="communication_decision_clarity",
            signal="A trade-off answer enumerated options without making and defending a choice.",
            evidence_excerpt="Asked to choose between event-driven glue and a programmatic pipeline, the answer enumerated more agents instead of choosing and defending one approach.",
            required_action=ACTIONS[4],
            rule_effect="activate",
            scope=_scope(role_family=role_family, seniority="senior", stage="technical"),
            confidence="0.95",
        )
    elif company == "Digital Workforce":
        add(
            evidence_needles=("no Tallinn entity", "B2B freelance arrangement", "concrete B2B collaboration model"),
            stage="screen",
            source="candidate_postmortem",
            evidence_tier="observed",
            category="logistics_work_authorization",
            signal="The Helsinki employment constraint required a concrete Tallinn B2B delivery model.",
            evidence_excerpt="The call established that there was no Tallinn entity, so the Helsinki role required a B2B arrangement and a concrete collaboration model in follow-up.",
            required_action=ACTIONS[6],
            rule_effect="activate",
            scope=_scope(employment_model="b2b", geography="Helsinki/Tallinn", stage="screen"),
            confidence="0.9",
        )
    return events


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
        specific = _specific_events(application, created_at)
        events.extend(specific)
        if "REJECTED" in application.get("status", "").upper() and not specific:
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
    applications = read_csv_rows(tracker_path, TRACKER_COLUMNS)
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
