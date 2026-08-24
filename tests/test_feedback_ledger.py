import re
import tempfile
from contextlib import redirect_stdout
import unittest
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from analytics.feedback import (
    ACTIONS,
    _backfill_command,
    feedback_id,
    merge_feedback,
    seed_inception_feedback,
    validate_feedback,
)
from analytics.model import (
    FEEDBACK_COLUMNS,
    TRACKER_COLUMNS,
    read_csv_rows,
    write_csv_atomic,
)


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 8, 24, 12, 0, tzinfo=timezone.utc)
SHA256 = re.compile(r"[0-9a-f]{64}")


def valid_event(**overrides):
    event = {
        "feedback_id": "",
        "application_id": "app-1",
        "occurred_at": "2026-08-24T00:00:00Z",
        "stage": "application",
        "source": "tracker_backfill",
        "evidence_tier": "explicit",
        "category": "application_quality",
        "signal": "A specific signal",
        "evidence_excerpt": "A short excerpt.",
        "required_action": "A specific action.",
        "rule_effect": "activate",
        "resolves_feedback_id": "",
        "scope": '{"stage":"application"}',
        "confidence": "0.9",
        "source_ref": "2" * 64,
        "created_at": "2026-08-24T12:00:00Z",
    }
    event.update(overrides)
    if "feedback_id" not in overrides:
        event["feedback_id"] = feedback_id(
            event["application_id"],
            event["occurred_at"],
            event["category"],
            event["source_ref"],
        )
    return event


class FeedbackValidationTests(unittest.TestCase):
    def test_feedback_id_is_stable_and_input_sensitive(self):
        first = feedback_id("app-1", "2026-08-24T00:00:00Z", "technical_depth", "a" * 64)
        self.assertEqual(first, feedback_id("app-1", "2026-08-24T00:00:00Z", "technical_depth", "a" * 64))
        self.assertRegex(first, r"^fb-[0-9a-f]{64}$")
        self.assertNotEqual(first, feedback_id("app-2", "2026-08-24T00:00:00Z", "technical_depth", "a" * 64))

    def test_feedback_id_must_match_its_identity_fields(self):
        with self.assertRaisesRegex(ValueError, "feedback_id"):
            validate_feedback(
                valid_event(feedback_id="fb-" + "f" * 64),
                {"app-1"},
            )

    def test_exact_columns_are_required(self):
        event = valid_event()
        event["unexpected"] = "value"
        with self.assertRaisesRegex(ValueError, "columns"):
            validate_feedback(event, {"app-1"})
        event = valid_event()
        del event["scope"]
        with self.assertRaisesRegex(ValueError, "columns"):
            validate_feedback(event, {"app-1"})

    def test_foreign_key_and_excerpt_limit_are_enforced(self):
        with self.assertRaisesRegex(ValueError, "unknown application_id"):
            validate_feedback(valid_event(application_id="missing"), {"app-1"})
        with self.assertRaisesRegex(ValueError, "280"):
            validate_feedback(valid_event(evidence_excerpt="x" * 281), {"app-1"})
        validate_feedback(valid_event(evidence_excerpt="x" * 280), {"app-1"})

    def test_enumerations_and_confidence_are_enforced(self):
        invalid_fields = {
            "stage": "interview",
            "source": "gmail",
            "evidence_tier": "guess",
            "category": "other",
            "rule_effect": "ignore",
        }
        for field, value in invalid_fields.items():
            with self.subTest(field=field):
                with self.assertRaisesRegex(ValueError, field):
                    validate_feedback(valid_event(**{field: value}), {"app-1"})
        for confidence in ("", "nan", "-0.01", "1.01"):
            with self.subTest(confidence=confidence):
                with self.assertRaisesRegex(ValueError, "confidence"):
                    validate_feedback(valid_event(confidence=confidence), {"app-1"})

    def test_resolve_requires_a_feedback_reference(self):
        with self.assertRaisesRegex(ValueError, "resolves_feedback_id"):
            validate_feedback(valid_event(rule_effect="resolve"), {"app-1"})
        validate_feedback(
            valid_event(
                rule_effect="resolve",
                resolves_feedback_id="fb-" + "3" * 64,
            ),
            {"app-1"},
        )

    def test_source_reference_must_be_a_sha256_hash(self):
        with self.assertRaisesRegex(ValueError, "source_ref"):
            validate_feedback(valid_event(source_ref="gmail-thread-123"), {"app-1"})

    def test_identical_duplicates_are_idempotent(self):
        event = valid_event()
        self.assertEqual(merge_feedback([event], [dict(event)], {"app-1"}), [event])

    def test_conflicting_id_or_source_reference_raises(self):
        event = valid_event()
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            merge_feedback([event], [valid_event(signal="different")], {"app-1"})
        with self.assertRaisesRegex(ValueError, "conflicting duplicate"):
            merge_feedback(
                [event],
                [valid_event(application_id="app-2")],
                {"app-1", "app-2"},
            )


class InceptionFeedbackTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.applications = read_csv_rows(ROOT / "job_search_tracker.csv", TRACKER_COLUMNS)
        cls.events = seed_inception_feedback(cls.applications, NOW)
        cls.by_company = {}
        application_by_id = {row["application_id"]: row for row in cls.applications}
        for event in cls.events:
            company = application_by_id[event["application_id"]]["company"]
            cls.by_company.setdefault(company, []).append(event)

    def test_every_rejected_application_has_feedback(self):
        rejected_ids = {
            row["application_id"]
            for row in self.applications
            if "REJECTED" in row["status"].upper()
        }
        covered_ids = {event["application_id"] for event in self.events}
        self.assertTrue(rejected_ids)
        self.assertEqual(rejected_ids - covered_ids, set())

    def test_required_company_mappings_are_encoded(self):
        expected = {
            "CuspAI": {("explicit", "logistics_work_authorization")},
            "Zapier": {("explicit", "logistics_work_authorization")},
            "Dust": {("explicit", "role_seniority_alignment")},
            "Lobby": {("explicit", "role_seniority_alignment")},
            "Nordea": {
                ("explicit", "ml_genai_evaluation"),
                ("observed", "metric_rigor_provenance"),
            },
            "Wise": {
                ("observed", "leadership_people_evidence"),
                ("observed", "communication_decision_clarity"),
            },
            "Dragonfly (askdragonfly.com)": {
                ("observed", "technical_depth"),
                ("observed", "communication_decision_clarity"),
            },
            "Digital Workforce": {("observed", "logistics_work_authorization")},
        }
        for company, pairs in expected.items():
            with self.subTest(company=company):
                actual = {
                    (event["evidence_tier"], event["category"])
                    for event in self.by_company[company]
                }
                self.assertTrue(pairs <= actual)

    def test_required_high_value_actions_are_exact(self):
        actual = {event["required_action"] for event in self.events}
        self.assertEqual(set(ACTIONS), actual & set(ACTIONS))
        self.assertEqual(len(ACTIONS), 7)

    def test_generic_rejections_are_boilerplate_only(self):
        for company in ("RobCo", "Carta", "Databricks", "Taktile", "Bolt"):
            with self.subTest(company=company):
                events = self.by_company[company]
                self.assertTrue(events)
                self.assertEqual(
                    {(event["evidence_tier"], event["category"]) for event in events},
                    {("boilerplate", "competition_no_specific_signal")},
                )
                self.assertTrue(all(event["rule_effect"] == "monitor" for event in events))
                self.assertTrue(all(not event["required_action"] for event in events))

    def test_high_value_interview_events_are_actionable(self):
        for company in ("Nordea", "Wise", "Dragonfly (askdragonfly.com)"):
            with self.subTest(company=company):
                events = self.by_company[company]
                self.assertTrue(
                    any(
                        event["evidence_tier"] != "boilerplate"
                        and event["rule_effect"] == "activate"
                        and event["required_action"]
                        for event in events
                    )
                )

    def test_seed_output_obeys_schema_privacy_and_uniqueness(self):
        application_ids = {row["application_id"] for row in self.applications}
        feedback_ids = []
        source_refs = []
        for event in self.events:
            validate_feedback(event, application_ids)
            self.assertEqual(tuple(event), FEEDBACK_COLUMNS)
            self.assertLessEqual(len(event["evidence_excerpt"]), 280)
            self.assertIsNotNone(SHA256.fullmatch(event["source_ref"]))
            self.assertNotRegex(event["evidence_excerpt"], r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
            feedback_ids.append(event["feedback_id"])
            source_refs.append(event["source_ref"])
        self.assertEqual(len(feedback_ids), len(set(feedback_ids)))
        self.assertEqual(len(source_refs), len(set(source_refs)))
        self.assertEqual(
            self.events,
            seed_inception_feedback(self.applications, NOW),
        )

    def test_backfill_is_idempotent_across_insertion_times(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "application_feedback.csv"
            write_csv_atomic(output, FEEDBACK_COLUMNS, self.events)
            with redirect_stdout(StringIO()):
                _backfill_command(ROOT / "job_search_tracker.csv", output)
            self.assertEqual(
                read_csv_rows(output, FEEDBACK_COLUMNS),
                self.events,
            )


if __name__ == "__main__":
    unittest.main()
