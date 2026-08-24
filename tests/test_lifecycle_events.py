import unittest
from datetime import datetime, timezone

from analytics.events import backfill_events, event_id, merge_events


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
