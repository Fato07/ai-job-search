import unittest
from datetime import datetime, timezone

from analytics.events import backfill_events, event_id, merge_events
from analytics.model import hash_source_ref


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


class LifecycleEventTests(unittest.TestCase):
    def test_backfill_creates_discovery_submission_and_rejection(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-08-20",
            "submitted_at": "2026-08-20",
            "status_updated_at": "2026-08-21",
            "stage": "closed",
            "status": "REJECTED 2026-08-21",
            "notes": "Gmail rejection received 2026-08-21.",
        }
        events = backfill_events([application], NOW)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["discovered", "submitted", "rejected"],
        )

    def test_note_only_clear_rejection_outcomes_create_rejected_events(self):
        applications = [
            {
                "application_id": application_id,
                "discovered_at": "2026-08-20",
                "submitted_at": "",
                "status_updated_at": "2026-08-20",
                "stage": "prospect",
                "status": "OPEN",
                "notes": notes,
            }
            for application_id, notes in (
                ("app-passive", "Application was rejected 2026-08-21."),
                (
                    "app-active",
                    "The hiring team rejected the candidate 2026-08-22.",
                ),
            )
        ]
        rejected = [
            (event["application_id"], event["occurred_at"])
            for event in backfill_events(applications, NOW)
            if event["event_type"] == "rejected"
        ]
        self.assertEqual(
            rejected,
            [
                ("app-passive", "2026-08-21T00:00:00Z"),
                ("app-active", "2026-08-22T00:00:00Z"),
            ],
        )

    def test_passive_rejection_requires_employer_actor_direction(self):
        applications = [
            {
                "application_id": application_id,
                "discovered_at": "2026-08-20",
                "submitted_at": "",
                "status_updated_at": "2026-08-20",
                "stage": "prospect",
                "status": "OPEN",
                "notes": notes,
            }
            for application_id, notes in (
                (
                    "app-employer-actor",
                    "Candidate rejected by the employer 2026-08-21.",
                ),
                (
                    "app-candidate-actor",
                    "Rejected by the candidate 2026-08-21.",
                ),
            )
        ]

        rejected = [
            (event["application_id"], event["occurred_at"])
            for event in backfill_events(applications, NOW)
            if event["event_type"] == "rejected"
        ]

        self.assertEqual(
            rejected,
            [("app-employer-actor", "2026-08-21T00:00:00Z")],
        )

    def test_candidate_direction_and_negation_are_not_employer_rejections(self):
        notes = (
            "Candidate rejected the offer 2026-08-21.",
            "I will not proceed with the application 2026-08-21.",
            "No candidate was rejected 2026-08-21.",
        )
        for index, note in enumerate(notes):
            with self.subTest(note=note):
                application = {
                    "application_id": f"app-{index}",
                    "discovered_at": "2026-08-20",
                    "submitted_at": "",
                    "status_updated_at": "2026-08-20",
                    "stage": "prospect",
                    "status": "OPEN",
                    "notes": note,
                }
                events = backfill_events([application], NOW)
                self.assertEqual(
                    [event["event_type"] for event in events],
                    ["discovered"],
                )

    def test_dated_note_event_requires_phrase_and_date_in_same_sentence(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-08-20",
            "submitted_at": "",
            "status_updated_at": "2026-08-20",
            "stage": "prospect",
            "status": "AWAITING REVIEW",
            "notes": (
                "The application was viewed. Logged on 2026-08-21. "
                "Follow-up sent 2026-08-22. Interview invite received without a date."
            ),
        }
        events = backfill_events([application], NOW)
        self.assertEqual(
            [(event["event_type"], event["occurred_at"]) for event in events],
            [
                ("discovered", "2026-08-20T00:00:00Z"),
                ("follow_up", "2026-08-22T00:00:00Z"),
            ],
        )

    def test_note_sentence_can_capture_distinct_rejection_and_interview_dates(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-07-01",
            "submitted_at": "",
            "status_updated_at": "2026-07-01",
            "stage": "prospect",
            "status": "OPEN",
            "notes": (
                "Rejection received 2026-07-31 after the 2026-07-30 video interview."
            ),
        }
        events = backfill_events([application], NOW)
        self.assertEqual(
            [(event["event_type"], event["occurred_at"]) for event in events],
            [
                ("discovered", "2026-07-01T00:00:00Z"),
                ("interview", "2026-07-30T00:00:00Z"),
                ("rejected", "2026-07-31T00:00:00Z"),
            ],
        )

    def test_note_detail_truncates_at_280_but_hashes_full_sentence(self):
        prefix = "Application was viewed 2026-08-21 "
        sentence_280 = prefix + "x" * (280 - len(prefix))
        sentence_281 = prefix + "y" * (281 - len(prefix))
        applications = [
            {
                "application_id": application_id,
                "discovered_at": "2026-08-20",
                "submitted_at": "",
                "status_updated_at": "2026-08-20",
                "stage": "prospect",
                "status": "OPEN",
                "notes": sentence,
            }
            for application_id, sentence in (
                ("app-280", sentence_280),
                ("app-281", sentence_281),
            )
        ]
        viewed = {
            event["application_id"]: event
            for event in backfill_events(applications, NOW)
            if event["event_type"] == "viewed"
        }
        self.assertEqual(viewed["app-280"]["detail"], sentence_280)
        self.assertEqual(len(viewed["app-281"]["detail"]), 280)
        self.assertEqual(viewed["app-281"]["detail"], sentence_281[:280])
        self.assertEqual(
            viewed["app-281"]["source_ref"],
            hash_source_ref(f"app-281\x1fnotes:{sentence_281}"),
        )

    def test_note_parser_ignores_non_occurrence_and_negative_contexts(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-08-20",
            "submitted_at": "",
            "status_updated_at": "2026-08-20",
            "stage": "prospect",
            "status": "OPEN",
            "notes": (
                "The team will follow up 2026-08-21. "
                "Choose Ready to interview 2026-08-22. "
                "Interview-recording opt-out notice received 2026-08-23. "
                "Status marked post-interview 2026-08-24. "
                "Follow-up draft prepared 2026-08-25. "
                "No follow-up received 2026-08-26."
            ),
        }
        events = backfill_events([application], NOW)
        self.assertEqual(
            [(event["event_type"], event["occurred_at"]) for event in events],
            [("discovered", "2026-08-20T00:00:00Z")],
        )

    def test_post_interview_rejection_status_does_not_date_an_interview(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-07-01",
            "submitted_at": "2026-07-01",
            "status_updated_at": "2026-08-04",
            "stage": "interview",
            "status": "REJECTED 2026-08-04 (post-interview)",
            "notes": "",
        }
        events = backfill_events([application], NOW)
        self.assertEqual(
            [event["event_type"] for event in events],
            ["discovered", "submitted", "rejected"],
        )

    def test_event_id_is_stable_and_distinguishes_source_references(self):
        first = event_id("app-1", "submitted", "2026-08-20T00:00:00Z", "source-1")
        self.assertEqual(
            first,
            event_id("app-1", "submitted", "2026-08-20T00:00:00Z", "source-1"),
        )
        self.assertNotEqual(
            first,
            event_id("app-1", "submitted", "2026-08-20T00:00:00Z", "source-2"),
        )

    def test_merge_is_idempotent_by_event_id(self):
        event = {
            "event_id": "evt-1",
            "application_id": "app-1",
            "occurred_at": "2026-08-20T00:00:00Z",
            "event_type": "submitted",
            "source": "tracker_backfill",
            "detail": "Submitted",
            "source_ref": "source-1",
            "created_at": "2026-08-24T00:00:00Z",
        }
        self.assertEqual(merge_events([event], [event], {"app-1"}), [event])

    def test_merge_rejects_unknown_application_id(self):
        event = {
            "event_id": "evt-1",
            "application_id": "missing",
            "occurred_at": "2026-08-20T00:00:00Z",
            "event_type": "submitted",
            "source": "tracker_backfill",
            "detail": "Submitted",
            "source_ref": "source-1",
            "created_at": "2026-08-24T00:00:00Z",
        }
        with self.assertRaisesRegex(ValueError, "unknown application_id"):
            merge_events([], [event], {"app-1"})


if __name__ == "__main__":
    unittest.main()
