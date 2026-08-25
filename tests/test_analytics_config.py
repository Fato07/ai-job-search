import json
import tempfile
import unittest
from pathlib import Path

from analytics.config import AnalyticsConfigError, load_local_config


class AnalyticsConfigTests(unittest.TestCase):
    def test_missing_local_config_uses_sanitized_non_gmail_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = load_local_config(Path(tmp) / "analytics" / "config.json")

        self.assertEqual(config["reporting_timezone"], "UTC")
        self.assertNotIn("gmail_expected_address", config)
        self.assertNotIn("gmail_account_alias", config)

    def test_gmail_sync_requires_copy_and_edit_instruction(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analytics" / "config.json"
            with self.assertRaisesRegex(
                AnalyticsConfigError,
                r"Copy analytics/config\.example\.json to analytics/config\.json and edit gmail_account_alias and gmail_expected_address",
            ):
                load_local_config(path, require_gmail=True)

    def test_gmail_sync_loads_local_alias_and_expected_mailbox(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analytics" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "gmail_account_alias": "candidate-mail",
                        "gmail_expected_address": "mailbox@example.test",
                        "reporting_timezone": "UTC",
                    }
                ),
                encoding="utf-8",
            )

            config = load_local_config(path, require_gmail=True)

        self.assertEqual(config["gmail_account_alias"], "candidate-mail")
        self.assertEqual(config["gmail_expected_address"], "mailbox@example.test")
        self.assertEqual(config["reporting_timezone"], "UTC")

    def test_unedited_example_mailbox_is_rejected_for_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "analytics" / "config.json"
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "gmail_account_alias": "CHANGE_ME",
                        "gmail_expected_address": "mailbox@example.test",
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(AnalyticsConfigError, "edit gmail_account_alias"):
                load_local_config(path, require_gmail=True)



if __name__ == "__main__":
    unittest.main()
