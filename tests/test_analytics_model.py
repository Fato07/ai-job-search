import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analytics.model import (
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    stable_application_id,
    write_csv_atomic,
)


class AnalyticsModelTests(unittest.TestCase):
    def test_application_id_is_stable_and_human_readable(self):
        first = stable_application_id(
            "2026-08-17", "Eigen Labs", "Senior Agentic AI Engineer"
        )
        second = stable_application_id(
            "2026-08-17", "Eigen Labs", "Senior Agentic AI Engineer"
        )
        self.assertEqual(first, second)
        self.assertRegex(
            first,
            r"^app-20260817-eigen-labs-senior-agentic-ai-engineer-[0-9a-f]{8}$",
        )

    def test_source_reference_is_sha256_and_does_not_leak_input(self):
        digest = hash_source_ref("gmail-message-id")
        self.assertEqual(len(digest), 64)
        self.assertNotIn("gmail-message-id", digest)

    def test_atomic_csv_round_trip_preserves_declared_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tracker.csv"
            row = {column: "" for column in TRACKER_COLUMNS}
            row.update(
                application_id="app-1",
                discovered_at="2026-08-17",
                company="Eigen Labs",
                role="Senior Agentic AI Engineer",
            )
            write_csv_atomic(path, TRACKER_COLUMNS, [row])
            self.assertEqual(read_csv_rows(path, {"application_id"}), [row])
            self.assertFalse(path.with_suffix(".csv.tmp").exists())

    def test_atomic_csv_failure_preserves_destination_and_removes_temporary_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            path = directory / "tracker.csv"
            original = b"existing tracker\n"
            path.write_bytes(original)
            entries_before = {entry.name for entry in directory.iterdir()}
            row = {column: "" for column in TRACKER_COLUMNS}

            with patch(
                "analytics.model.os.replace", side_effect=OSError("replace failed")
            ):
                with self.assertRaisesRegex(OSError, "replace failed"):
                    write_csv_atomic(path, TRACKER_COLUMNS, [row])

            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(
                {entry.name for entry in directory.iterdir()}, entries_before
            )


if __name__ == "__main__":
    unittest.main()
