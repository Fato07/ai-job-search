from __future__ import annotations

import argparse
import json
import math
import os
import statistics
import tempfile
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_DATA_MARKER = "__DASHBOARD_DATA__"
_DEFAULT_TEMPLATE = (
    "<!doctype html><html><head><meta charset=\"utf-8\"><title>Job analytics</title>"
    "</head><body><script>window.__JOB_ANALYTICS__=__DASHBOARD_DATA__;</script>"
    "</body></html>"
)
_EVENT_TYPES = (
    "discovered", "screened", "qualified", "submitted", "responded",
    "interviewed", "offered",
)
_RESPONSE_EVENTS = frozenset({"responded", "rejected", "interview", "offer"})
_DECISION_EVENTS = frozenset({"rejected", "withdrawn", "offer"})
_ACTIVE_STAGES = frozenset({
    "prospect", "qualified", "drafting", "ready", "submitted", "response",
    "interview", "offer",
})
_KNOWN_STAGES = _ACTIVE_STAGES | {"closed"}
_NEXT_ACTION = {
    "prospect": "Screen opportunity",
    "qualified": "Draft application",
    "drafting": "Complete and review",
    "ready": "Submit application",
    "submitted": "Follow up",
    "response": "Review response",
    "interview": "Prepare for interview",
    "offer": "Evaluate offer",
    "closed": "None",
}
_CALIBRATION_DIMENSIONS = (
    "fit_band", "role_family", "geography", "channel", "logistics_status",
    "seniority",
)


def _stable_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _text(value: object) -> str:
    return "" if value is None else str(value).strip()


def _parse_timestamp(value: object) -> datetime | None:
    text = _text(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text)
        except ValueError:
            return None
        return datetime.combine(parsed_date, time.min, timezone.utc)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _reporting_zone(config: Mapping[str, object]) -> tuple[str, ZoneInfo]:
    name = _text(config.get("reporting_timezone")) or "Europe/Tallinn"
    try:
        return name, ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"invalid reporting_timezone: {name!r}") from exc


def _operational_date(timestamp: datetime, reporting_zone: ZoneInfo) -> date:
    return timestamp.astimezone(reporting_zone).date()


def _event_label(row: Mapping[str, object], fallback: str) -> str:
    return _text(row.get("event_id")) or fallback


def _feedback_label(row: Mapping[str, object], fallback: str) -> str:
    return _text(row.get("feedback_id")) or fallback


def _normalize_applications(
    rows: Sequence[Mapping[str, object]],
) -> tuple[dict[str, dict[str, object]], list[str], list[int], list[str]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    orphan_rows: list[int] = []
    for index, frozen_row in enumerate(rows, start=1):
        row = dict(frozen_row)
        application_id = _text(row.get("application_id"))
        if not application_id:
            orphan_rows.append(index)
            continue
        grouped[application_id].append(row)

    applications: dict[str, dict[str, object]] = {}
    duplicate_ids: list[str] = []
    for application_id, candidates in grouped.items():
        if len(candidates) > 1:
            duplicate_ids.append(application_id)
        applications[application_id] = min(candidates, key=_stable_json)

    duplicate_roles_or_sources: set[str] = set()
    by_role: dict[tuple[str, str], set[str]] = defaultdict(set)
    by_source: dict[str, set[str]] = defaultdict(set)
    for application_id, row in applications.items():
        company = _text(row.get("company")).casefold()
        role = _text(row.get("role")).casefold()
        if company and role:
            by_role[(company, role)].add(application_id)
        source = _text(row.get("source"))
        if source:
            by_source[source].add(application_id)
    for (company, role), application_ids in by_role.items():
        if len(application_ids) > 1:
            duplicate_roles_or_sources.add(f"role:{company}|{role}")
    for source, application_ids in by_source.items():
        if len(application_ids) > 1:
            duplicate_roles_or_sources.add(f"source:{source}")

    return (
        applications,
        sorted(duplicate_ids),
        orphan_rows,
        sorted(duplicate_roles_or_sources),
    )


def _normalize_events(
    rows: Sequence[Mapping[str, object]],
    application_ids: set[str],
    today: date,
    reporting_zone: ZoneInfo,
) -> tuple[
    list[tuple[dict[str, object], datetime]], list[str], list[str], list[str], list[str]
]:
    chosen: dict[tuple[object, ...], tuple[dict[str, object], datetime]] = {}
    duplicate_labels: set[str] = set()
    orphan_labels: list[str] = []
    invalid_timestamp_labels: list[str] = []
    future_labels: list[str] = []

    for index, frozen_row in enumerate(rows, start=1):
        row = dict(frozen_row)
        label = _event_label(row, f"row-{index}")
        application_id = _text(row.get("application_id"))
        if application_id not in application_ids:
            orphan_labels.append(label)
            continue
        occurred_at = _parse_timestamp(row.get("occurred_at"))
        if occurred_at is None:
            invalid_timestamp_labels.append(label)
            continue
        if _operational_date(occurred_at, reporting_zone) > today:
            future_labels.append(label)
            continue
        event_id = _text(row.get("event_id"))
        identity: tuple[object, ...]
        if event_id:
            identity = ("event_id", event_id)
        else:
            identity = (
                "semantic",
                application_id,
                _text(row.get("event_type")).casefold(),
                occurred_at.isoformat(),
                _text(row.get("source_ref")),
                _text(row.get("detail")),
            )
        candidate = (row, occurred_at)
        if identity in chosen:
            duplicate_labels.add(event_id or label)
            if _stable_json(row) < _stable_json(chosen[identity][0]):
                chosen[identity] = candidate
        else:
            chosen[identity] = candidate

    events = sorted(
        chosen.values(),
        key=lambda item: (
            item[1],
            _text(item[0].get("application_id")),
            _text(item[0].get("event_type")),
            _text(item[0].get("event_id")),
        ),
    )
    return (
        events,
        sorted(duplicate_labels),
        sorted(orphan_labels),
        sorted(invalid_timestamp_labels),
        sorted(future_labels),
    )


def _normalize_feedback(
    rows: Sequence[Mapping[str, object]], application_ids: set[str]
) -> tuple[list[dict[str, object]], list[str], list[str]]:
    chosen: dict[tuple[str, str], dict[str, object]] = {}
    duplicate_labels: set[str] = set()
    orphan_labels: list[str] = []
    for index, frozen_row in enumerate(rows, start=1):
        row = dict(frozen_row)
        label = _feedback_label(row, f"row-{index}")
        application_id = _text(row.get("application_id"))
        if application_id not in application_ids:
            orphan_labels.append(label)
            continue
        feedback_id = _text(row.get("feedback_id"))
        identity = ("feedback_id", feedback_id) if feedback_id else ("row", _stable_json(row))
        if identity in chosen:
            duplicate_labels.add(feedback_id or label)
            if _stable_json(row) < _stable_json(chosen[identity]):
                chosen[identity] = row
        else:
            chosen[identity] = row
    feedback = sorted(
        chosen.values(),
        key=lambda row: (
            _text(row.get("occurred_at")),
            _text(row.get("feedback_id")),
            _stable_json(row),
        ),
    )
    return feedback, sorted(duplicate_labels), sorted(orphan_labels)


def _events_by_application(
    events: Iterable[tuple[dict[str, object], datetime]],
) -> dict[str, list[tuple[dict[str, object], datetime]]]:
    grouped: dict[str, list[tuple[dict[str, object], datetime]]] = defaultdict(list)
    for row, occurred_at in events:
        grouped[_text(row.get("application_id"))].append((row, occurred_at))
    return grouped


def _first_event(
    events: Sequence[tuple[dict[str, object], datetime]], event_types: set[str] | frozenset[str]
) -> datetime | None:
    matches = [
        occurred_at
        for row, occurred_at in events
        if _text(row.get("event_type")).casefold() in event_types
    ]
    return min(matches) if matches else None


def _ids_with_event(
    events: Iterable[tuple[dict[str, object], datetime]], event_types: set[str] | frozenset[str]
) -> set[str]:
    return {
        _text(row.get("application_id"))
        for row, _ in events
        if _text(row.get("event_type")).casefold() in event_types
    }


def _rate(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def _median_hours(values: Sequence[float]) -> float | None:
    return float(statistics.median(values)) if values else None


def _fit_score(value: object) -> float | None:
    try:
        score = float(_text(value))
    except ValueError:
        return None
    if not math.isfinite(score) or not 0.0 <= score <= 100.0:
        return None
    return score


def _fit_band(value: object) -> str:
    score = _fit_score(value)
    if score is None:
        return "missing"
    if score >= 90:
        return "90-100"
    if score >= 80:
        return "80-89"
    if score >= 70:
        return "70-79"
    if score >= 60:
        return "60-69"
    return "0-59"


def _event_sets(
    events: Sequence[tuple[dict[str, object], datetime]],
) -> dict[str, set[str]]:
    return {
        "discovered": _ids_with_event(events, {"discovered"}),
        "screened": _ids_with_event(events, {"screened"}),
        "qualified": _ids_with_event(events, {"qualified"}),
        "submitted": _ids_with_event(events, {"submitted"}),
        "responded": _ids_with_event(events, _RESPONSE_EVENTS),
        "interviewed": _ids_with_event(events, {"interview"}),
        "offered": _ids_with_event(events, {"offer"}),
    }

def _chronological_outcome_times(
    grouped_events: Mapping[str, Sequence[tuple[dict[str, object], datetime]]],
) -> dict[str, dict[str, tuple[datetime, ...]]]:
    collected: dict[str, dict[str, list[datetime]]] = {
        "responded": defaultdict(list),
        "interviewed": defaultdict(list),
        "offered": defaultdict(list),
        "decided": defaultdict(list),
    }
    for application_id, application_events in grouped_events.items():
        submitted_at = _first_event(application_events, {"submitted"})
        if submitted_at is None:
            continue
        for row, occurred_at in application_events:
            if occurred_at < submitted_at:
                continue
            event_type = _text(row.get("event_type")).casefold()
            if event_type in _RESPONSE_EVENTS:
                collected["responded"][application_id].append(occurred_at)
            if event_type == "interview":
                collected["interviewed"][application_id].append(occurred_at)
            if event_type == "offer":
                collected["offered"][application_id].append(occurred_at)
            if event_type in _DECISION_EVENTS:
                collected["decided"][application_id].append(occurred_at)
    return {
        outcome: {
            application_id: tuple(sorted(timestamps))
            for application_id, timestamps in sorted(by_application.items())
        }
        for outcome, by_application in collected.items()
    }

def _lifecycle_outcomes(
    events: Sequence[tuple[dict[str, object], datetime]],
    outcome_times: Mapping[str, Mapping[str, Sequence[datetime]]],
) -> dict[str, set[str]]:
    outcomes = _event_sets(events)
    for outcome in ("responded", "interviewed", "offered"):
        outcomes[outcome] = set(outcome_times[outcome])
    return outcomes


def _series_row(
    day: date,
    events: Sequence[tuple[dict[str, object], datetime]],
    outcome_times: Mapping[str, Mapping[str, Sequence[datetime]]],
    reporting_zone: ZoneInfo,
) -> dict[str, object]:
    on_day = [
        (row, timestamp)
        for row, timestamp in events
        if _operational_date(timestamp, reporting_zone) == day
    ]
    sets = _event_sets(on_day)
    for outcome in ("responded", "interviewed", "offered"):
        sets[outcome] = {
            application_id
            for application_id, timestamps in outcome_times[outcome].items()
            if any(_operational_date(timestamp, reporting_zone) == day for timestamp in timestamps)
        }
    return {
        "date": day.isoformat(),
        **{name: len(sets[name]) for name in _EVENT_TYPES},
        "submitted_application_ids": sorted(sets["submitted"]),
    }


def _daily_series(
    events: Sequence[tuple[dict[str, object], datetime]],
    outcome_times: Mapping[str, Mapping[str, Sequence[datetime]]],
    today: date,
    reporting_zone: ZoneInfo,
) -> list[dict[str, object]]:
    start = min(
        (_operational_date(timestamp, reporting_zone) for _, timestamp in events),
        default=today,
    )
    return [
        _series_row(
            start + timedelta(days=offset),
            events,
            outcome_times,
            reporting_zone,
        )
        for offset in range((today - start).days + 1)
    ]


def _conversion_row(
    value_key: str,
    value: str,
    application_ids: set[str],
    outcomes: Mapping[str, set[str]],
) -> dict[str, object]:
    submitted = application_ids & outcomes["submitted"]
    row: dict[str, object] = {
        value_key: value,
        "applications": len(application_ids),
        "submitted": len(submitted),
        "responded": len(application_ids & outcomes["responded"]),
        "interviewed": len(application_ids & outcomes["interviewed"]),
        "offered": len(application_ids & outcomes["offered"]),
        "insufficient_sample": len(submitted) < 5,
    }
    if len(submitted) >= 5:
        row["conversion_rates"] = {
            "response": _rate(len(submitted & outcomes["responded"]), len(submitted)),
            "interview": _rate(len(submitted & outcomes["interviewed"]), len(submitted)),
            "offer": _rate(len(submitted & outcomes["offered"]), len(submitted)),
        }
    return row


def _weekly_cohorts(
    grouped_events: Mapping[str, Sequence[tuple[dict[str, object], datetime]]],
    outcomes: Mapping[str, set[str]],
    reporting_zone: ZoneInfo,
) -> list[dict[str, object]]:
    cohorts: dict[str, set[str]] = defaultdict(set)
    for application_id, events in grouped_events.items():
        submitted_at = _first_event(events, {"submitted"})
        if submitted_at is None:
            continue
        submitted_date = _operational_date(submitted_at, reporting_zone)
        monday = submitted_date - timedelta(days=submitted_date.weekday())
        cohorts[monday.isoformat()].add(application_id)
    return [
        _conversion_row("week_start", week_start, application_ids, outcomes)
        for week_start, application_ids in sorted(cohorts.items())
    ]


def _response_metrics(
    grouped_events: Mapping[str, Sequence[tuple[dict[str, object], datetime]]],
    outcomes: Mapping[str, set[str]],
    outcome_times: Mapping[str, Mapping[str, Sequence[datetime]]],
) -> dict[str, object]:
    response_hours: list[float] = []
    decision_hours: list[float] = []
    open_applications = 0
    for application_id in sorted(outcomes["submitted"]):
        submitted_at = _first_event(grouped_events.get(application_id, ()), {"submitted"})
        if submitted_at is None:
            continue
        response_at = min(outcome_times["responded"].get(application_id, ()), default=None)
        decision_at = min(outcome_times["decided"].get(application_id, ()), default=None)
        if response_at is not None:
            response_hours.append((response_at - submitted_at).total_seconds() / 3600)
        if decision_at is not None:
            decision_hours.append((decision_at - submitted_at).total_seconds() / 3600)
        else:
            open_applications += 1

    submitted = len(outcomes["submitted"])
    responded = len(outcomes["responded"] & outcomes["submitted"])
    interviewed = len(outcomes["interviewed"] & outcomes["submitted"])
    offered = len(outcomes["offered"] & outcomes["submitted"])
    return {
        "submitted": submitted,
        "responded": responded,
        "interviewed": interviewed,
        "offered": offered,
        "response_rate": _rate(responded, submitted),
        "interview_rate": _rate(interviewed, submitted),
        "offer_rate": _rate(offered, submitted),
        "median_time_to_response_hours": _median_hours(response_hours),
        "median_time_to_decision_hours": _median_hours(decision_hours),
        "open_applications": open_applications,
    }


def _calibration(
    applications: Mapping[str, Mapping[str, object]],
    outcomes: Mapping[str, set[str]],
) -> dict[str, list[dict[str, object]]]:
    result: dict[str, list[dict[str, object]]] = {}
    for dimension in _CALIBRATION_DIMENSIONS:
        segments: dict[str, set[str]] = defaultdict(set)
        for application_id, row in applications.items():
            value = _fit_band(row.get("fit_score")) if dimension == "fit_band" else _text(row.get(dimension)) or "unknown"
            segments[value].add(application_id)
        result[dimension] = [
            _conversion_row("value", value, application_ids, outcomes)
            for value, application_ids in sorted(segments.items())
        ]
    return result


def _feedback_snapshot(
    feedback: Sequence[Mapping[str, object]],
    rules: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    category_counts = Counter(_text(row.get("category")) or "unknown" for row in feedback)
    evidence_counts = Counter(_text(row.get("evidence_tier")) or "unknown" for row in feedback)
    feedback_by_id = {
        _text(row.get("feedback_id")): row
        for row in feedback
        if _text(row.get("feedback_id"))
    }
    category_application_ids: dict[str, set[str]] = defaultdict(set)
    for row in feedback:
        category = _text(row.get("category")) or "unknown"
        application_id = _text(row.get("application_id"))
        if application_id:
            category_application_ids[category].add(application_id)

    def confidence(value: object) -> float | None:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return parsed if math.isfinite(parsed) else None

    def source_feedback(row: Mapping[str, object]) -> dict[str, object]:
        return {
            "feedback_id": _text(row.get("feedback_id")),
            "application_id": _text(row.get("application_id")),
            "category": _text(row.get("category")) or "unknown",
            "evidence_tier": _text(row.get("evidence_tier")) or "unknown",
            "evidence_excerpt": _text(row.get("evidence_excerpt")),
            "required_action": _text(row.get("required_action")),
            "confidence": confidence(row.get("confidence")),
        }

    status_counts = {"active": 0, "monitor": 0, "resolved": 0}
    lineage: list[dict[str, object]] = []
    for rule in sorted(rules, key=lambda row: (_text(row.get("rule_id")), _stable_json(row))):
        status = _text(rule.get("status")).casefold()
        if status == "monitoring":
            status = "monitor"
        if status in status_counts:
            status_counts[status] += 1
        source_ids_value = rule.get("source_feedback_ids", ())
        source_ids = sorted(
            {
                _text(value)
                for value in source_ids_value
                if _text(value) in feedback_by_id
            }
        ) if isinstance(source_ids_value, (list, tuple, set)) else []
        sources = [source_feedback(feedback_by_id[feedback_id]) for feedback_id in source_ids]
        application_ids = sorted({
            _text(row.get("application_id"))
            for row in sources
            if _text(row.get("application_id"))
        })
        evidence_tiers_value = rule.get("evidence_tiers", ())
        evidence_tiers = sorted({
            _text(value)
            for value in evidence_tiers_value
            if _text(value)
        }) if isinstance(evidence_tiers_value, (list, tuple, set)) else []
        if not evidence_tiers:
            evidence_tiers = sorted({
                _text(row.get("evidence_tier"))
                for row in sources
                if _text(row.get("evidence_tier"))
            })
        lineage.append({
            "rule_id": _text(rule.get("rule_id")),
            "category": _text(rule.get("category")) or "unknown",
            "status": status or "unknown",
            "required_action": _text(rule.get("required_action")),
            "confidence": confidence(rule.get("confidence")),
            "evidence_tiers": evidence_tiers,
            "feedback_ids": source_ids,
            "application_ids": application_ids,
            "source_feedback": sources,
        })
    return {
        "category_counts": dict(sorted(category_counts.items())),
        "category_application_ids": {
            category: sorted(ids)
            for category, ids in sorted(category_application_ids.items())
        },
        "evidence_tier_counts": dict(sorted(evidence_counts.items())),
        "rule_status_counts": status_counts,
        "lineage": lineage,
    }


def _latest_feedback(
    application_id: str, feedback: Sequence[Mapping[str, object]]
) -> Mapping[str, object] | None:
    candidates = [row for row in feedback if _text(row.get("application_id")) == application_id]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda row: (
            _parse_timestamp(row.get("occurred_at")) or datetime.min.replace(tzinfo=timezone.utc),
            _text(row.get("feedback_id")),
        ),
    )


def _pipeline(
    applications: Mapping[str, Mapping[str, object]],
    grouped_events: Mapping[str, Sequence[tuple[dict[str, object], datetime]]],
    feedback: Sequence[Mapping[str, object]],
    today: date,
    reporting_zone: ZoneInfo,
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for application_id, application in sorted(applications.items()):
        events = grouped_events.get(application_id, ())
        submitted_at = _first_event(events, {"submitted"})
        latest_at = max((occurred_at for _, occurred_at in events), default=None)
        latest_feedback = _latest_feedback(application_id, feedback)
        stage = _text(application.get("stage")).casefold()
        score = _fit_score(application.get("fit_score"))
        rows.append({
            "application_id": application_id,
            "company": _text(application.get("company")),
            "role": _text(application.get("role")),
            "application_date": (
                _operational_date(submitted_at, reporting_zone).isoformat()
                if submitted_at else None
            ),
            "stage": stage or "unknown",
            "status": _text(application.get("status")),
            "age_days": (
                (today - _operational_date(latest_at, reporting_zone)).days
                if latest_at else None
            ),
            "role_family": _text(application.get("role_family")) or "unknown",
            "geography": _text(application.get("geography")) or "unknown",
            "channel": _text(application.get("channel")) or "unknown",
            "logistics_status": _text(application.get("logistics_status")) or "unknown",
            "seniority": _text(application.get("seniority")) or "unknown",
            "fit_score": score,
            "fit_band": _fit_band(application.get("fit_score")),
            "latest_feedback_signal": _text(latest_feedback.get("signal")) if latest_feedback else "",
            "feedback_evidence_tier": _text(latest_feedback.get("evidence_tier")) if latest_feedback else "",
            "next_action": _NEXT_ACTION.get(stage, "Review status"),
            "source": _text(application.get("source")),
        })
    return rows


def _review_queue(
    review_items: Sequence[Mapping[str, object]],
    application_ids: set[str],
) -> dict[str, object]:
    pending = [
        dict(row)
        for row in review_items
        if _text(row.get("status")).casefold() in {"", "pending", "open"}
    ]
    pending.sort(key=lambda row: (_text(row.get("occurred_at")), _text(row.get("review_id")), _stable_json(row)))
    items: list[dict[str, object]] = []
    for row in pending:
        raw_candidates = row.get("candidate_application_ids", "")
        try:
            candidates = json.loads(raw_candidates) if isinstance(raw_candidates, str) else raw_candidates
        except json.JSONDecodeError as exc:
            raise ValueError(
                "review candidate_application_ids must be a JSON array"
            ) from exc
        if not isinstance(candidates, list) or any(
            not isinstance(application_id, str)
            or application_id not in application_ids
            for application_id in candidates
        ):
            raise ValueError(
                "review candidate_application_ids must contain known application IDs"
            )
        deduplicated_candidates = list(dict.fromkeys(candidates))
        items.append({
            "review_id": _text(row.get("review_id")),
            "candidate_application_ids": deduplicated_candidates,
            "occurred_at": _text(row.get("occurred_at")) or None,
            "company": _text(row.get("company")),
            "role": _text(row.get("role")),
            "reason": _text(row.get("reason")),
        })
    return {"count": len(items), "items": items}


def _filters(
    pipeline: Sequence[Mapping[str, object]],
    feedback: Sequence[Mapping[str, object]],
    data_start: str | None,
    data_end: str | None,
) -> dict[str, object]:
    def values(field: str) -> list[str]:
        return sorted({_text(row.get(field)) for row in pipeline if _text(row.get(field))})

    return {
        "date_range": {"start": data_start, "end": data_end},
        "role_family": values("role_family"),
        "geography": values("geography"),
        "channel": values("channel"),
        "logistics_status": values("logistics_status"),
        "seniority": values("seniority"),
        "stage": values("stage"),
        "fit_band": values("fit_band"),
        "evidence_tier": sorted({_text(row.get("evidence_tier")) for row in feedback if _text(row.get("evidence_tier"))}),
        "feedback_category": sorted({_text(row.get("category")) for row in feedback if _text(row.get("category"))}),
    }


def build_snapshot(
    applications: Iterable[Mapping[str, object]],
    events: Iterable[Mapping[str, object]],
    feedback: Iterable[Mapping[str, object]],
    rules: Iterable[Mapping[str, object]],
    review_items: Iterable[Mapping[str, object]],
    config: Mapping[str, object],
    today: date,
) -> dict[str, object]:
    """Build one deterministic, as-of-date analytics snapshot."""
    if not isinstance(today, date) or isinstance(today, datetime):
        raise TypeError("today must be a date")
    reporting_timezone, reporting_zone = _reporting_zone(config)

    application_rows = [dict(row) for row in applications]
    event_rows = [dict(row) for row in events]
    feedback_rows = [dict(row) for row in feedback]
    rule_rows = [dict(row) for row in rules]
    review_rows = [dict(row) for row in review_items]

    normalized_apps, duplicate_app_ids, orphan_app_rows, duplicate_roles = _normalize_applications(application_rows)
    application_ids = set(normalized_apps)
    (
        normalized_events,
        duplicate_events,
        orphan_events,
        invalid_event_timestamps,
        future_events,
    ) = _normalize_events(event_rows, application_ids, today, reporting_zone)
    normalized_feedback, duplicate_feedback, orphan_feedback = _normalize_feedback(feedback_rows, application_ids)
    grouped_events = _events_by_application(normalized_events)
    outcome_times = _chronological_outcome_times(grouped_events)
    outcomes = _lifecycle_outcomes(normalized_events, outcome_times)

    missing_scores = sorted(
        application_id
        for application_id, row in normalized_apps.items()
        if _fit_score(row.get("fit_score")) is None
    )
    missing_dates = sorted(
        application_id
        for application_id in application_ids
        if _first_event(grouped_events.get(application_id, ()), {"discovered"}) is None
    )
    ambiguous_statuses = sorted(
        application_id
        for application_id, row in normalized_apps.items()
        if _text(row.get("stage")).casefold() not in _KNOWN_STAGES
    )
    try:
        stale_after_days = int(config.get("stale_after_days", 14))
    except (TypeError, ValueError):
        stale_after_days = 14
    if stale_after_days < 0:
        stale_after_days = 14
    stale_rows: list[str] = []
    for application_id, row in normalized_apps.items():
        stage = _text(row.get("stage")).casefold()
        app_events = grouped_events.get(application_id, ())
        latest_at = max((occurred_at for _, occurred_at in app_events), default=None)
        terminal = _first_event(app_events, {"rejected", "withdrawn"}) is not None
        if stage != "closed" and not terminal and latest_at is not None:
            if (
                today - _operational_date(latest_at, reporting_zone)
            ).days > stale_after_days:
                stale_rows.append(application_id)
    stale_rows.sort()

    review_queue = _review_queue(review_rows, application_ids)
    missing_early_events = sorted(
        event_type
        for event_type in ("screened", "qualified")
        if outcomes["submitted"] and not outcomes[event_type]
    )
    lifecycle_coverage = {
        "missing_event_types": missing_early_events,
        "early_funnel_conversion_available": not missing_early_events,
        "insufficient": bool(missing_early_events),
    }
    def linked_application_ids(
        rows: Sequence[Mapping[str, object]],
        label_field: str,
        labels: Sequence[str],
    ) -> list[str]:
        selected = set(labels)
        return sorted({
            _text(row.get("application_id"))
            for row in rows
            if _text(row.get(label_field)) in selected
            and _text(row.get("application_id")) in application_ids
        })

    quality_application_ids = {
        "missing_scores": missing_scores,
        "missing_dates": missing_dates,
        "ambiguous_statuses": ambiguous_statuses,
        "stale_rows": stale_rows,
        "duplicate_applications": duplicate_app_ids,
        "duplicate_events": linked_application_ids(event_rows, "event_id", duplicate_events),
        "duplicate_feedback": linked_application_ids(feedback_rows, "feedback_id", duplicate_feedback),
        "orphaned_events": linked_application_ids(event_rows, "event_id", orphan_events),
        "orphaned_feedback": linked_application_ids(feedback_rows, "feedback_id", orphan_feedback),
        "invalid_event_timestamps": linked_application_ids(
            event_rows, "event_id", invalid_event_timestamps
        ),
        "future_events": linked_application_ids(event_rows, "event_id", future_events),
        "review_queue": sorted({
            application_id
            for row in review_queue["items"]
            for application_id in row["candidate_application_ids"]
        }),
    }
    data_quality = {
        "missing_scores": missing_scores,
        "missing_dates": missing_dates,
        "ambiguous_statuses": ambiguous_statuses,
        "duplicates": {
            "application_ids": duplicate_app_ids,
            "roles_or_sources": duplicate_roles,
            "events": duplicate_events,
            "feedback": duplicate_feedback,
        },
        "orphaned_application_rows": orphan_app_rows,
        "orphaned_events": orphan_events,
        "orphaned_feedback": orphan_feedback,
        "invalid_event_timestamps": invalid_event_timestamps,
        "future_events": future_events,
        "stale_rows": stale_rows,
        "review_queue": review_queue,
        "lifecycle_coverage": lifecycle_coverage,
        "application_ids": quality_application_ids,
    }

    dates = [
        _operational_date(occurred_at, reporting_zone)
        for _, occurred_at in normalized_events
    ]
    data_start = min(dates).isoformat() if dates else None
    data_end = max(dates).isoformat() if dates else None
    warnings = sorted(
        key
        for key, value in data_quality.items()
        if (isinstance(value, list) and value)
        or (key == "duplicates" and any(value.values()))
        or (key == "review_queue" and value["count"])
        or (key == "lifecycle_coverage" and value["insufficient"])
    )

    today_events = [
        (row, timestamp)
        for row, timestamp in normalized_events
        if _operational_date(timestamp, reporting_zone) == today
    ]
    today_sets = _event_sets(today_events)
    screened_today = today_sets["screened"]
    explicit_gate_rejections = _ids_with_event(today_events, {"rejected_by_gate", "gate_rejected"})
    screened_gate_rejections = {
        application_id
        for application_id in screened_today
        if _text(normalized_apps[application_id].get("screening_decision")).casefold() == "rejected"
    }
    gate_rejections_today = explicit_gate_rejections | screened_gate_rejections
    follow_ups_today = _ids_with_event(today_events, {"follow_up"})
    pipeline = _pipeline(
        normalized_apps,
        grouped_events,
        normalized_feedback,
        today,
        reporting_zone,
    )
    feedback_snapshot = _feedback_snapshot(normalized_feedback, rule_rows)

    snapshot = {
        "meta": {
            "generated_at": f"{today.isoformat()}T00:00:00Z",
            "reporting_timezone": reporting_timezone,
            "data_range": {"start": data_start, "end": data_end},
            "record_counts": {
                "applications": len(application_ids),
                "application_rows": len(application_rows),
                "events": len(normalized_events),
                "event_rows": len(event_rows),
                "feedback": len(normalized_feedback),
                "feedback_rows": len(feedback_rows),
                "rules": len(rule_rows),
                "review_items": len(review_rows),
            },
            "warnings": warnings,
        },
        "today": {
            "screening_target": int(config.get("daily_screening_target", 100)),
            "submission_soft_capacity": int(config.get("daily_submission_soft_capacity", 20)),
            "screened": len(screened_today),
            "rejected_by_gate": len(gate_rejections_today),
            "qualified": sum(
                _text(row.get("stage")).casefold() == "qualified"
                for row in normalized_apps.values()
            ),
            "drafting": sum(
                _text(row.get("stage")).casefold() == "drafting"
                for row in normalized_apps.values()
            ),
            "ready": sum(
                _text(row.get("stage")).casefold() == "ready"
                for row in normalized_apps.values()
            ),
            "submitted": len(today_sets["submitted"]),
            "follow_ups": len(follow_ups_today),
            "stale": len(stale_rows),
            "review_queue": review_queue["count"],
            "application_ids": {
                "screened": sorted(screened_today),
                "rejected_by_gate": sorted(gate_rejections_today),
                "submitted": sorted(today_sets["submitted"]),
                "follow_ups": sorted(follow_ups_today),
            },
        },
        "funnel": {name: len(outcomes[name]) for name in _EVENT_TYPES},
        "lifecycle_application_ids": {
            name: sorted(outcomes[name])
            for name in _EVENT_TYPES
        },
        "daily_series": _daily_series(
            normalized_events,
            outcome_times,
            today,
            reporting_zone,
        ),
        "weekly_cohorts": _weekly_cohorts(
            grouped_events,
            outcomes,
            reporting_zone,
        ),
        "response_metrics": _response_metrics(
            grouped_events,
            outcomes,
            outcome_times,
        ),
        "calibration": _calibration(normalized_apps, outcomes),
        "feedback": feedback_snapshot,
        "pipeline": pipeline,
        "data_quality": data_quality,
        "filters": _filters(pipeline, normalized_feedback, data_start, data_end),
    }
    return snapshot


def render_dashboard(snapshot: Mapping[str, object], template: str) -> str:
    """Embed deterministic JSON into the template's single data marker."""
    if template.count(_DATA_MARKER) != 1:
        raise ValueError(f"dashboard template must contain exactly one {_DATA_MARKER} marker")
    serialized = json.dumps(
        snapshot,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).replace("<", r"\u003c")
    return template.replace(_DATA_MARKER, serialized)


def _write_text_atomic(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_json(path: Path, expected_type: type) -> object:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot load {path}") from exc
    if not isinstance(value, expected_type):
        raise ValueError(f"{path} has unexpected JSON shape")
    return value


def _load_inputs(root: Path) -> tuple[
    list[dict[str, str]], list[dict[str, str]], list[dict[str, str]],
    list[dict[str, object]], list[dict[str, str]], dict[str, object],
]:
    from analytics.model import (
        EVENT_COLUMNS, FEEDBACK_COLUMNS, REVIEW_COLUMNS, TRACKER_COLUMNS,
        read_csv_rows,
    )

    analytics = root / "analytics"
    return (
        read_csv_rows(root / "job_search_tracker.csv", TRACKER_COLUMNS),
        read_csv_rows(analytics / "application_events.csv", EVENT_COLUMNS),
        read_csv_rows(analytics / "application_feedback.csv", FEEDBACK_COLUMNS),
        _load_json(analytics / "feedback_rules.json", list),
        read_csv_rows(analytics / "reconciliation_review.csv", REVIEW_COLUMNS),
        _load_json(analytics / "config.json", dict),
    )


def _cli_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("today must use YYYY-MM-DD") from exc


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the local job analytics dashboard")
    parser.add_argument("--sync-gmail", action="store_true")
    parser.add_argument("--today", type=_cli_date)
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    root = Path.cwd()
    if args.sync_gmail:
        from analytics.refresh import RefreshPaths, refresh

        refresh(
            RefreshPaths.for_root(root),
            client=None,
            sync_gmail=True,
            now=datetime.now(timezone.utc),
        )

    applications, events, feedback, rules, review_items, config = _load_inputs(root)
    _, reporting_zone = _reporting_zone(config)
    snapshot_date = args.today or datetime.now(reporting_zone).date()
    snapshot = build_snapshot(
        applications,
        events,
        feedback,
        rules,
        review_items,
        config,
        snapshot_date,
    )
    template_path = root / "dashboard" / "template.html"
    template = template_path.read_text(encoding="utf-8") if template_path.is_file() else _DEFAULT_TEMPLATE
    output_path = root / "dashboard" / "index.html"
    _write_text_atomic(output_path, render_dashboard(snapshot, template))
    print(json.dumps({
        "applications": snapshot["meta"]["record_counts"]["applications"],
        "generated_at": snapshot["meta"]["generated_at"],
        "output": str(output_path),
    }, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
