import unittest
from pathlib import Path

from analytics.migrate import migrate_rows
from analytics.model import read_csv_rows

FIXTURES = Path("tests/fixtures/job_analytics")


class TrackerMigrationTests(unittest.TestCase):
    def test_migration_preserves_rows_and_normalizes_fields(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date", "fit_rating"})
        migrated = migrate_rows(legacy)
        self.assertEqual(len(migrated), len(legacy))
        self.assertEqual(migrated[0]["discovered_at"], legacy[0]["date"])
        self.assertEqual(migrated[0]["fit_score"], "92")
        self.assertEqual(migrated[0]["fit_label"], "Strong")
        self.assertEqual(migrated[0]["role_family"], "forward_deployed")
        self.assertEqual(migrated[1]["stage"], "closed")
        self.assertEqual(migrated[2]["screening_decision"], "rejected")
        self.assertEqual(len({row["application_id"] for row in migrated}), len(migrated))

    def test_migration_is_idempotent(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date"})
        once = migrate_rows(legacy)
        twice = migrate_rows(once)
        self.assertEqual(once, twice)

    def test_migration_preserves_nonstandard_fit_rating_text(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date"})
        legacy[0]["fit_rating"] = "Technical 91/100, logistics fail"
        migrated = migrate_rows([legacy[0]])
        self.assertEqual(migrated[0]["fit_score"], "")
        self.assertEqual(
            migrated[0]["fit_label"], "Technical 91/100, logistics fail"
        )

    def test_closed_unsubmitted_record_remains_rejected_and_unsubmitted(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date"})
        legacy[0]["status"] = "CLOSED 2026-08-05 - NOT SUBMITTED 2026-08-01"
        migrated = migrate_rows([legacy[0]])
        self.assertEqual(migrated[0]["stage"], "closed")
        self.assertEqual(migrated[0]["screening_decision"], "rejected")
        self.assertEqual(migrated[0]["submitted_at"], "")
        self.assertEqual(migrated[0]["status_updated_at"], "2026-08-05")

    def test_migration_redacts_personal_addresses_from_contact_and_notes(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date"})
        legacy[0]["contact_person"] = "Recruiter <person@example.com>"
        legacy[0]["notes"] = (
            "Confirmation received from person@example.com and "
            "backup@example.org."
        )

        migrated = migrate_rows([legacy[0]])[0]

        self.assertEqual(
            migrated["contact_person"],
            "Recruiter <[address removed]>",
        )
        self.assertEqual(
            migrated["notes"],
            (
                "Confirmation received from [address removed] and "
                "[address removed]."
            ),
        )
