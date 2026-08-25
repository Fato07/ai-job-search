import csv
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from analytics.init import initialize
from analytics.model import (
    EVENT_COLUMNS, TRACKER_COLUMNS, read_csv_rows, read_tracker_rows,
    write_csv_atomic,
)
from analytics.record import record_draft, record_transition
from dashboard.build import build_snapshot


class InitTests(unittest.TestCase):
    def root(self, tmp):
        root = Path(tmp)
        (root / "analytics").mkdir()
        (root / "analytics/config.example.json").write_text(
            '{"gmail_account_alias":"CHANGE_ME","gmail_expected_address":"candidate@example.test","reporting_timezone":"UTC"}\n',
            encoding="utf-8",
        )
        return root

    def test_fresh_init_is_complete_and_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            first = initialize(root, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            before = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            second = initialize(root, now=datetime(2026, 8, 26, tzinfo=timezone.utc))
            after = {path: path.read_bytes() for path in root.rglob("*") if path.is_file()}
            self.assertEqual(first, second)
            self.assertEqual(before, after)
            self.assertEqual(read_tracker_rows(root / "job_search_tracker.csv"), [])

    def test_init_migrates_legacy_and_backfills_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            (root / "job_search_tracker.csv").write_text(
                "date,company,sector,role,role_type,channel,status,contact_person,fit_rating,notes,cv_file,cover_letter_file,source,deadline\n"
                "2026-08-01,Example Co,AI,Applied AI Engineer,Full-time,online,SUBMITTED 2026-08-02,,88/100 Strong,,,,https://jobs.example.test/1,2026-09-30\n",
                encoding="utf-8",
            )
            summary = initialize(root, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            self.assertEqual(summary["applications"], 1)
            self.assertGreater(summary["events"], 0)
            self.assertEqual(read_tracker_rows(root / "job_search_tracker.csv")[0]["deadline"], "2026-09-30")

    def test_init_fails_on_malformed_existing_state_without_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            initialize(root)
            (root / "analytics/application_events.csv").write_text(
                "bad,header\n1,2\n", encoding="utf-8"
            )
            before = {
                path: path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and path.name != ".mutation.lock"
            }
            with self.assertRaises(ValueError):
                initialize(root)
            after = {
                path: path.read_bytes()
                for path in root.rglob("*")
                if path.is_file() and path.name != ".mutation.lock"
            }
            self.assertEqual(after, before)
    def test_invalid_canonical_tracker_never_falls_back_to_migration_or_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            row = {column: "" for column in TRACKER_COLUMNS}
            row.update(
                application_id="app-invalid",
                discovered_at="2026-08-25",
                company="Example",
                role="Engineer",
                role_family="other",
                geography="unknown",
                logistics_status="unknown",
                screening_decision="pending",
                stage="invalid",
                status="drafted",
                status_updated_at="2026-08-25",
            )
            write_csv_atomic(root / "job_search_tracker.csv", TRACKER_COLUMNS, [row])
            before = (root / "job_search_tracker.csv").read_bytes()
            with self.assertRaisesRegex(ValueError, "stage"):
                initialize(root)
            self.assertEqual((root / "job_search_tracker.csv").read_bytes(), before)
            self.assertFalse((root / "analytics/config.json").exists())
            self.assertFalse((root / "analytics/application_events.csv").exists())


    def test_init_commit_interruption_leaves_no_partial_destinations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.root(tmp)
            with patch("analytics.init.commit_staged_files", side_effect=OSError("stop")):
                with self.assertRaisesRegex(OSError, "stop"):
                    initialize(root)
            self.assertFalse((root / "job_search_tracker.csv").exists())
            self.assertFalse((root / "analytics/config.json").exists())
            self.assertFalse((root / "analytics/application_events.csv").exists())


class RecordTests(unittest.TestCase):
    def setUpRoot(self, tmp):
        root = InitTests().root(tmp)
        initialize(root, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
        return root

    def test_draft_and_outcomes_update_tracker_and_events_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.setUpRoot(tmp)
            app_id = record_draft(
                root,
                {
                    "discovered_at": "2026-08-25",
                    "company": "Example Co",
                    "role": "Applied AI Engineer",
                    "role_family": "applied_ai",
                    "geography": "EEA",
                    "logistics_status": "pass",
                    "deadline": "2026-09-30",
                },
            )
            record_transition(root, app_id, "submitted", "2026-08-26")
            record_transition(root, app_id, "interview", "2026-09-02")
            record_transition(root, app_id, "offer", "2026-09-10")
            row = read_tracker_rows(root / "job_search_tracker.csv")[0]
            events = read_csv_rows(root / "analytics/application_events.csv", EVENT_COLUMNS)
            self.assertEqual((row["stage"], row["status"]), ("offer", "offer"))
            self.assertEqual(row["submitted_at"], "2026-08-26")
            self.assertEqual(row["deadline"], "2026-09-30")
            self.assertEqual(
                {item["event_type"] for item in events},
                {"discovered", "drafting", "submitted", "interview", "offer"},
            )
            snapshot = build_snapshot(
                [row],
                events,
                [],
                [],
                [],
                {"reporting_timezone": "UTC"},
                date(2026, 9, 10),
            )
            self.assertEqual(snapshot["funnel"]["submitted"], 1)
            self.assertEqual(snapshot["funnel"]["interviewed"], 1)
            self.assertEqual(snapshot["funnel"]["offered"], 1)
    def test_terminal_outcomes_have_canonical_events_and_dashboard_semantics(self):
        cases = (
            ("hired", True, True),
            ("offer_declined", True, True),
            ("no_response", False, False),
        )
        for terminal, responded, offered in cases:
            with self.subTest(terminal=terminal), tempfile.TemporaryDirectory() as tmp:
                root = self.setUpRoot(tmp)
                app_id = record_draft(
                    root,
                    {
                        "discovered_at": "2026-08-25",
                        "company": f"Example {terminal}",
                        "role": "Engineer",
                    },
                )
                record_transition(root, app_id, "submitted", "2026-08-26")
                if terminal != "no_response":
                    record_transition(root, app_id, "offer", "2026-09-01")
                record_transition(root, app_id, terminal, "2026-09-02")
                row = read_tracker_rows(root / "job_search_tracker.csv")[0]
                events = read_csv_rows(root / "analytics/application_events.csv", EVENT_COLUMNS)
                snapshot = build_snapshot(
                    [row], events, [], [], [], {"reporting_timezone": "UTC"}, date(2026, 9, 2)
                )
                self.assertEqual((row["stage"], row["status"]), ("closed", terminal))
                self.assertIn(terminal, {event["event_type"] for event in events})
                self.assertEqual(bool(snapshot["funnel"]["responded"]), responded)
                self.assertEqual(bool(snapshot["funnel"]["offered"]), offered)

    def test_redraft_merges_allowed_fields_without_moving_lifecycle_backwards(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.setUpRoot(tmp)
            values = {
                "discovered_at": "2026-08-25",
                "company": "Example Merge",
                "role": "Engineer",
                "deadline": "2026-09-30",
                "cv_file": "cv/first.tex",
            }
            app_id = record_draft(root, values)
            record_transition(root, app_id, "submitted", "2026-08-26")
            record_draft(
                root,
                {
                    **values,
                    "deadline": "",
                    "cv_file": "cv/revised.tex",
                    "fit_score": "91",
                    "notes": "reviewed",
                },
            )
            row = read_tracker_rows(root / "job_search_tracker.csv")[0]
            self.assertEqual((row["stage"], row["status"]), ("submitted", "applied"))
            self.assertEqual(row["submitted_at"], "2026-08-26")
            self.assertEqual(row["deadline"], "2026-09-30")
            self.assertEqual(row["cv_file"], "cv/revised.tex")
            self.assertEqual(row["fit_score"], "91")
            self.assertIn("redrafted", row["notes"])
            record_transition(root, app_id, "rejected", "2026-09-03")
            with self.assertRaisesRegex(ValueError, "closed"):
                record_draft(root, values)


    def test_draft_is_idempotent_and_malformed_tracker_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self.setUpRoot(tmp)
            values = {"discovered_at": "2026-08-25", "company": "Example Co", "role": "Engineer"}
            first = record_draft(root, values)
            second = record_draft(root, values)
            self.assertEqual(first, second)
            self.assertEqual(len(read_tracker_rows(root / "job_search_tracker.csv")), 1)
            (root / "job_search_tracker.csv").write_text("bad\nvalue\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                record_transition(root, first, "rejected", "2026-08-30")


if __name__ == "__main__":
    unittest.main()
