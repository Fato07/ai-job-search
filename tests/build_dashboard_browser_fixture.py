from datetime import date
from pathlib import Path

from dashboard.build import build_snapshot, render_dashboard


ROOT = Path(__file__).parents[1]
OUTPUT = Path("/tmp/task9-dashboard-review-fixture.html")

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
        "application_id": "app-browser-linked",
        "status": "pending",
        "company": "Linked Review Co",
        "role": "Applied AI Engineer",
        "reason": "Confirm the linked reconciliation match.",
    },
    {
        "review_id": "review-browser-global",
        "application_id": "",
        "status": "pending",
        "company": "Unmapped Review Co",
        "role": "Unknown Role",
        "reason": "Resolve this review without an application ID.",
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
    print(OUTPUT)


if __name__ == "__main__":
    main()
