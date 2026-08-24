import json
import re
import subprocess
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from analytics.gmail_sync import (
    ComposioClient,
    ComposioError,
    SyncProposal,
    classify_message,
    match_application,
    unwrap_composio_result,
    verify_mailbox,
)


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "job_analytics"
SHA256 = re.compile(r"^[0-9a-f]{64}$")


def load_json(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class RecordingClient:
    def __init__(self, address, account="job-search"):
        self.address = address
        self.account = account
        self.calls = []

    def execute(self, slug, data):
        self.calls.append((slug, data))
        if slug != "GMAIL_GET_PROFILE":
            raise AssertionError("message read occurred before mailbox verification")
        return {"data": {"emailAddress": self.address}}


class GmailReconciliationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.messages = load_json("gmail_messages.json")
        cls.by_case = {message["case"]: message for message in cls.messages}

    def test_inline_composio_output_is_loaded(self):
        result = unwrap_composio_result(load_json("gmail_inline.json"))
        self.assertEqual(result["data"]["messages"][0]["subject"], "Application received")

    def test_parallel_composio_output_is_unwrapped_in_order(self):
        result = unwrap_composio_result({
            "successful": True,
            "results": [
                {"successful": True, "data": {"messages": [{"subject": "first"}]}},
                {"successful": True, "data": {"messages": [{"subject": "second"}]}},
            ],
        })
        self.assertEqual(
            [item["data"]["messages"][0]["subject"] for item in result["results"]],
            ["first", "second"],
        )

    def test_spilled_composio_output_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text(
                json.dumps({"successful": True, "data": {"messages": [{"subject": "x"}]}}),
                encoding="utf-8",
            )
            spilled = load_json("gmail_spilled.json")
            spilled["outputFilePath"] = str(payload)
            self.assertEqual(
                unwrap_composio_result(spilled)["data"]["messages"][0]["subject"],
                "x",
            )

    def test_spilled_path_must_be_an_existing_json_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.json"
            with self.assertRaisesRegex(ComposioError, "spilled output"):
                unwrap_composio_result({
                    "successful": True,
                    "storedInFile": True,
                    "outputFilePath": str(missing),
                })

    def test_malformed_spilled_json_raises_without_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text('{"secret": "do-not-leak"', encoding="utf-8")
            with self.assertRaises(ComposioError) as raised:
                unwrap_composio_result({
                    "successful": True,
                    "storedInFile": True,
                    "outputFilePath": str(payload),
                })
            self.assertNotIn("do-not-leak", str(raised.exception))

    def test_unsuccessful_result_raises_only_composio_error(self):
        with self.assertRaises(ComposioError) as raised:
            unwrap_composio_result({
                "successful": False,
                "error": "rate limited",
                "data": {"messageText": "do-not-leak"},
            })
        self.assertIn("rate limited", str(raised.exception))
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_unsuccessful_parallel_result_raises(self):
        with self.assertRaisesRegex(ComposioError, "profile unavailable"):
            unwrap_composio_result({
                "successful": True,
                "results": [
                    {"successful": True, "data": {}},
                    {"successful": False, "error": "profile unavailable"},
                ],
            })

    @patch("analytics.gmail_sync.subprocess.run")
    def test_client_executes_composio_with_argv_and_no_shell(self, run):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout='{"successful":true,"data":{"ok":true}}', stderr=""
        )
        result = ComposioClient().execute("GMAIL_GET_PROFILE", {"user_id": "me"})
        self.assertTrue(result["data"]["ok"])
        args, kwargs = run.call_args
        self.assertEqual(
            args[0],
            [
                "composio", "execute", "GMAIL_GET_PROFILE",
                "--account", "job-search", "-d", '{"user_id":"me"}',
            ],
        )
        self.assertFalse(kwargs.get("shell", False))
        self.assertTrue(kwargs["check"])
        self.assertTrue(kwargs["capture_output"])
        self.assertTrue(kwargs["text"])

    @patch("analytics.gmail_sync.subprocess.run")
    def test_client_command_failure_uses_stderr_not_stdout_payload(self, run):
        run.side_effect = subprocess.CalledProcessError(
            2,
            ["composio"],
            output='{"messageText":"do-not-leak"}',
            stderr="connection failed",
        )
        with self.assertRaises(ComposioError) as raised:
            ComposioClient().execute("GMAIL_GET_PROFILE", {"user_id": "me"})
        self.assertIn("connection failed", str(raised.exception))
        self.assertNotIn("do-not-leak", str(raised.exception))

    def test_client_rejects_any_account_other_than_job_search(self):
        with self.assertRaisesRegex(ValueError, "job-search"):
            ComposioClient(account="other")

    def test_mailbox_profile_exact_case_insensitive_match_passes(self):
        client = RecordingClient("FATHINDOS.FD@GMAIL.COM")
        verify_mailbox(client, "fathindos.fd@gmail.com")
        self.assertEqual(client.calls, [("GMAIL_GET_PROFILE", {"user_id": "me"})])

    def test_mailbox_gate_cannot_be_redirected_to_another_expected_address(self):
        client = RecordingClient("other@example.test")
        with self.assertRaisesRegex(ComposioError, "expected mailbox"):
            verify_mailbox(client, "other@example.test")
        self.assertEqual(client.calls, [])


    def test_mailbox_mismatch_fails_before_any_message_read(self):
        client = RecordingClient("wrong-account@example.test")
        with self.assertRaisesRegex(ComposioError, "mailbox mismatch"):
            verify_mailbox(client, "fathindos.fd@gmail.com")
        self.assertEqual(client.calls, [("GMAIL_GET_PROFILE", {"user_id": "me"})])

    def test_mailbox_gate_rejects_wrong_account_alias_before_execute(self):
        client = RecordingClient("fathindos.fd@gmail.com", account="other")
        with self.assertRaisesRegex(ComposioError, "account alias"):
            verify_mailbox(client, "fathindos.fd@gmail.com")
        self.assertEqual(client.calls, [])

    def test_named_rejections_are_classified_as_rejected_boilerplate(self):
        cases = (
            "robco_rejection",
            "taktile_rejection_us_role",
            "luminor_security_rejection",
            "carta_rejection",
            "databricks_rejection",
            "bolt_rejection",
        )
        for case in cases:
            with self.subTest(case=case):
                signal = classify_message(self.by_case[case])
                self.assertIsNotNone(signal)
                self.assertEqual(signal.event_type, "rejected")
                self.assertEqual(signal.evidence_tier, "boilerplate")
                self.assertEqual(signal.category, "competition_no_specific_signal")
                self.assertEqual(signal.required_action, "")

    def test_common_exact_rejection_phrases_are_classified(self):
        phrases = (
            "Unfortunately, we won't be moving forward with your application.",
            "We have chosen to move forward with other candidates.",
            "Your application was not selected for the next stage.",
        )
        for index, phrase in enumerate(phrases):
            with self.subTest(phrase=phrase):
                signal = classify_message({
                    "subject": "Application update",
                    "sender": "ExampleCo Talent",
                    "messageTimestamp": "2026-08-24T10:00:00Z",
                    "messageText": phrase,
                    "messageId": f"fixture-rejection-phrase-{index}",
                    "company": "ExampleCo",
                    "role": "Applied AI Engineer",
                })
                self.assertIsNotNone(signal)
                self.assertEqual(signal.event_type, "rejected")


    def test_confirmation_and_interview_signals_are_exact(self):
        confirmation = classify_message(self.by_case["application_confirmation"])
        interview = classify_message(self.by_case["interview_invitation"])
        self.assertEqual((confirmation.event_type, confirmation.category), ("received", "application_status"))
        self.assertEqual((interview.event_type, interview.category), ("interview", "interview_invitation"))

    def test_explicit_logistics_and_skill_feedback_are_not_boilerplate(self):
        logistics = classify_message({
            "subject": "Application update",
            "sender": "ExampleCo Talent",
            "messageTimestamp": "2026-08-24T10:00:00Z",
            "messageText": "<p>We will not move forward.</p><p>This role requires work authorization and visa sponsorship is unavailable.</p>",
            "messageId": "fixture-logistics",
            "company": "ExampleCo",
            "role": "Platform Engineer",
        })
        skill = classify_message({
            "subject": "Application update",
            "sender": "ExampleCo Talent",
            "messageTimestamp": "2026-08-24T10:00:00Z",
            "messageText": "We will not move forward. Selected candidates had more hands-on GenAI evaluation experience.",
            "messageId": "fixture-skill",
            "company": "ExampleCo",
            "role": "AI Engineer",
        })
        self.assertEqual((logistics.evidence_tier, logistics.category), ("explicit", "logistics_work_authorization"))
        self.assertEqual((skill.evidence_tier, skill.category), ("explicit", "ml_genai_evaluation"))

    def test_html_is_stripped_whitespace_normalized_excerpt_bounded_and_source_hashed(self):
        message = {
            "subject": "Application update",
            "sender": "ExampleCo Talent",
            "messageTimestamp": "2026-08-24T10:00:00Z",
            "messageText": "<p>We will not move forward.</p>" + ("  Other candidates were a closer match. " * 20),
            "messageId": "fixture-private-source",
            "company": "ExampleCo",
            "role": "Applied AI Engineer",
        }
        signal = classify_message(message)
        self.assertNotIn("<", signal.excerpt)
        self.assertNotRegex(signal.excerpt, r"\s{2,}")
        self.assertLessEqual(len(signal.excerpt), 280)
        self.assertRegex(signal.source_ref, SHA256)
        self.assertNotIn(message["messageId"], signal.source_ref)

    def test_unrelated_newsletter_is_ignored(self):
        self.assertIsNone(classify_message(self.by_case["unrelated_newsletter"]))

    def test_role_specific_luminor_rejection_matches_only_security_role(self):
        signal = classify_message(self.by_case["luminor_security_rejection"])
        applications = [
            {"application_id": "ai", "company": "Luminor Bank", "role": "AI Platform Engineer", "discovered_at": "2026-08-16"},
            {"application_id": "security", "company": "Luminor Bank", "role": "Senior Security Engineer (Data Platform)", "discovered_at": "2026-08-16"},
        ]
        result = match_application(signal, applications)
        self.assertEqual(result.application_id, "security")
        self.assertGreaterEqual(result.score, 0.85)

    def test_taktile_us_role_matches_unique_berlin_london_application(self):
        signal = classify_message(self.by_case["taktile_rejection_us_role"])
        applications = [{
            "application_id": "taktile-fde",
            "company": "Taktile",
            "role": "Senior Forward Deployed Engineer",
            "geography": "Berlin/London",
            "discovered_at": "2026-07-19",
        }]
        result = match_application(signal, applications)
        self.assertEqual(result.application_id, "taktile-fde")
        self.assertEqual(result.score, 0.90)

    def test_similarly_named_companies_are_not_implicit_aliases(self):
        signal = classify_message({
            "subject": "Application update",
            "sender": "Example Bank Talent",
            "messageTimestamp": "2026-08-24T10:00:00Z",
            "messageText": "We will not progress with your application.",
            "messageId": "fixture-distinct-company",
            "company": "Example Bank",
            "role": "Applied AI Engineer",
        })
        result = match_application(signal, [{
            "application_id": "wrong-company",
            "company": "Example Group",
            "role": "Applied AI Engineer",
            "discovered_at": "2026-08-20",
        }])
        self.assertIsNone(result.application_id)
        self.assertEqual(result.candidates, ())


    def test_same_company_multi_role_ambiguity_never_guesses(self):
        signal = classify_message(self.by_case["luminor_ambiguous_same_company"])
        applications = [
            {"application_id": "ai", "company": "Luminor Bank", "role": "AI Platform Engineer", "discovered_at": "2026-08-16"},
            {"application_id": "security", "company": "Luminor Bank", "role": "Senior Security Engineer (Data Platform)", "discovered_at": "2026-08-16"},
        ]
        result = match_application(signal, applications)
        self.assertIsNone(result.application_id)
        self.assertEqual(set(result.candidates), {"ai", "security"})
        self.assertIn("ambiguous", result.reason)

    def test_unique_margin_must_be_at_least_point_two(self):
        signal = classify_message({
            "subject": "Application update",
            "sender": "Aster Labs Talent",
            "messageTimestamp": "2026-08-24T10:00:00Z",
            "messageText": "We will not progress with your application for Senior AI Platform Engineer.",
            "messageId": "fixture-margin",
            "company": "Aster Labs",
            "role": "Senior AI Platform Engineer",
        })
        applications = [
            {"application_id": "one", "company": "Aster Labs", "role": "Senior AI Platform Engineer", "discovered_at": "2026-08-20"},
            {"application_id": "two", "company": "Aster Labs", "role": "AI Platform Engineer", "discovered_at": "2026-08-20"},
        ]
        result = match_application(signal, applications)
        self.assertIsNone(result.application_id)
        self.assertIn("margin", result.reason)

    def test_message_before_application_never_matches(self):
        signal = classify_message(self.by_case["carta_rejection"])
        result = match_application(signal, [{
            "application_id": "later",
            "company": "Carta",
            "role": "Staff Software Engineer, Middle Office",
            "discovered_at": "2026-08-22",
        }])
        self.assertIsNone(result.application_id)
        self.assertIn("predates", result.reason)

    def test_sync_proposal_is_immutable(self):
        proposal = SyncProposal((), (), (), (), {"last_successful_at": None})
        with self.assertRaises(FrozenInstanceError):
            proposal.events = ()

    def test_fixtures_are_sanitized_and_complete_for_task_matrix(self):
        required = {
            "robco_rejection", "taktile_rejection_us_role", "luminor_security_rejection",
            "carta_rejection", "databricks_rejection", "bolt_rejection",
            "application_confirmation", "interview_invitation",
            "luminor_ambiguous_same_company", "unrelated_newsletter",
        }
        self.assertEqual(set(self.by_case), required)
        raw = "\n".join(
            (FIXTURES / name).read_text(encoding="utf-8")
            for name in ("gmail_inline.json", "gmail_spilled.json", "gmail_messages.json")
        )
        self.assertNotIn("@", raw)
        self.assertNotRegex(raw, r"\b(?:code|token|password)\b")
        for message in self.messages:
            self.assertLessEqual(len(message["messageText"]), 280)
            source = message.get("messageId") or message.get("threadId")
            self.assertTrue(source.startswith("fixture-"))


if __name__ == "__main__":
    unittest.main()
