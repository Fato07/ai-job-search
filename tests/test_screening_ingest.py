import csv
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import analytics.screening as screening
from analytics.model import (
    EVENT_COLUMNS,
    TRACKER_COLUMNS,
    hash_source_ref,
    redact_email_addresses,
)
from analytics.screening import (
    SCREENING_COLUMNS,
    evaluate_hard_gates,
    ingest_screening_rows,
)


NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)
CONFIG = {"max_active_applications_per_company": 2}
EXPECTED_SCREENING_COLUMNS = (
    "discovered_at",
    "company",
    "sector",
    "role",
    "role_family",
    "role_type",
    "geography",
    "logistics_status",
    "channel",
    "screening_decision",
    "screening_reason",
    "fit_score",
    "fit_label",
    "source",
    "deadline",
)


def candidate(**overrides):
    row = {
        "discovered_at": "2026-08-24",
        "company": "TestCo",
        "sector": "AI",
        "role": "Applied AI Engineer",
        "role_family": "applied_ai",
        "role_type": "Full-time",
        "geography": "Remote Europe",
        "logistics_status": "pass",
        "channel": "Careers",
        "screening_decision": "qualified",
        "screening_reason": "strong fit",
        "fit_score": "90",
        "fit_label": "Strong",
        "source": "https://jobs.test/1",
        "deadline": "2026-09-30",
    }
    row.update(overrides)
    return row


def active_application(index, company="TestCo"):
    return {
        "application_id": f"app-{index}",
        "company": company,
        "role": f"Existing Role {index}",
        "source": f"https://jobs.test/existing-{index}",
        "stage": "submitted",
        "submitted_at": "2026-08-20",
    }


class ScreeningIngestTests(unittest.TestCase):
    def test_screening_header_is_exact(self):
        self.assertEqual(SCREENING_COLUMNS, EXPECTED_SCREENING_COLUMNS)

    def test_duplicate_source_url_does_not_create_second_application(self):
        existing = [{"application_id": "app-1", "source": "https://jobs.test/1"}]
        applications, events, summary = ingest_screening_rows(
            [candidate()], existing, [], NOW, config=CONFIG
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(events, [])

    def test_canonical_url_and_company_role_duplicates_are_not_bypassable(self):
        existing = [
            {
                "application_id": "app-1",
                "company": "Test Co",
                "role": "Applied-AI Engineer",
                "source": "https://JOBS.test/roles/1/?utm_source=feed#apply",
                "stage": "prospect",
            }
        ]
        rows = [
            candidate(
                source="https://jobs.test/roles/1",
                role="Different Role",
                screening_reason="strategic_override: source exception",
            ),
            candidate(
                company=" test co ",
                role="Applied AI Engineer",
                source="https://jobs.test/roles/2",
                screening_reason="strategic_override: duplicate exception",
            ),
        ]
        applications, events, summary = ingest_screening_rows(
            rows, existing, [], NOW, config=CONFIG
        )
        self.assertEqual(applications, existing)
        self.assertEqual(events, [])
        self.assertEqual(summary.duplicates, 2)

    def test_qualified_candidate_creates_no_submitted_event(self):
        applications, events, summary = ingest_screening_rows(
            [candidate()], [], [], NOW, config=CONFIG
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(set(applications[0]), set(TRACKER_COLUMNS))
        self.assertEqual(applications[0]["screening_decision"], "qualified")
        self.assertEqual(applications[0]["stage"], "qualified")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["discovered", "qualified", "screened"],
        )
        self.assertNotIn("submitted", {event["event_type"] for event in events})
        self.assertTrue(all(set(event) == set(EVENT_COLUMNS) for event in events))
        self.assertTrue(all(event["source"] == "workflow" for event in events))
        self.assertEqual(summary.imported, 1)
        self.assertEqual(summary.qualified, 1)
    def test_screening_deadline_is_imported_and_must_be_iso_date(self):
        applications, _, _ = ingest_screening_rows(
            [candidate()], [], [], NOW, config=CONFIG
        )
        self.assertEqual(applications[0]["deadline"], "2026-09-30")

        with self.assertRaisesRegex(ValueError, "deadline"):
            ingest_screening_rows(
                [candidate(deadline="September 30")], [], [], NOW, config=CONFIG
            )

    def test_screening_reason_is_redacted_bounded_before_event_identity(self):
        suffix = " " + "x" * 320

        def import_reason(address):
            reason = f"Contact {address} about the rejection." + suffix
            applications, events, _ = ingest_screening_rows(
                [
                    candidate(
                        screening_decision="rejected",
                        screening_reason=reason,
                    )
                ],
                [],
                [],
                NOW,
                config=CONFIG,
            )
            decision_event = next(
                event for event in events if event["event_type"] == "rejected"
            )
            return applications[0], decision_event, reason

        first_application, first_event, raw_reason = import_reason(
            "candidate@example.com"
        )
        _, second_event, _ = import_reason("other.person@example.org")
        safe_reason = redact_email_addresses(raw_reason, 280)
        expected_source_ref = hash_source_ref(
            f"{first_application['application_id']}\x1f"
            f"screening:{first_application['source']}\x1f{safe_reason}"
        )

        self.assertEqual(first_event["detail"], safe_reason)
        self.assertEqual(len(first_event["detail"]), 280)
        self.assertNotIn("@", first_event["detail"])
        self.assertEqual(first_event["source_ref"], expected_source_ref)
        self.assertEqual(first_event["source_ref"], second_event["source_ref"])
        self.assertEqual(first_event["event_id"], second_event["event_id"])


    def test_missing_geography_and_logistics_normalize_to_unknown(self):
        applications, _, _ = ingest_screening_rows(
            [candidate(geography="", logistics_status="")],
            [],
            [],
            NOW,
            config=CONFIG,
        )
        self.assertEqual(applications[0]["geography"], "unknown")
        self.assertEqual(applications[0]["logistics_status"], "unknown")

    def test_invalid_screening_decision_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "invalid screening_decision"):
            ingest_screening_rows(
                [candidate(screening_decision="maybe")], [], [], NOW, config=CONFIG
            )

    def test_unverified_source_forces_qualified_decision_to_rejected(self):
        for source in ("", "jobs.test/1"):
            with self.subTest(source=source):
                applications, events, summary = ingest_screening_rows(
                    [
                        candidate(
                            source=source,
                            screening_reason="strategic_override: trust this source",
                        )
                    ],
                    [],
                    [],
                    NOW,
                    config=CONFIG,
                )
                self.assertEqual(applications[0]["screening_decision"], "rejected")
                self.assertEqual(
                    applications[0]["screening_reason"], "hard_gate:source_unverified"
                )
                self.assertEqual(summary.rejected, 1)
                self.assertIn("rejected", {event["event_type"] for event in events})
                self.assertNotIn("qualified", {event["event_type"] for event in events})

    def test_blocked_logistics_is_never_bypassable(self):
        row = candidate(
            logistics_status="blocked",
            screening_reason="strategic_override: relocation is worthwhile",
        )
        applications, events, summary = ingest_screening_rows(
            [row], [], [], NOW, config=CONFIG
        )
        self.assertEqual(applications[0]["screening_decision"], "rejected")
        self.assertEqual(
            applications[0]["screening_reason"], "hard_gate:logistics_blocked"
        )
        self.assertEqual(summary.rejected, 1)
        self.assertNotIn("qualified", {event["event_type"] for event in events})

    def test_other_role_family_requires_strategic_override(self):
        failed = evaluate_hard_gates(
            candidate(role_family="other"), [], CONFIG
        )
        passed = evaluate_hard_gates(
            candidate(
                role_family="other",
                screening_reason="strategic_override: exceptional developer tooling role",
            ),
            [],
            CONFIG,
        )
        self.assertFalse(failed.passed)
        self.assertEqual(failed.reason, "hard_gate:role_family_other")
        applications, _, summary = ingest_screening_rows(
            [candidate(role_family="other")], [], [], NOW, config=CONFIG
        )
        self.assertEqual(applications[0]["screening_decision"], "rejected")
        self.assertEqual(
            applications[0]["screening_reason"], "hard_gate:role_family_other"
        )
        self.assertEqual(summary.rejected, 1)
        self.assertTrue(passed.passed)
        self.assertEqual(passed.reason, "")

    def test_blank_and_unknown_role_families_require_strategic_override(self):
        for role_family in ("", "finance"):
            with self.subTest(role_family=role_family):
                applications, _, summary = ingest_screening_rows(
                    [candidate(role_family=role_family)], [], [], NOW, config=CONFIG
                )
                self.assertEqual(
                    applications[0]["screening_decision"], "rejected"
                )
                self.assertEqual(
                    applications[0]["screening_reason"],
                    "hard_gate:role_family_not_approved",
                )
                self.assertEqual(summary.rejected, 1)

                gate = evaluate_hard_gates(
                    candidate(
                        role_family=role_family,
                        screening_reason="strategic_override: adjacent strategic role",
                    ),
                    [],
                    CONFIG,
                )
                self.assertTrue(gate.passed)

    def test_company_cap_requires_strategic_override(self):
        existing = [active_application(1), active_application(2)]
        applications, _, summary = ingest_screening_rows(
            [candidate()], existing, [], NOW, config=CONFIG
        )
        self.assertEqual(applications[-1]["screening_decision"], "rejected")
        self.assertEqual(
            applications[-1]["screening_reason"],
            "hard_gate:company_active_application_cap",
        )
        self.assertEqual(summary.rejected, 1)

        overridden, events, overridden_summary = ingest_screening_rows(
            [
                candidate(
                    source="https://jobs.test/override",
                    role="AI Platform Engineer",
                    screening_reason="strategic_override: uniquely strategic company role",
                )
            ],
            existing,
            [],
            NOW,
            config=CONFIG,
        )
        self.assertEqual(overridden[-1]["screening_decision"], "qualified")
        self.assertEqual(overridden_summary.qualified, 1)
        self.assertNotIn("submitted", {event["event_type"] for event in events})

    def test_closed_company_application_does_not_count_toward_cap(self):
        existing = [active_application(1), {**active_application(2), "stage": "closed"}]
        applications, _, summary = ingest_screening_rows(
            [candidate()], existing, [], NOW, config=CONFIG
        )
        self.assertEqual(applications[-1]["screening_decision"], "qualified")
        self.assertEqual(summary.qualified, 1)

    def test_cli_updates_tracker_and_event_ledger(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "screening.csv"
            tracker_path = root / "tracker.csv"
            events_path = root / "events.csv"
            for path, columns, rows in (
                (input_path, SCREENING_COLUMNS, [candidate()]),
                (tracker_path, TRACKER_COLUMNS, []),
                (events_path, EVENT_COLUMNS, []),
            ):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(rows)

            result = subprocess.run(
                (
                    sys.executable,
                    "-m",
                    "analytics.screening",
                    str(input_path),
                    "--tracker",
                    str(tracker_path),
                    "--events",
                    str(events_path),
                ),
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                json.loads(result.stdout),
                {"duplicates": 0, "imported": 1, "qualified": 1, "rejected": 0},
            )
            with tracker_path.open(newline="", encoding="utf-8") as handle:
                applications = list(csv.DictReader(handle))
            with events_path.open(newline="", encoding="utf-8") as handle:
                events = list(csv.DictReader(handle))
            self.assertEqual(len(applications), 1)
            self.assertEqual(
                {event["event_type"] for event in events},
                {"discovered", "screened", "qualified"},
            )
            self.assertNotIn("submitted", {event["event_type"] for event in events})

    def test_cli_rolls_back_both_ledgers_when_second_replace_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "screening.csv"
            tracker_path = root / "tracker.csv"
            events_path = root / "events.csv"
            for path, columns, rows in (
                (input_path, SCREENING_COLUMNS, [candidate()]),
                (tracker_path, TRACKER_COLUMNS, []),
                (events_path, EVENT_COLUMNS, []),
            ):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(rows)
            original_tracker = tracker_path.read_bytes()
            original_events = events_path.read_bytes()
            real_replace = os.replace

            def fail_second_destination(source, destination):
                if (
                    Path(destination) == events_path
                    and Path(source).name.startswith(".screening-stage-")
                ):
                    raise OSError("forced second destination failure")
                return real_replace(source, destination)

            argv = [
                "analytics.screening",
                str(input_path),
                "--tracker",
                str(tracker_path),
                "--events",
                str(events_path),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                screening.os, "replace", side_effect=fail_second_destination
            ):
                with self.assertRaisesRegex(OSError, "forced second destination"):
                    screening.main()

            self.assertEqual(tracker_path.read_bytes(), original_tracker)
            self.assertEqual(events_path.read_bytes(), original_events)
            self.assertEqual(list(root.glob("*.screening-transaction.json")), [])

    def test_cli_recovers_interrupted_transaction_on_next_invocation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "screening.csv"
            tracker_path = root / "tracker.csv"
            events_path = root / "events.csv"
            for path, columns, rows in (
                (input_path, SCREENING_COLUMNS, [candidate()]),
                (tracker_path, TRACKER_COLUMNS, []),
                (events_path, EVENT_COLUMNS, []),
            ):
                with path.open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle, fieldnames=columns)
                    writer.writeheader()
                    writer.writerows(rows)
            original_events = events_path.read_bytes()
            real_replace = os.replace

            def interrupt_after_first_destination(source, destination):
                if (
                    Path(destination) == tracker_path
                    and Path(source).name.startswith(".screening-stage-")
                ):
                    real_replace(source, destination)
                    raise KeyboardInterrupt
                return real_replace(source, destination)

            argv = [
                "analytics.screening",
                str(input_path),
                "--tracker",
                str(tracker_path),
                "--events",
                str(events_path),
            ]
            with patch.object(sys, "argv", argv), patch.object(
                screening.os, "replace", side_effect=interrupt_after_first_destination
            ):
                with self.assertRaises(KeyboardInterrupt):
                    screening.main()
            self.assertEqual(events_path.read_bytes(), original_events)
            self.assertTrue(list(root.glob("*.screening-transaction.json")))

            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                screening.main()
            with tracker_path.open(newline="", encoding="utf-8") as handle:
                applications = list(csv.DictReader(handle))
            with events_path.open(newline="", encoding="utf-8") as handle:
                events = list(csv.DictReader(handle))
            self.assertEqual(len(applications), 1)
            self.assertEqual(len(events), 3)
            self.assertEqual(list(root.glob("*.screening-transaction.json")), [])

    def test_screening_transaction_rejects_malformed_complete_event_rows(self):
        applications, events, _ = ingest_screening_rows(
            [candidate()], [], [], NOW, config=CONFIG
        )
        malformed_events = [
            {
                **event,
                "detail": (
                    "candidate@example.com"
                    if event["event_type"] == "qualified"
                    else event["detail"]
                ),
            }
            for event in events
        ]

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            tracker_path = root / "tracker.csv"
            events_path = root / "events.csv"
            with self.assertRaisesRegex(ValueError, "email address"):
                screening._write_ledgers_transaction(
                    tracker_path,
                    applications,
                    events_path,
                    malformed_events,
                )
            self.assertFalse(tracker_path.exists())
            self.assertFalse(events_path.exists())


if __name__ == "__main__":
    unittest.main()
