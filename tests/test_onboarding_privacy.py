"""Guards for the onboarding privacy warnings (issue #345).

The README's quick start walks a new user into creating a public fork
(forks of public repos cannot be private) and then has /setup write
personal data into tracked files, with the only complete warning sitting
in SETUP.md section 8 - a section about pulling updates, downstream of
the decision it should inform. A real user hit exactly this. These tests
pin that the warning lives at the point of decision (adjacent to both
fork commands) and that /setup checks the origin's visibility BEFORE
writing anything, not in its closing notes.
"""
import re
import unittest
from pathlib import Path
import subprocess

REPO = Path(__file__).resolve().parent.parent
README = REPO / "README.md"
SETUP_GUIDE = REPO / "SETUP.md"
SETUP_COMMAND = REPO / ".claude" / "commands" / "setup.md"


def section(text: str, heading: str) -> str:
    """Body of a markdown section up to the next heading of the same level."""
    level = heading.split(" ")[0]
    pattern = re.compile(
        rf"^{re.escape(heading)}\n(.*?)(?=^{level} |\Z)", re.MULTILINE | re.DOTALL
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


class TestForkWarningsAtTheDecisionPoint(unittest.TestCase):
    def assert_warns(self, body: str, where: str):
        self.assertRegex(
            body,
            re.compile(r"public", re.IGNORECASE),
            f"{where}'s fork section must say the fork will be public",
        )
        self.assertIn(
            "personal data",
            body,
            f"{where}'s fork section must say /setup writes personal data into tracked files",
        )
        self.assertRegex(
            body,
            re.compile(r"section 8|§8|#8-pulling", re.IGNORECASE),
            f"{where}'s fork section must point at SETUP.md section 8's private-remote recipe",
        )

    def test_readme_quick_start_warns_next_to_the_fork_command(self):
        body = section(README.read_text(encoding="utf-8"), "### 1. Fork and clone")
        self.assertIn("gh repo fork", body, "sanity: the fork command lives in this section")
        self.assert_warns(body, "README")

    def test_setup_guide_warns_next_to_the_fork_command(self):
        body = section(SETUP_GUIDE.read_text(encoding="utf-8"), "## 2. Fork and clone")
        self.assertIn("gh repo fork", body, "sanity: the fork command lives in this section")
        self.assert_warns(body, "SETUP.md")


class TestSetupChecksOriginBeforeWriting(unittest.TestCase):
    def test_preflight_exists_and_precedes_profile_generation(self):
        text = SETUP_COMMAND.read_text(encoding="utf-8")
        self.assertIn(
            "git remote get-url origin",
            text,
            "/setup must check where the working copy would publish to",
        )
        preflight_at = text.index("git remote get-url origin")
        writes_at = text.index("## Step 3: Generate Profile Files")
        self.assertLess(
            preflight_at,
            writes_at,
            "the origin check must run before any profile file is written - the "
            "existing Step 4 note fires after everything is already on disk",
        )
        self.assertIn(
            "public",
            text[max(0, preflight_at - 2000) : preflight_at + 2000].lower(),
            "the preflight must be about public visibility, not just remote presence",
        )

class TestTrackedAnalyticsPrivacy(unittest.TestCase):
    def test_personal_analytics_state_is_not_tracked(self):
        tracked = set(
            subprocess.run(
                ["git", "ls-files"],
                cwd=REPO,
                check=True,
                capture_output=True,
                text=True,
            ).stdout.splitlines()
        )
        personal = {
            "job_search_tracker.csv",
            "analytics/config.json",
            "analytics/application_events.csv",
            "analytics/application_feedback.csv",
            "analytics/feedback_rules.json",
            "analytics/reconciliation_review.csv",
            "analytics/gmail_checkpoint.json",
            "dashboard/index.html",
        }
        self.assertTrue(personal.isdisjoint(tracked), personal & tracked)
        self.assertIn("analytics/config.example.json", tracked)

    def test_tracked_text_has_no_known_local_identity_or_timezone(self):
        tracked = subprocess.run(
            ["git", "ls-files"],
            cwd=REPO,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        forbidden = tuple(
            bytes.fromhex(encoded).decode("utf-8")
            for encoded in ("66617468696e646f73", "4575726f70652f54616c6c696e6e")
        )
        findings = []
        for relative in tracked:
            path = REPO / relative
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for needle in forbidden:
                if needle.casefold() in text.casefold():
                    findings.append(f"{relative}: {needle}")
        self.assertEqual(findings, [])


if __name__ == "__main__":
    unittest.main()
