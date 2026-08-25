import json
from datetime import date
from pathlib import Path

from dashboard.build import _load_inputs, build_snapshot, render_dashboard


ROOT = Path(__file__).parents[1]
OUTPUT = Path("/tmp/task9-dashboard-review-fixture.html")
SHIFTED_OUTPUT = Path("/tmp/task11-dashboard-shifted-fixture.html")

APPLICATIONS = [
    {
        "application_id": "app-browser-linked",
        "company": "Linked Review Co",
        "role": "Applied AI Engineer",
        "role_family": "applied_ai",
        "geography": "EEA Remote",
        "channel": "Direct",
        "logistics_status": "pass",
        "seniority": "senior",
        "screening_decision": "qualified",
        "stage": "submitted",
        "fit_score": "92",
        "status": "SUBMITTED",
        "source": "https://jobs.example/linked",
    },
    {
        "application_id": "app-browser-other",
        "company": "Other Co",
        "role": "AI Platform Engineer",
        "role_family": "ai_platform",
        "geography": "Tallinn",
        "channel": "Direct",
        "logistics_status": "pass",
        "seniority": "mid",
        "screening_decision": "qualified",
        "stage": "qualified",
        "fit_score": "84",
        "status": "QUALIFIED",
        "source": "https://jobs.example/other",
    },
]
EVENTS = [
    {
        "event_id": "event-linked-discovered",
        "application_id": "app-browser-linked",
        "event_type": "discovered",
        "occurred_at": "2026-08-20T08:00:00Z",
    },
    {
        "event_id": "event-linked-submitted",
        "application_id": "app-browser-linked",
        "event_type": "submitted",
        "occurred_at": "2026-08-21T08:00:00Z",
    },
    {
        "event_id": "event-other-discovered",
        "application_id": "app-browser-other",
        "event_type": "discovered",
        "occurred_at": "2026-08-22T08:00:00Z",
    },
]
REVIEWS = [
    {
        "review_id": "review-browser-linked",
        "occurred_at": "2026-08-23T08:00:00Z",
        "sender": "Fixture Recruiting",
        "subject": "Fixture application update",
        "company": "Linked Review Co",
        "role": "Applied AI Engineer",
        "candidate_application_ids": json.dumps([
            "app-browser-linked",
            "app-browser-other",
        ]),
        "reason": "Confirm the linked reconciliation match.",
        "source_ref": "1" * 64,
        "status": "pending",
    },
    {
        "review_id": "review-browser-global",
        "occurred_at": "2026-08-23T09:00:00Z",
        "sender": "Fixture Recruiting",
        "subject": "Fixture unmapped update",
        "company": "Unmapped Review Co",
        "role": "Unknown Role",
        "candidate_application_ids": "[]",
        "reason": "Resolve this review without an application ID.",
        "source_ref": "2" * 64,
        "status": "pending",
    },
]
CONFIG = {
    "daily_screening_target": 100,
    "daily_submission_soft_capacity": 20,
    "stale_after_days": 14,
    "reporting_timezone": "Europe/Tallinn",
}


def main() -> None:
    snapshot = build_snapshot(
        APPLICATIONS,
        EVENTS,
        [],
        [],
        REVIEWS,
        CONFIG,
        date(2026, 8, 24),
    )
    template = (ROOT / "dashboard" / "template.html").read_text(encoding="utf-8")
    OUTPUT.write_text(render_dashboard(snapshot, template), encoding="utf-8")
    applications, events, feedback, rules, reviews, config = _load_inputs(ROOT)
    shifted_applications = [*applications, {
        "application_id": "app-browser-shifted",
        "company": "Shifted Fixture Co",
        "role": "Applied AI Engineer",
        "role_family": "applied_ai",
        "geography": "EEA Remote",
        "channel": "Fixture",
        "logistics_status": "pass",
        "screening_decision": "pending",
        "stage": "prospect",
        "fit_score": "81",
        "status": "DISCOVERED",
        "source": "https://jobs.example/shifted",
    }]
    shifted_events = [*events, {
        "event_id": "event-browser-shifted-discovered",
        "application_id": "app-browser-shifted",
        "event_type": "discovered",
        "occurred_at": "2026-08-26T08:00:00Z",
    }]
    shifted_snapshot = build_snapshot(
        shifted_applications,
        shifted_events,
        feedback,
        rules,
        reviews,
        config,
        date(2026, 8, 26),
    )
    SHIFTED_OUTPUT.write_text(
        render_dashboard(shifted_snapshot, template),
        encoding="utf-8",
    )
    print(OUTPUT)
    print(SHIFTED_OUTPUT)

if __name__ == "__main__":
    main()
