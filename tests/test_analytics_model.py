import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from analytics.model import (
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    read_tracker_rows,
    stable_application_id,
    validate_tracker_rows,
    write_csv_atomic,
)


def tracker_row(**overrides):
    row = {column: "" for column in TRACKER_COLUMNS}
    row.update(
        application_id=stable_application_id(
            "2026-08-17", "Eigen Labs", "Senior Agentic AI Engineer"
        ),
        discovered_at="2026-08-17",
        company="Eigen Labs",
        role="Senior Agentic AI Engineer",
        role_family="applied_ai",
        geography="unknown",
        logistics_status="unknown",
        screening_decision="pending",
        stage="prospect",
        status="PROSPECT",
        status_updated_at="2026-08-17",
    )
    row.update(overrides)
    return row


class AnalyticsModelTests(unittest.TestCase):
    def test_tracker_schema_has_24_columns_with_deadline_last(self):
        self.assertEqual(len(TRACKER_COLUMNS), 24)
        self.assertEqual(TRACKER_COLUMNS[-1], "deadline")

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

    def test_tracker_validator_rejects_short_long_none_and_extra_header_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            cases = {
                "short": [*TRACKER_COLUMNS[:-1]],
                "long": [*TRACKER_COLUMNS, "unexpected"],
                "extra-header": [*TRACKER_COLUMNS, "extra_header"],
            }
            for name, header in cases.items():
                with self.subTest(name=name):
                    path = directory / f"{name}.csv"
                    row = tracker_row()
                    values = [row.get(column, "unexpected") for column in header]
                    if name == "short":
                        header = list(TRACKER_COLUMNS)
                        values = values[:-1]
                    with path.open("w", newline="", encoding="utf-8") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(header)
                        writer.writerow(values)
                    with self.assertRaisesRegex(ValueError, "tracker"):
                        read_tracker_rows(path)

        invalid = tracker_row()
        invalid["company"] = None
        with self.assertRaisesRegex(ValueError, "company"):
            validate_tracker_rows([invalid])

    def test_tracker_validator_rejects_header_order_domain_date_fit_and_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "reordered.csv"
            header = list(TRACKER_COLUMNS)
            header[0], header[1] = header[1], header[0]
            row = tracker_row()
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=header)
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(ValueError, "header"):
                read_tracker_rows(path)

        invalid_values = {
            "domain": {"stage": "mystery"},
            "date": {"discovered_at": "17-08-2026"},
            "fit": {"fit_score": "101"},
            "reason": {
                "screening_decision": "rejected",
                "screening_reason": "",
            },
        }
        for name, overrides in invalid_values.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    validate_tracker_rows([tracker_row(**overrides)])
    def test_tracker_validator_accepts_empty_or_iso_date_deadline_only(self):
        validate_tracker_rows([tracker_row(deadline="")])
        validate_tracker_rows([tracker_row(deadline="2026-09-30")])

        for invalid in ("30-09-2026", "2026-09-30T00:00:00Z", "2026-9-30"):
            with self.subTest(deadline=invalid):
                with self.assertRaisesRegex(ValueError, "deadline"):
                    validate_tracker_rows([tracker_row(deadline=invalid)])



if __name__ == "__main__":
    unittest.main()
