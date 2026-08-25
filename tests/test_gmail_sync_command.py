"""Guards for /gmail-sync's Gmail query semantics.

The command's stated intent is "skip sent/drafts - status signals come
from what employers send you". `in:inbox` does not mean that: it matches
only messages currently IN the inbox, so it also excludes every archived
message - and, self-defeatingly, the mail matched by the very
job-search label Step 3.1 hunts for, because the standard filter that
applies such a label also archives ("skip the inbox"). The correct
operators for the stated intent are `-in:sent -in:drafts` (review
finding F18, 2026-08-19). The failure mode is silent under-detection: a
missed rejection or interview invite just looks like "no updates".
"""
import unittest
from pathlib import Path
from analytics.gmail_sync import _scan_queries

REPO = Path(__file__).resolve().parent.parent
GMAIL_SYNC = REPO / ".claude" / "commands" / "gmail-sync.md"


class TestGmailQueryOperators(unittest.TestCase):
    def setUp(self):
        self.text = GMAIL_SYNC.read_text(encoding="utf-8")

    def test_command_routes_to_reviewed_refresh_and_review_queue(self):
        self.assertIn("python3 -m analytics.refresh --sync-gmail", self.text)
        self.assertIn("analytics/reconciliation_review.csv", self.text)
        self.assertIn("analytics/config.json", self.text)

    def test_every_reviewed_query_excludes_sent_and_drafts_without_inbox_scope(self):
        queries = _scan_queries(
            [
                {
                    "application_id": "app-1",
                    "discovered_at": "2026-08-01",
                    "submitted_at": "2026-08-02",
                    "company": "Example Co",
                    "stage": "submitted",
                }
            ],
            {"last_successful_at": None},
        )
        self.assertTrue(queries)
        for query in queries:
            with self.subTest(query=query):
                self.assertIn("-in:sent -in:drafts", query)
                self.assertNotIn("in:inbox", query)


if __name__ == "__main__":
    unittest.main()
