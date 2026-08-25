import json
import multiprocessing
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path

from analytics.init import initialize
from analytics.lock import analytics_lock
from analytics.model import EVENT_COLUMNS, read_csv_rows, read_tracker_rows
from analytics.record import record_draft, record_transition


def _hold(root: str, ready, release):
    with analytics_lock(Path(root), timeout=2):
        ready.set()
        release.wait(2)


def _paused_manual(root: str, application_id: str, loaded, release, result):
    try:
        import analytics.record as module
        original = module._load

        def paused(path):
            state = original(path)
            loaded.set()
            release.wait(3)
            return state

        module._load = paused
        module.record_transition(Path(root), application_id, "interview", "2026-08-27")
        result.put("manual-ok")
    except BaseException as exc:
        result.put(f"manual-error:{exc}")


def _refresh_offer(root: str, application_id: str, started, result):
    try:
        import analytics.refresh as module
        from analytics.gmail_sync import MailboxDiscovery, SyncProposal
        from analytics.record import _event

        class Client:
            account = "test-mail"

        proposal = SyncProposal(
            (_event(application_id, "2026-08-28", "offer", "Offer"),),
            (),
            ({
                "application_id": application_id,
                "stage": "offer",
                "status": "offer",
                "status_updated_at": "2026-08-28",
            },),
            (),
            {"last_successful_at": "2026-08-28T00:00:00Z"},
        )
        module.discover_mailbox = lambda *args, **kwargs: MailboxDiscovery(proposal, 1, 1)
        started.set()
        module.refresh(
            module.RefreshPaths.for_root(Path(root)),
            client=Client(),
            sync_gmail=True,
            now=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
        result.put("refresh-ok")
    except BaseException as exc:
        result.put(f"refresh-error:{exc}")


class AnalyticsLockTests(unittest.TestCase):
    def test_cross_process_mutations_serialize_with_bounded_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            ready = multiprocessing.Event()
            release = multiprocessing.Event()
            process = multiprocessing.Process(target=_hold, args=(tmp, ready, release))
            process.start()
            try:
                self.assertTrue(ready.wait(2))
                with self.assertRaises(TimeoutError):
                    with analytics_lock(Path(tmp), timeout=0.1):
                        pass
            finally:
                release.set()
                process.join(2)
            self.assertEqual(process.exitcode, 0)
            with self.assertRaisesRegex(RuntimeError, "release"):
                with analytics_lock(Path(tmp), timeout=0.2):
                    raise RuntimeError("release")
            with analytics_lock(Path(tmp), timeout=0.2):
                pass

    def test_manual_and_gmail_writers_retain_both_interleaved_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "analytics").mkdir()
            (root / "analytics/config.example.json").write_text(
                '{"gmail_account_alias":"CHANGE_ME","gmail_expected_address":"candidate@example.test","reporting_timezone":"UTC","company_aliases":{}}\n',
                encoding="utf-8",
            )
            initialize(root, now=datetime(2026, 8, 25, tzinfo=timezone.utc))
            config = {
                "gmail_account_alias": "test-mail",
                "gmail_expected_address": "mailbox@example.test",
                "reporting_timezone": "UTC",
                "company_aliases": {},
            }
            (root / "analytics/config.json").write_text(json.dumps(config), encoding="utf-8")
            application_id = record_draft(
                root,
                {"discovered_at": "2026-08-25", "company": "Example", "role": "Engineer"},
            )
            record_transition(root, application_id, "submitted", "2026-08-26")

            loaded = multiprocessing.Event()
            release = multiprocessing.Event()
            refresh_started = multiprocessing.Event()
            results = multiprocessing.Queue()
            manual = multiprocessing.Process(
                target=_paused_manual,
                args=(tmp, application_id, loaded, release, results),
            )
            gmail = multiprocessing.Process(
                target=_refresh_offer,
                args=(tmp, application_id, refresh_started, results),
            )
            manual.start()
            self.assertTrue(loaded.wait(2))
            gmail.start()
            self.assertTrue(refresh_started.wait(2))
            time.sleep(0.15)
            self.assertTrue(gmail.is_alive(), "refresh must wait while manual writer holds lock")
            release.set()
            manual.join(4)
            gmail.join(4)
            self.assertEqual({results.get(timeout=1), results.get(timeout=1)}, {"manual-ok", "refresh-ok"})
            self.assertEqual(manual.exitcode, 0)
            self.assertEqual(gmail.exitcode, 0)

            row = read_tracker_rows(root / "job_search_tracker.csv")[0]
            events = read_csv_rows(root / "analytics/application_events.csv", EVENT_COLUMNS)
            self.assertEqual((row["stage"], row["status"]), ("offer", "offer"))
            self.assertTrue({"interview", "offer"} <= {event["event_type"] for event in events})


if __name__ == "__main__":
    unittest.main()
