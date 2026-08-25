import unittest
from datetime import date
from pathlib import Path
import re

from dashboard.build import build_snapshot, render_dashboard


APPLICATIONS = [
    {
        "application_id": "app-1",
        "company": "Alpha",
        "role": "Applied AI Engineer",
        "role_family": "applied_ai",
        "geography": "EEA Remote",
        "channel": "Ashby",
        "logistics_status": "pass",
        "seniority": "senior",
        "screening_decision": "qualified",
        "stage": "submitted",
        "fit_score": "90",
        "status": "SUBMITTED 2026-08-24",
        "status_updated_at": "2026-08-24",
        "source": "https://jobs.example/alpha",
    },
    {
        "application_id": "app-2",
        "company": "Beta",
        "role": "AI Platform Engineer",
        "role_family": "ai_platform",
        "geography": "Tallinn",
        "channel": "Teamtailor",
        "logistics_status": "pass",
        "seniority": "mid",
        "screening_decision": "qualified",
        "stage": "qualified",
        "fit_score": "85",
        "status": "QUALIFIED",
        "status_updated_at": "2026-08-24",
        "source": "https://jobs.example/beta",
    },
]
EVENTS = [
    {"event_id": "evt-1", "application_id": "app-1", "event_type": "discovered", "occurred_at": "2026-08-23T08:00:00Z"},
    {"event_id": "evt-2", "application_id": "app-1", "event_type": "screened", "occurred_at": "2026-08-24T08:00:00Z"},
    {"event_id": "evt-3", "application_id": "app-1", "event_type": "submitted", "occurred_at": "2026-08-24T09:00:00Z"},
    {"event_id": "evt-4", "application_id": "app-2", "event_type": "discovered", "occurred_at": "2026-08-24T09:30:00Z"},
    {"event_id": "evt-5", "application_id": "app-2", "event_type": "screened", "occurred_at": "2026-08-24T10:00:00Z"},
    {"event_id": "evt-6", "application_id": "app-2", "event_type": "qualified", "occurred_at": "2026-08-23T10:05:00Z"},
]
FEEDBACK = [{
    "feedback_id": "fb-1",
    "application_id": "app-1",
    "category": "metric_rigor_provenance",
    "evidence_tier": "observed",
    "evidence_excerpt": "The metric omitted its denominator.",
    "required_action": "State the denominator and source.",
    "confidence": "0.95",
}]
RULES = [{
    "rule_id": "rule-1",
    "category": "metric_rigor_provenance",
    "status": "active",
    "required_action": "State the denominator and source.",
    "confidence": 0.95,
    "evidence_tiers": ["observed"],
    "source_feedback_ids": ["fb-1"],
}]
CONFIG = {
    "daily_screening_target": 100,
    "daily_submission_soft_capacity": 20,
    "stale_after_days": 14,
    "reporting_timezone": "Europe/Tallinn",
}
TODAY = date(2026, 8, 24)


def application(application_id, score, stage="submitted"):
    return {
        "application_id": application_id,
        "company": f"Company {application_id}",
        "role": "Applied AI Engineer",
        "role_family": "applied_ai",
        "geography": "EEA Remote",
        "channel": "Ashby",
        "logistics_status": "pass",
        "seniority": "senior",
        "screening_decision": "qualified",
        "stage": stage,
        "fit_score": str(score),
        "status": stage.upper(),
        "source": f"https://jobs.example/{application_id}",
    }


def event(event_id, application_id, event_type, occurred_at):
    return {
        "event_id": event_id,
        "application_id": application_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
    }


class DashboardBuildTests(unittest.TestCase):
    def build(self, **overrides):
        arguments = {
            "applications": APPLICATIONS,
            "events": EVENTS,
            "feedback": FEEDBACK,
            "rules": RULES,
            "review_items": [],
            "config": CONFIG,
            "today": TODAY,
        }
        arguments.update(overrides)
        return build_snapshot(**arguments)

    def test_snapshot_has_complete_contract_and_separates_screened_from_submitted(self):
        snapshot = self.build()

        self.assertEqual(
            set(snapshot),
            {
                "meta", "today", "funnel", "lifecycle_application_ids",
                "daily_series", "weekly_cohorts", "response_metrics",
                "calibration", "feedback", "pipeline", "data_quality",
                "filters",
            },
        )
        self.assertEqual(snapshot["meta"]["generated_at"], "2026-08-24T00:00:00Z")
        self.assertEqual(snapshot["today"]["screening_target"], 100)
        self.assertEqual(snapshot["today"]["submission_soft_capacity"], 20)
        self.assertEqual(snapshot["today"]["screened"], 2)
        self.assertEqual(snapshot["today"]["submitted"], 1)
        self.assertEqual(snapshot["today"]["qualified"], 1)
        self.assertEqual(snapshot["today"]["application_ids"], {
            "screened": ["app-1", "app-2"],
            "rejected_by_gate": [],
            "submitted": ["app-1"],
            "follow_ups": [],
        })
        self.assertEqual(snapshot["funnel"]["screened"], 2)
        self.assertEqual(snapshot["funnel"]["submitted"], 1)
        self.assertEqual(snapshot["lifecycle_application_ids"], {
            "discovered": ["app-1", "app-2"],
            "screened": ["app-1", "app-2"],
            "qualified": ["app-2"],
            "submitted": ["app-1"],
            "responded": [],
            "interviewed": [],
            "offered": [],
        })

    def test_funnel_timing_and_medians_use_deduplicated_lifecycle_events(self):
        applications = [application(f"app-{number}", 95 - number) for number in range(1, 8)]
        events = []
        for number in range(1, 8):
            events.append(event(f"d-{number}", f"app-{number}", "discovered", "2026-08-01T08:00:00Z"))
        transitions = [
            ("s-1", "app-1", "submitted", "2026-08-24T09:00:00Z"),
            ("r-1", "app-1", "rejected", "2026-08-24T12:00:00Z"),
            ("s-3", "app-3", "submitted", "2026-08-18T09:00:00Z"),
            ("i-3", "app-3", "interview", "2026-08-20T09:00:00Z"),
            ("s-4", "app-4", "submitted", "2026-08-18T10:00:00Z"),
            ("o-4", "app-4", "offer", "2026-08-22T10:00:00Z"),
            ("s-5", "app-5", "submitted", "2026-08-19T08:00:00Z"),
            ("s-6", "app-6", "submitted", "2026-08-19T11:00:00Z"),
            ("r-6", "app-6", "rejected", "2026-08-24T11:00:00Z"),
            ("s-7", "app-7", "submitted", "2026-08-20T12:00:00Z"),
            ("p-7", "app-7", "responded", "2026-08-21T12:00:00Z"),
            ("i-7", "app-7", "interview", "2026-08-23T12:00:00Z"),
        ]
        events.extend(event(*transition) for transition in transitions)
        events.append(dict(events[-1]))

        snapshot = self.build(applications=applications, events=events, feedback=[], rules=[])

        self.assertEqual(snapshot["funnel"], {
            "discovered": 7,
            "screened": 0,
            "qualified": 0,
            "submitted": 6,
            "responded": 5,
            "interviewed": 2,
            "offered": 1,
        })
        metrics = snapshot["response_metrics"]
        self.assertEqual(metrics["submitted"], 6)
        self.assertEqual(metrics["responded"], 5)
        self.assertAlmostEqual(metrics["response_rate"], 5 / 6)
        self.assertAlmostEqual(metrics["interview_rate"], 2 / 6)
        self.assertAlmostEqual(metrics["offer_rate"], 1 / 6)
        self.assertEqual(metrics["median_time_to_response_hours"], 48.0)
        self.assertEqual(metrics["median_time_to_decision_hours"], 96.0)
        self.assertEqual(metrics["open_applications"], 3)
        self.assertEqual(snapshot["data_quality"]["duplicates"]["events"], ["i-7"])

    def test_zero_denominators_and_missing_timing_emit_defined_values(self):
        snapshot = self.build(events=[], feedback=[], rules=[])

        self.assertEqual(snapshot["response_metrics"]["response_rate"], 0.0)
        self.assertEqual(snapshot["response_metrics"]["interview_rate"], 0.0)
        self.assertEqual(snapshot["response_metrics"]["offer_rate"], 0.0)
        self.assertIsNone(snapshot["response_metrics"]["median_time_to_response_hours"])
        self.assertIsNone(snapshot["response_metrics"]["median_time_to_decision_hours"])
        self.assertEqual(snapshot["daily_series"], [{
            "date": "2026-08-24", "discovered": 0, "screened": 0,
            "qualified": 0, "submitted": 0, "responded": 0,
            "interviewed": 0, "offered": 0,
            "submitted_application_ids": [],
        }])

    def test_outcomes_before_submission_do_not_count_as_conversion(self):
        events = [
            event("d-1", "app-1", "discovered", "2026-08-20T08:00:00Z"),
            event("r-1", "app-1", "rejected", "2026-08-20T09:00:00Z"),
            event("s-1", "app-1", "submitted", "2026-08-21T09:00:00Z"),
        ]

        snapshot = self.build(applications=[APPLICATIONS[0]], events=events, feedback=[], rules=[])

        self.assertEqual(snapshot["funnel"]["responded"], 0)
        self.assertEqual(snapshot["response_metrics"]["responded"], 0)
        self.assertEqual(snapshot["response_metrics"]["open_applications"], 1)
        self.assertIsNone(snapshot["response_metrics"]["median_time_to_response_hours"])
        daily = {row["date"]: row for row in snapshot["daily_series"]}
        self.assertEqual(daily["2026-08-20"]["responded"], 0)

    def test_missing_early_lifecycle_events_emit_coverage_warning_without_inference(self):
        events = [
            event("d-1", "app-1", "discovered", "2026-08-20T08:00:00Z"),
            event("s-1", "app-1", "submitted", "2026-08-21T09:00:00Z"),
        ]

        snapshot = self.build(applications=[APPLICATIONS[0]], events=events, feedback=[], rules=[])

        self.assertEqual(snapshot["funnel"]["screened"], 0)
        self.assertEqual(snapshot["funnel"]["qualified"], 0)
        self.assertIn("lifecycle_coverage", snapshot["meta"]["warnings"])
        self.assertEqual(snapshot["data_quality"]["lifecycle_coverage"], {
            "missing_event_types": ["qualified", "screened"],
            "early_funnel_conversion_available": False,
            "insufficient": True,
        })

    def test_reporting_timezone_controls_operational_day_and_week_boundaries(self):
        events = [
            event("s-1", "app-1", "submitted", "2026-08-23T22:30:00Z"),
        ]

        snapshot = self.build(applications=[APPLICATIONS[0]], events=events, feedback=[], rules=[])

        self.assertEqual(snapshot["daily_series"], [{
            "date": "2026-08-24", "discovered": 0, "screened": 0,
            "qualified": 0, "submitted": 1, "responded": 0,
            "interviewed": 0, "offered": 0,
            "submitted_application_ids": ["app-1"],
        }])
        self.assertEqual(snapshot["weekly_cohorts"][0]["week_start"], "2026-08-24")
        self.assertEqual(snapshot["pipeline"][0]["application_date"], "2026-08-24")
        self.assertEqual(snapshot["meta"]["reporting_timezone"], "Europe/Tallinn")

    def test_daily_series_preserves_zero_days_and_submitted_membership(self):
        events = [
            event("s-1", "app-1", "submitted", "2026-08-20T08:00:00Z"),
            event("s-2", "app-2", "submitted", "2026-08-22T08:00:00Z"),
        ]

        series = self.build(events=events, feedback=[], rules=[])["daily_series"]

        self.assertEqual(
            [row["date"] for row in series],
            ["2026-08-20", "2026-08-21", "2026-08-22", "2026-08-23", "2026-08-24"],
        )
        self.assertEqual(series[0]["submitted_application_ids"], ["app-1"])
        self.assertEqual(series[1]["submitted"], 0)
        self.assertEqual(series[1]["submitted_application_ids"], [])
        self.assertEqual(series[2]["submitted_application_ids"], ["app-2"])
        self.assertEqual(series[-1]["submitted"], 0)

    def test_fit_band_calibration_counts_match_lifecycle_membership(self):
        snapshot = self.build()
        by_band = {row["value"]: row for row in snapshot["calibration"]["fit_band"]}
        submitted_ids = set(snapshot["lifecycle_application_ids"]["submitted"])
        app_ids_by_band = {
            "90-100": {"app-1"},
            "80-89": {"app-2"},
        }

        for band, application_ids in app_ids_by_band.items():
            with self.subTest(band=band):
                self.assertEqual(by_band[band]["submitted"], len(application_ids & submitted_ids))

    def test_invalid_reporting_timezone_fails_precisely(self):
        with self.assertRaisesRegex(ValueError, "invalid reporting_timezone: 'Mars/Olympus'"):
            self.build(config={**CONFIG, "reporting_timezone": "Mars/Olympus"})

    def test_calibration_and_weekly_cohorts_gate_conversion_below_five_submissions(self):
        applications = [application(f"app-{number}", 96 - number) for number in range(1, 7)]
        applications.append({**application("app-small", 80), "role_family": "ai_platform"})
        events = []
        for number in range(1, 7):
            app_id = f"app-{number}"
            events.extend([
                event(f"d-{number}", app_id, "discovered", "2026-08-17T08:00:00Z"),
                event(f"s-{number}", app_id, "submitted", f"2026-08-{17 + number:02d}T09:00:00Z"),
            ])
        events.extend([
            event("d-small", "app-small", "discovered", "2026-08-24T08:00:00Z"),
            event("s-small", "app-small", "submitted", "2026-08-24T09:00:00Z"),
            event("r-1", "app-1", "rejected", "2026-08-23T09:00:00Z"),
        ])

        snapshot = self.build(applications=applications, events=events, feedback=[], rules=[])
        role_segments = {row["value"]: row for row in snapshot["calibration"]["role_family"]}

        self.assertFalse(role_segments["applied_ai"]["insufficient_sample"])
        self.assertEqual(role_segments["applied_ai"]["conversion_rates"]["response"], 1 / 6)
        self.assertTrue(role_segments["ai_platform"]["insufficient_sample"])
        self.assertNotIn("conversion_rates", role_segments["ai_platform"])
        self.assertTrue(all(0.0 <= value <= 1.0 for value in role_segments["applied_ai"]["conversion_rates"].values()))
        cohorts = {row["week_start"]: row for row in snapshot["weekly_cohorts"]}
        self.assertFalse(cohorts["2026-08-17"]["insufficient_sample"])
        self.assertTrue(cohorts["2026-08-24"]["insufficient_sample"])
        self.assertNotIn("conversion_rates", cohorts["2026-08-24"])

    def test_feedback_pipeline_and_filters_are_normalized_and_sorted(self):
        feedback = FEEDBACK + [{
            "feedback_id": "fb-2",
            "application_id": "app-2",
            "category": "technical_depth",
            "evidence_tier": "explicit",
            "evidence_excerpt": "The explanation lacked implementation depth.",
            "required_action": "Lead with implementation evidence.",
            "confidence": "0.9",
        }]
        rules = RULES + [{
            "rule_id": "rule-2",
            "category": "technical_depth",
            "status": "monitor",
            "required_action": "Lead with implementation evidence.",
            "confidence": 0.9,
            "evidence_tiers": ["explicit"],
            "source_feedback_ids": ["fb-2"],
        }]

        snapshot = self.build(feedback=feedback, rules=rules)

        self.assertEqual(snapshot["feedback"]["category_counts"], {
            "metric_rigor_provenance": 1,
            "technical_depth": 1,
        })
        self.assertEqual(snapshot["feedback"]["rule_status_counts"], {
            "active": 1, "monitor": 1, "resolved": 0,
        })
        self.assertEqual(snapshot["feedback"]["lineage"][0]["application_ids"], ["app-1"])
        first_lineage = snapshot["feedback"]["lineage"][0]
        self.assertEqual(first_lineage["category"], "metric_rigor_provenance")
        self.assertEqual(first_lineage["required_action"], "State the denominator and source.")
        self.assertEqual(first_lineage["confidence"], 0.95)
        self.assertEqual(first_lineage["evidence_tiers"], ["observed"])
        self.assertEqual(first_lineage["source_feedback"], [{
            "feedback_id": "fb-1",
            "application_id": "app-1",
            "category": "metric_rigor_provenance",
            "evidence_tier": "observed",
            "evidence_excerpt": "The metric omitted its denominator.",
            "required_action": "State the denominator and source.",
            "confidence": 0.95,
        }])
        self.assertEqual(snapshot["feedback"]["category_application_ids"], {
            "metric_rigor_provenance": ["app-1"],
            "technical_depth": ["app-2"],
        })
        self.assertEqual([row["application_id"] for row in snapshot["pipeline"]], ["app-1", "app-2"])
        self.assertEqual(snapshot["pipeline"][0]["application_date"], "2026-08-24")
        self.assertEqual(snapshot["pipeline"][0]["next_action"], "Follow up")
        self.assertEqual(snapshot["filters"]["role_family"], ["ai_platform", "applied_ai"])
        self.assertEqual(snapshot["filters"]["logistics_status"], ["pass"])
        self.assertEqual(snapshot["filters"]["seniority"], ["mid", "senior"])
        self.assertEqual(snapshot["filters"]["evidence_tier"], ["explicit", "observed"])
        self.assertEqual(snapshot["filters"]["feedback_category"], ["metric_rigor_provenance", "technical_depth"])
        self.assertEqual(snapshot["filters"]["date_range"], {"start": "2026-08-23", "end": "2026-08-24"})

    def test_data_quality_reports_missing_ambiguous_duplicate_stale_and_orphan_rows(self):
        applications = [
            application("app-1", 91),
            {**application("app-old", ""), "stage": "mystery"},
            dict(application("app-1", 91)),
            {**application("", 80), "company": "No ID"},
        ]
        events = [
            event("old", "app-old", "discovered", "2026-08-01T08:00:00Z"),
            event("dup", "app-1", "submitted", "2026-08-20T08:00:00Z"),
            event("dup", "app-1", "submitted", "2026-08-20T08:00:00Z"),
            event("orphan", "app-404", "rejected", "2026-08-21T08:00:00Z"),
            event("bad-time", "app-1", "interview", "not-a-date"),
        ]
        feedback = [{"feedback_id": "fb-orphan", "application_id": "app-404", "category": "technical_depth", "evidence_tier": "observed"}]
        review_items = [{
            "review_id": "review-1",
            "candidate_application_ids": "[]",
            "status": "pending",
        }]

        quality = self.build(
            applications=applications,
            events=events,
            feedback=feedback,
            rules=[],
            review_items=review_items,
        )["data_quality"]

        self.assertEqual(quality["missing_scores"], ["app-old"])
        self.assertEqual(quality["missing_dates"], ["app-1"])
        self.assertEqual(quality["ambiguous_statuses"], ["app-old"])
        self.assertEqual(quality["duplicates"]["application_ids"], ["app-1"])
        self.assertEqual(quality["duplicates"]["events"], ["dup"])
        self.assertEqual(quality["orphaned_application_rows"], [4])
        self.assertEqual(quality["orphaned_events"], ["orphan"])
        self.assertEqual(quality["orphaned_feedback"], ["fb-orphan"])
        self.assertEqual(quality["invalid_event_timestamps"], ["bad-time"])
        self.assertEqual(quality["stale_rows"], ["app-old"])
        self.assertEqual(quality["review_queue"]["count"], 1)
        self.assertEqual(quality["application_ids"]["duplicate_events"], ["app-1"])
        self.assertEqual(quality["application_ids"]["invalid_event_timestamps"], ["app-1"])
        self.assertEqual(quality["application_ids"]["stale_rows"], ["app-old"])
        self.assertEqual(quality["application_ids"]["orphaned_events"], [])

    def test_review_queue_preserves_candidate_arrays_and_unmapped_items(self):
        review_items = [
            {
                "review_id": "review-linked",
                "candidate_application_ids": '["app-1", "app-2", "app-1"]',
                "status": "pending",
                "company": "Alpha",
                "role": "Applied AI Engineer",
                "reason": "Confirm the Gmail match.",
            },
            {
                "review_id": "review-global",
                "candidate_application_ids": "[]",
                "status": "pending",
                "company": "Unknown",
                "role": "Unknown",
                "reason": "Resolve without an application ID.",
            },
        ]

        quality = self.build(review_items=review_items)["data_quality"]

        self.assertEqual(quality["review_queue"]["count"], 2)
        self.assertEqual(
            [
                item["candidate_application_ids"]
                for item in quality["review_queue"]["items"]
            ],
            [[], ["app-1", "app-2"]],
        )
        self.assertEqual(
            quality["application_ids"]["review_queue"],
            ["app-1", "app-2"],
        )

    def test_review_queue_rejects_malformed_or_unknown_candidate_arrays(self):
        for value in ("not-json", "{}", '["missing-app"]'):
            with self.subTest(value=value):
                with self.assertRaisesRegex(
                    ValueError,
                    "review candidate_application_ids",
                ):
                    self.build(review_items=[{
                        "review_id": "review-invalid",
                        "candidate_application_ids": value,
                        "status": "pending",
                    }])

    def test_render_is_deterministic_html_safe_and_self_contained(self):
        template = "<html><script>window.DATA=__DASHBOARD_DATA__</script></html>"

        first = render_dashboard({"b": 2, "a": "</script><tag>"}, template)
        second = render_dashboard({"a": "</script><tag>", "b": 2}, template)

        self.assertEqual(first, second)
        self.assertNotIn("__DASHBOARD_DATA__", first)
        self.assertNotIn("</script><tag>", first)
        self.assertIn(r"\u003c/script>\u003ctag>", first)
        self.assertNotIn("https://", first)

    def test_render_requires_exactly_one_marker(self):
        for template in ("<html></html>", "__DASHBOARD_DATA____DASHBOARD_DATA__"):
            with self.subTest(template=template):
                with self.assertRaisesRegex(ValueError, "exactly one"):
                    render_dashboard({}, template)

    def test_dashboard_template_exposes_accessible_self_contained_contract(self):
        template_path = Path(__file__).parents[1] / "dashboard" / "template.html"
        template = template_path.read_text(encoding="utf-8")
        lowered = template.casefold()

        self.assertEqual(template.count("__DASHBOARD_DATA__"), 1)
        self.assertNotRegex(lowered, r'(?:src|href)\s*=\s*["\']https?://')
        self.assertNotIn("@import", lowered)
        self.assertNotRegex(lowered, r'url\(\s*["\']?https?://')
        self.assertNotIn("transition: all", lowered)
        self.assertIn("@media (prefers-reduced-motion: reduce)", lowered)

        for landmark in (
            '<a class="skip-link" href="#main">',
            'aria-label="dashboard sections"',
            "<header",
            '<main id="main"',
            '<div id="status" role="status" aria-live="polite"',
        ):
            self.assertIn(landmark, lowered)
        self.assertEqual(len(re.findall(r"<h1(?:\s|>)", lowered)), 1)

        for control_name in (
            "date-start", "date-end", "role-family", "geography", "channel",
            "stage", "fit-band", "evidence-tier", "feedback-category",
            "logistics-status", "seniority", "pipeline-search", "cadence-granularity",
        ):
            with self.subTest(control_name=control_name):
                self.assertRegex(
                    lowered,
                    rf'<label[^>]+for=["\']{re.escape(control_name)}["\']',
                )

        self.assertIn("urlsearchparams", lowered)
        self.assertIn("history.replacestate", lowered)
        self.assertIn('addEventListener("popstate"', template)
        self.assertIn('role="img"', lowered)
        self.assertIn("<title", lowered)
        self.assertIn("<desc", lowered)
        self.assertIn("<table", lowered)
        self.assertIn('aria-sort="', lowered)
        self.assertIn("intl.datetimeformat", lowered)
        self.assertIn("intl.numberformat", lowered)
        self.assertIn("content-visibility: auto", lowered)
        for chart_id in (
            "funnel-chart", "time-series-chart", "calibration-chart",
            "feedback-category-chart", "aging-chart",
        ):
            with self.subTest(chart_id=chart_id):
                self.assertIn(f'id="{chart_id}"', lowered)
        self.assertIn('<details id="filter-disclosure"', lowered)
        self.assertIn('id="filter-active-count"', lowered)
        self.assertIn('data-application-id=', lowered)
        self.assertIn("cadence interval", lowered)
        self.assertIn("weekly", lowered)
        self.assertIn("axis-line-strong", lowered)
        self.assertIn("full continuous range", lowered)
        self.assertIn('class="quality-targets"', lowered)
        self.assertIn('data-focus-target=', lowered)
        self.assertIn("<strong>category:</strong>", lowered)

    def test_generated_dashboard_embeds_snapshot_without_external_dependencies(self):
        template_path = Path(__file__).parents[1] / "dashboard" / "template.html"
        template = template_path.read_text(encoding="utf-8")

        rendered = render_dashboard(self.build(), template)

        self.assertNotIn("__DASHBOARD_DATA__", rendered)
        self.assertIn('"applications":2', rendered)
        self.assertIn('"application_id":"app-1"', rendered)
        self.assertIn('"category_application_ids":', rendered)
        self.assertIn('"evidence_excerpt":"The metric omitted its denominator."', rendered)
        self.assertIn('"required_action":"State the denominator and source."', rendered)
        self.assertIn('"application_ids":{"follow_ups":[],"rejected_by_gate":[],"screened":["app-1","app-2"],"submitted":["app-1"]}', rendered)
        self.assertIn('"lifecycle_application_ids":', rendered)
        self.assertIn('"submitted_application_ids":["app-1"]', rendered)
        self.assertNotRegex(rendered.casefold(), r'(?:src|href)\s*=\s*["\']https?://')
        self.assertNotRegex(rendered.casefold(), r'url\(\s*["\']?https?://')
        self.assertIn("Dashboard ready", rendered)


if __name__ == "__main__":
    unittest.main()
