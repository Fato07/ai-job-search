import hashlib
import json
import os
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from analytics.gmail_sync import (
    EXPECTED_MAILBOX,
    ComposioError,
    MailboxDiscovery,
    SyncProposal,
    _scan_queries,
    classify_message,
)
from analytics.model import (
    EVENT_COLUMNS,
    FEEDBACK_COLUMNS,
    REVIEW_COLUMNS,
    TRACKER_COLUMNS,
    read_csv_rows,
    write_csv_atomic,
)
from analytics.refresh import RefreshPaths, refresh

FIXED_NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


class RecordingMailboxClient:
    account = "job-search"

    def __init__(self, messages):
        self.messages = {message["messageId"]: dict(message) for message in messages}
        self.calls = []

    def execute(self, slug, data):
        self.calls.append((slug, dict(data)))
        if slug == "GMAIL_GET_PROFILE":
            return {"data": {"emailAddress": EXPECTED_MAILBOX}}
        if slug == "GMAIL_FETCH_EMAILS":
            return {"data": {"messages": [dict(message) for message in self.messages.values()]}}
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            return {"data": dict(self.messages[data["message_id"]])}
        raise AssertionError(f"unexpected Composio tool: {slug}")


class ConcurrentMailboxClient(RecordingMailboxClient):
    def __init__(self, messages):
        super().__init__(messages)
        self._barrier = threading.Barrier(2)
        self._lock = threading.Lock()
        self._active = 0
        self.peak_active = 0

    def execute(self, slug, data):
        if slug != "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            return super().execute(slug, data)
        with self._lock:
            self._active += 1
            self.peak_active = max(self.peak_active, self._active)
        try:
            try:
                self._barrier.wait(timeout=0.5)
            except threading.BrokenBarrierError:
                pass
            return super().execute(slug, data)
        finally:
            with self._lock:
                self._active -= 1



class PaginatedMailboxClient(RecordingMailboxClient):
    def __init__(self, pages):
        messages = [message for page, _ in pages for message in page]
        super().__init__(messages)
        self.pages = pages

    def execute(self, slug, data):
        if slug != "GMAIL_FETCH_EMAILS":
            return super().execute(slug, data)
        self.calls.append((slug, dict(data)))
        index = 1 if data.get("page_token") == "next-page" else 0
        messages, next_token = self.pages[index]
        return {
            "data": {
                "messages": [dict(message) for message in messages],
                "nextPageToken": next_token,
            }
        }


class MissingBulkBodyClient(ConcurrentMailboxClient):
    def __init__(self, messages, missing_ids):
        super().__init__(messages)
        self.missing_ids = frozenset(missing_ids)
        self.failed_once = set()
        self.retry_ids = set()

    def execute(self, slug, data):
        if slug == "GMAIL_FETCH_EMAILS":
            result = super().execute(slug, data)
            messages = result["data"]["messages"]
            for message in messages:
                if message["messageId"] in self.missing_ids:
                    message.pop("messageText", None)
            return result
        if (
            slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
            and data["message_id"] in self.retry_ids
            and data["message_id"] not in self.failed_once
        ):
            self.calls.append((slug, dict(data)))
            self.failed_once.add(data["message_id"])
            raise ComposioError("Composio command timed out")
        return super().execute(slug, data)


class FakeClock:
    def __init__(self):
        self.value = 0.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


class DeadlineMailboxClient(RecordingMailboxClient):
    def __init__(self, messages, clock):
        super().__init__(messages)
        self.clock = clock

    def execute(self, slug, data):
        result = super().execute(slug, data)
        if slug == "GMAIL_FETCH_EMAILS":
            self.clock.advance(301)
        return result


class EndlessPageMailboxClient(RecordingMailboxClient):
    def __init__(self):
        super().__init__([])
        self.page = 0

    def execute(self, slug, data):
        if slug != "GMAIL_FETCH_EMAILS":
            return super().execute(slug, data)
        self.calls.append((slug, dict(data)))
        self.page += 1
        return {
            "data": {
                "messages": [],
                "nextPageToken": f"page-{self.page}",
            }
        }


class UnavailableBodyMailboxClient(MissingBulkBodyClient):
    def execute(self, slug, data):
        result = super().execute(slug, data)
        if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID":
            result["data"].pop("messageText", None)
        return result

class AtomicRefreshTests(unittest.TestCase):
    def _paths(self, root):
        paths = RefreshPaths.for_root(root)
        application = {column: "" for column in TRACKER_COLUMNS}
        application.update(
            application_id="app-1",
            discovered_at="2026-08-10",
            company="TestCo",
            role="Applied AI Engineer",
            role_family="applied_ai",
            geography="EEA",
            role_type="Full-time",
            stage="prospect",
            status="PROSPECT",
        )
        write_csv_atomic(paths.tracker, TRACKER_COLUMNS, [application])
        write_csv_atomic(paths.events, EVENT_COLUMNS, [])
        write_csv_atomic(paths.feedback, FEEDBACK_COLUMNS, [])
        write_csv_atomic(paths.review, REVIEW_COLUMNS, [])
        paths.rules.write_text("[]\n", encoding="utf-8")
        paths.checkpoint.write_text(
            json.dumps({"last_successful_at": None}) + "\n",
            encoding="utf-8",
        )
        return paths

    @staticmethod
    def _snapshot(paths):
        return {path: path.read_bytes() for path in paths.mutable_files()}

    @staticmethod
    def _message(message_id, *, body, subject="Application update", role="Applied AI Engineer"):
        return {
            "messageId": message_id,
            "subject": subject,
            "sender": "TestCo Talent",
            "messageTimestamp": "2026-08-24T10:00:00Z",
            "messageText": body,
            "company": "TestCo",
            "role": role,
        }

    def test_paths_use_the_canonical_mutable_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            paths = RefreshPaths.for_root(root)
            self.assertEqual(paths.root, root)
            self.assertEqual(paths.tracker, root / "job_search_tracker.csv")
            self.assertEqual(paths.events, root / "analytics/application_events.csv")
            self.assertEqual(paths.feedback, root / "analytics/application_feedback.csv")
            self.assertEqual(paths.rules, root / "analytics/feedback_rules.json")
            self.assertEqual(paths.review, root / "analytics/reconciliation_review.csv")
            self.assertEqual(paths.checkpoint, root / "analytics/gmail_checkpoint.json")
            self.assertEqual(len(paths.mutable_files()), 6)
            with self.assertRaises(FrozenInstanceError):
                paths.tracker = root / "other.csv"

    def test_failure_keeps_every_original_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before = self._snapshot(paths)
            message = self._message(
                "validation-change-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )

            with patch(
                "analytics.refresh.validate_staged_files",
                side_effect=ValueError("bad"),
            ):
                with self.assertRaisesRegex(ValueError, "bad"):
                    refresh(
                        paths,
                        client=RecordingMailboxClient([message]),
                        sync_gmail=True,
                        now=FIXED_NOW,
                    )

            self.assertEqual(self._snapshot(paths), before)

    def test_replace_failure_rolls_back_every_mutable_original_byte_for_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before = self._snapshot(paths)
            message = self._message(
                "rollback-change-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            real_replace = os.replace
            destinations = {path.resolve() for path in paths.mutable_files()}
            replaced = 0

            def fail_after_one_destination(source, destination):
                nonlocal replaced
                destination = Path(destination)
                if destination in destinations:
                    replaced += 1
                    if replaced == 2:
                        raise OSError("injected replace failure")
                return real_replace(source, destination)

            with patch("analytics.transaction.os.replace", side_effect=fail_after_one_destination):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    refresh(
                        paths,
                        client=RecordingMailboxClient([message]),
                        sync_gmail=True,
                        now=FIXED_NOW,
                    )
            self.assertEqual(self._snapshot(paths), before)
            self.assertFalse(paths.journal.exists())


    def test_unsupported_immutable_proposal_leaves_every_original_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before = self._snapshot(paths)
            invalid = MailboxDiscovery(
                proposal=SyncProposal(
                    (),
                    (),
                    ({"application_id": "app-1", "notes": "unsupported"},),
                    (),
                    {"last_successful_at": "2026-08-24T00:00:00Z"},
                ),
                scanned=1,
                matched=1,
            )

            with patch("analytics.refresh.discover_mailbox", return_value=invalid):
                with self.assertRaisesRegex(ValueError, "unsupported"):
                    refresh(
                        paths,
                        client=RecordingMailboxClient([]),
                        sync_gmail=True,
                        now=FIXED_NOW,
                    )

            self.assertEqual(self._snapshot(paths), before)
            self.assertFalse(paths.journal.exists())

    def test_interruption_is_recovered_before_next_refresh_reads_ledgers(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before = self._snapshot(paths)
            message = self._message(
                "recovery-change-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            real_replace = os.replace
            destinations = {path.resolve() for path in paths.mutable_files()}
            replaced = 0

            def interrupt_after_one_destination(source, destination):
                nonlocal replaced
                destination = Path(destination)
                if destination in destinations:
                    replaced += 1
                    if replaced == 2:
                        raise KeyboardInterrupt()
                return real_replace(source, destination)

            with patch("analytics.transaction.os.replace", side_effect=interrupt_after_one_destination):
                with self.assertRaises(KeyboardInterrupt):
                    refresh(
                        paths,
                        client=RecordingMailboxClient([message]),
                        sync_gmail=True,
                        now=FIXED_NOW,
                    )
            self.assertTrue(paths.journal.exists())

            with patch(
                "analytics.refresh.validate_staged_files",
                side_effect=ValueError("stop after recovery"),
            ):
                with self.assertRaisesRegex(ValueError, "stop after recovery"):
                    refresh(paths, client=None, sync_gmail=False, now=FIXED_NOW)

            self.assertEqual(self._snapshot(paths), before)
            self.assertFalse(paths.journal.exists())

    def test_dry_run_with_interrupted_journal_changes_no_transaction_byte(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            message = self._message(
                "dry-recovery-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            real_replace = os.replace
            destinations = {path.resolve() for path in paths.mutable_files()}
            replaced = 0

            def interrupt_after_one_destination(source, destination):
                nonlocal replaced
                destination = Path(destination)
                if destination in destinations:
                    replaced += 1
                    if replaced == 2:
                        raise KeyboardInterrupt()
                return real_replace(source, destination)

            with patch(
                "analytics.transaction.os.replace",
                side_effect=interrupt_after_one_destination,
            ):
                with self.assertRaises(KeyboardInterrupt):
                    refresh(
                        paths,
                        client=RecordingMailboxClient([message]),
                        sync_gmail=True,
                        now=FIXED_NOW,
                    )
            journal = json.loads(paths.journal.read_text(encoding="utf-8"))
            transaction_paths = [*paths.mutable_files(), paths.journal]
            transaction_paths.extend(
                Path(entry[key])
                for entry in journal["entries"]
                for key in ("backup", "staged")
                if entry.get(key) and Path(entry[key]).exists()
            )
            before = {path: path.read_bytes() for path in transaction_paths}

            with self.assertRaisesRegex(RuntimeError, "recovery required"):
                refresh(
                    paths,
                    client=None,
                    sync_gmail=False,
                    now=FIXED_NOW,
                    dry_run=True,
                )

            self.assertEqual(
                {path: path.read_bytes() for path in transaction_paths},
                before,
            )

    def test_discovery_queries_cover_classifier_ats_and_tracked_company_paths(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-08-10",
            "company": "TestCo",
        }
        queries = _scan_queries([application], {"last_successful_at": None})
        combined = " ".join(queries).casefold()
        supported = (
            ("We decided not to move forward.", "decided not to move forward"),
            ("We have decided not to proceed.", "decided not to proceed"),
            (
                "We have chosen to move forward with other candidates.",
                "move forward with other candidates",
            ),
            ("We will not progress with your application.", "will not progress"),
            ("Your application was not selected.", "application was not selected"),
            ("We received your application.", "received your application"),
            ("Thank you for applying.", "thank you for applying"),
            ("Interview invitation.", "interview invitation"),
            ("Please schedule your interview.", "schedule your interview"),
            ("We would like to speak with you.", "would like to speak with you"),
        )
        for body, query_term in supported:
            message = self._message("coverage-id", body=body)
            self.assertIsNotNone(classify_message(message), body)
            self.assertIn(query_term, combined)
        self.assertIn("from:ashby", combined)
        self.assertIn('"testco"', combined)
        for subject_term in (
            "your application",
            "application status",
            "thanks for your interest",
            "thank you for applying",
            "job application",
            "application at",
            "application to",
            "next steps",
            "interview",
            "assessment",
            "offer",
        ):
            self.assertIn(f"subject:{subject_term}", combined.replace('"', ""))
        self.assertIn("-from:jobalerts-noreply@linkedin.com", combined)


    def test_union_queries_deduplicate_the_same_hashed_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            message = self._message(
                "union-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            client = RecordingMailboxClient([message])

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual((summary.scanned, summary.matched), (1, 1))
            self.assertGreater(
                sum(slug == "GMAIL_FETCH_EMAILS" for slug, _ in client.calls),
                1,
            )


    def test_first_scan_starts_at_earliest_discovery_and_fetches_candidates_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            relevant = self._message(
                "relevant-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            unrelated = self._message(
                "unrelated-id",
                body="Platform engineering weekly digest.",
                subject="Weekly engineering digest",
            )
            unrelated["sender"] = "Industry Brief"
            client = RecordingMailboxClient([relevant, unrelated])

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual((summary.scanned, summary.matched), (2, 1))
            self.assertEqual(client.calls[0], ("GMAIL_GET_PROFILE", {"user_id": "me"}))
            list_calls = [
                data for slug, data in client.calls if slug == "GMAIL_FETCH_EMAILS"
            ]
            self.assertGreater(len(list_calls), 1)
            self.assertTrue(list_calls[0]["query"].startswith("after:2026/08/09 "))
            for call in list_calls:
                self.assertEqual(call["max_results"], 500)
                self.assertTrue(call["verbose"])
                self.assertFalse(call["include_payload"])
            self.assertFalse(
                any(
                    slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
                    for slug, _ in client.calls
                )
            )

    def test_bulk_fetch_paginates_and_deduplicates_source_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            first = self._message(
                "first-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            second = self._message(
                "second-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            first["messageId"] = "message_id"
            first["display_url"] = "https://mail.google.com/mail/u/0/#inbox/source-one"
            second["messageId"] = "message_id"
            second["display_url"] = "https://mail.google.com/mail/u/0/#inbox/source-two"
            client = PaginatedMailboxClient(
                [([first], "next-page"), ([first, second], "")]
            )

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual((summary.scanned, summary.matched), (2, 2))
            list_calls = [
                data for slug, data in client.calls if slug == "GMAIL_FETCH_EMAILS"
            ]
            self.assertGreater(len(list_calls), 2)
            first_pages = [call for call in list_calls if "page_token" not in call]
            next_pages = [
                call for call in list_calls if call.get("page_token") == "next-page"
            ]
            self.assertEqual(len(first_pages), len(next_pages))
            self.assertGreater(len(first_pages), 1)
            self.assertFalse(
                any(
                    slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
                    for slug, _ in client.calls
                )
            )

    def test_only_missing_specific_feedback_bodies_use_bounded_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            messages = [
                self._message(
                    f"missing-id-{index}",
                    body=(
                        "We decided not to move forward. "
                        "This role cannot sponsor work authorization."
                    ),
                    subject="Application update",
                )
                for index in range(4)
            ]
            complete = self._message(
                "complete-id",
                body=(
                    "We decided not to move forward. "
                    "This role cannot sponsor work authorization."
                ),
                subject="We decided not to move forward",
            )
            client = MissingBulkBodyClient(
                [*messages, complete],
                {message["messageId"] for message in messages},
            )

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual(summary.matched, 5)
            fallback_ids = [
                data["message_id"]
                for slug, data in client.calls
                if slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
            ]
            self.assertCountEqual(
                fallback_ids, [message["messageId"] for message in messages]
            )
            self.assertGreater(client.peak_active, 1)
            self.assertLessEqual(client.peak_active, 4)

    def test_missing_body_fallback_retries_once_after_transient_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            message = self._message(
                "retry-id",
                body=(
                    "We decided not to move forward. "
                    "This role cannot sponsor work authorization."
                ),
                subject="We decided not to move forward",
            )
            client = MissingBulkBodyClient([message], {"retry-id"})
            client.retry_ids.add("retry-id")

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual(summary.matched, 1)
            self.assertEqual(
                sum(
                    slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
                    for slug, _ in client.calls
                ),
                2,
            )

    def test_missing_body_fallback_fails_before_exceeding_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            messages = [
                self._message(
                    f"bounded-id-{index}",
                    body="We decided not to move forward because we cannot sponsor a visa.",
                    subject="We decided not to move forward",
                )
                for index in range(9)
            ]
            client = MissingBulkBodyClient(
                messages, {message["messageId"] for message in messages}
            )

            with patch("analytics.gmail_sync._MAX_FALLBACK_MESSAGES", 8):
                with self.assertRaisesRegex(ComposioError, "fallback limit"):
                    refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertFalse(
                any(
                    slug == "GMAIL_FETCH_MESSAGE_BY_MESSAGE_ID"
                    for slug, _ in client.calls
                )
            )

    def test_unavailable_candidate_body_does_not_advance_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before_checkpoint = paths.checkpoint.read_bytes()
            message = self._message(
                "unavailable-id",
                body="We decided not to move forward.",
                subject="Application update",
            )
            client = UnavailableBodyMailboxClient([message], {"unavailable-id"})

            with self.assertRaisesRegex(ComposioError, "content unavailable"):
                refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual(paths.checkpoint.read_bytes(), before_checkpoint)


    def test_refresh_deadline_uses_one_monotonic_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before_checkpoint = paths.checkpoint.read_bytes()
            clock = FakeClock()
            client = DeadlineMailboxClient([], clock)

            with patch("analytics.gmail_sync.time.monotonic", side_effect=clock):
                with self.assertRaisesRegex(ComposioError, "deadline"):
                    refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual(paths.checkpoint.read_bytes(), before_checkpoint)


    def test_page_ceiling_fails_without_advancing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before_checkpoint = paths.checkpoint.read_bytes()
            client = EndlessPageMailboxClient()

            with patch("analytics.gmail_sync._MAX_QUERY_PAGES", 2):
                with self.assertRaisesRegex(ComposioError, "page limit"):
                    refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual(paths.checkpoint.read_bytes(), before_checkpoint)


    def test_message_ceiling_fails_without_advancing_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before_checkpoint = paths.checkpoint.read_bytes()
            messages = [
                self._message(
                    f"ceiling-id-{index}",
                    body="We received your application.",
                    subject="Application received",
                )
                for index in range(3)
            ]

            with patch("analytics.gmail_sync._MAX_DISCOVERED_MESSAGES", 2):
                with self.assertRaisesRegex(ComposioError, "message limit"):
                    refresh(
                        paths,
                        client=RecordingMailboxClient(messages),
                        sync_gmail=True,
                        now=FIXED_NOW,
                    )

            self.assertEqual(paths.checkpoint.read_bytes(), before_checkpoint)

    def test_incremental_scan_overlaps_seven_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            paths.checkpoint.write_text(
                json.dumps({"last_successful_at": "2026-08-20T12:00:00Z"}) + "\n",
                encoding="utf-8",
            )
            client = RecordingMailboxClient([])
            refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)
            list_call = next(data for slug, data in client.calls if slug == "GMAIL_FETCH_EMAILS")
            self.assertTrue(list_call["query"].startswith("after:2026/08/12 "))

    def test_lifecycle_categories_update_tracker_and_events_but_not_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            client = RecordingMailboxClient(
                [
                    self._message(
                        "received-id",
                        body="We received your application for the Applied AI Engineer role.",
                        subject="Application received",
                    ),
                    self._message(
                        "interview-id",
                        body="We would like to invite you to interview for the Applied AI Engineer role.",
                        subject="Interview invitation",
                    ),
                ]
            )

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)
            application = read_csv_rows(paths.tracker, TRACKER_COLUMNS)[0]
            events = read_csv_rows(paths.events, EVENT_COLUMNS)
            feedback = read_csv_rows(paths.feedback, FEEDBACK_COLUMNS)

            self.assertEqual(summary.events_added, 2)
            self.assertEqual(summary.feedback_added, 0)
            self.assertEqual(summary.tracker_updates, 2)
            self.assertEqual(application["stage"], "interview")
            self.assertEqual(application["status"], "INTERVIEW 2026-08-24")
            self.assertEqual(application["status_updated_at"], "2026-08-24")
            self.assertEqual({event["event_type"] for event in events}, {"received", "interview"})
            self.assertEqual(feedback, [])

    def test_valid_feedback_category_creates_event_feedback_and_rules(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            client = RecordingMailboxClient(
                [
                    self._message(
                        "feedback-id",
                        body=(
                            "We will not progress with your application. "
                            "Your hands-on technical depth did not meet the role requirements."
                        ),
                    )
                ]
            )

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)
            events = read_csv_rows(paths.events, EVENT_COLUMNS)
            feedback = read_csv_rows(paths.feedback, FEEDBACK_COLUMNS)
            rules = json.loads(paths.rules.read_text(encoding="utf-8"))

            self.assertEqual(summary.events_added, 1)
            self.assertEqual(summary.feedback_added, 1)
            self.assertEqual(events[0]["event_type"], "rejected")
            self.assertEqual(feedback[0]["category"], "technical_depth")
            self.assertLessEqual(len(feedback[0]["evidence_excerpt"]), 280)
            self.assertRegex(feedback[0]["source_ref"], r"^[0-9a-f]{64}$")
            self.assertEqual(len(rules), 1)

    def test_logistics_feedback_keeps_geography_and_employment_model_scope(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            client = RecordingMailboxClient(
                [
                    self._message(
                        "logistics-id",
                        body=(
                            "We will not progress with your application. "
                            "This role requires work authorization in the advertised location."
                        ),
                    )
                ]
            )

            refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)
            feedback = read_csv_rows(paths.feedback, FEEDBACK_COLUMNS)

            self.assertEqual(
                json.loads(feedback[0]["scope"]),
                {
                    "employment_model": "employee",
                    "geography": "EEA",
                    "role_family": "applied_ai",
                    "stage": "application",
                },
            )

    def test_ambiguous_match_queues_review_without_other_updates(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            applications = read_csv_rows(paths.tracker, TRACKER_COLUMNS)
            second = dict(applications[0])
            second["application_id"] = "app-2"
            second["role"] = "Machine Learning Engineer"
            write_csv_atomic(paths.tracker, TRACKER_COLUMNS, [*applications, second])
            before_tracker = paths.tracker.read_bytes()
            client = RecordingMailboxClient(
                [self._message("ambiguous-id", body="We will not progress with your application.", role="")]
            )

            summary = refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)

            self.assertEqual(summary.review_items, 1)
            self.assertEqual(summary.events_added, 0)
            self.assertEqual(summary.feedback_added, 0)
            self.assertEqual(summary.tracker_updates, 0)
            self.assertEqual(paths.tracker.read_bytes(), before_tracker)
            review = read_csv_rows(paths.review, REVIEW_COLUMNS)
            self.assertEqual(len(review), 1)
            self.assertNotIn("ambiguous-id", json.dumps(review))
            self.assertRegex(review[0]["source_ref"], r"^[0-9a-f]{64}$")

    def test_duplicate_source_message_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            message = self._message(
                "stable-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            first = refresh(paths, client=RecordingMailboxClient([message]), sync_gmail=True, now=FIXED_NOW)
            second = refresh(paths, client=RecordingMailboxClient([message]), sync_gmail=True, now=FIXED_NOW)
            self.assertEqual(first.events_added, 1)
            self.assertEqual(second.events_added, 0)
            self.assertEqual(len(read_csv_rows(paths.events, EVENT_COLUMNS)), 1)

    def test_conflicting_duplicate_event_identity_is_rejected_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            message = self._message(
                "stable-event-id",
                body="We received your application for the Applied AI Engineer role.",
                subject="Application received",
            )
            refresh(
                paths,
                client=RecordingMailboxClient([message]),
                sync_gmail=True,
                now=FIXED_NOW,
            )
            before = self._snapshot(paths)
            conflicting = read_csv_rows(paths.events, EVENT_COLUMNS)[0]
            conflicting["detail"] = "Conflicting lifecycle detail"
            discovery = MailboxDiscovery(
                proposal=SyncProposal(
                    (conflicting,),
                    (),
                    (),
                    (),
                    json.loads(paths.checkpoint.read_text(encoding="utf-8")),
                ),
                scanned=1,
                matched=1,
            )

            with patch("analytics.refresh.discover_mailbox", return_value=discovery):
                with self.assertRaisesRegex(ValueError, "conflicting duplicate event_id"):
                    refresh(
                        paths,
                        client=RecordingMailboxClient([]),
                        sync_gmail=True,
                        now=FIXED_NOW + timedelta(days=1),
                    )

            self.assertEqual(self._snapshot(paths), before)


    def test_overlapped_feedback_message_is_idempotent_across_refresh_times(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            message = self._message(
                "stable-feedback-id",
                body=(
                    "We will not progress with your application. "
                    "Your hands-on technical depth did not meet the role requirements."
                ),
            )

            first = refresh(
                paths,
                client=RecordingMailboxClient([message]),
                sync_gmail=True,
                now=FIXED_NOW,
            )
            second = refresh(
                paths,
                client=RecordingMailboxClient([message]),
                sync_gmail=True,
                now=FIXED_NOW + timedelta(days=1),
            )

            self.assertEqual(first.feedback_added, 1)
            self.assertEqual(second.feedback_added, 0)
            self.assertEqual(len(read_csv_rows(paths.events, EVENT_COLUMNS)), 1)
            self.assertEqual(len(read_csv_rows(paths.feedback, FEEDBACK_COLUMNS)), 1)

    def test_dry_run_reads_mailbox_but_changes_no_bytes_or_checkpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            before = self._snapshot(paths)
            client = RecordingMailboxClient(
                [
                    self._message(
                        "dry-id",
                        body="We received your application for the Applied AI Engineer role.",
                        subject="Application received",
                    )
                ]
            )

            summary = refresh(
                paths,
                client=client,
                sync_gmail=True,
                now=FIXED_NOW,
                dry_run=True,
            )

            self.assertEqual(summary.matched, 1)
            self.assertEqual(self._snapshot(paths), before)
            self.assertFalse(paths.journal.exists())

    def test_source_identity_and_complete_body_are_never_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = self._paths(Path(tmp))
            raw_id = "private-message-identifier"
            body = "We will not progress with your application. " + "private detail " * 80
            client = RecordingMailboxClient([self._message(raw_id, body=body)])
            refresh(paths, client=client, sync_gmail=True, now=FIXED_NOW)
            persisted = b"\n".join(path.read_bytes() for path in paths.mutable_files())
            self.assertNotIn(raw_id.encode(), persisted)
            self.assertNotIn(body.encode(), persisted)
            self.assertIn(hashlib.sha256(raw_id.encode()).hexdigest().encode(), persisted)


if __name__ == "__main__":
    unittest.main()
