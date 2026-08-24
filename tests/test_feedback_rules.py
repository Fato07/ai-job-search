import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from analytics.rules import RuleContext, build_rules, main, select_rules


ROOT = Path(__file__).resolve().parents[1]
BASE_EVENT = {
    "feedback_id": "fb-base",
    "application_id": "app-1",
    "occurred_at": "2026-07-31T12:00:00Z",
    "stage": "application",
    "source": "candidate_postmortem",
    "evidence_tier": "observed",
    "category": "metric_rigor_provenance",
    "signal": "Metric could not be derived under interview probing.",
    "evidence_excerpt": "The interviewer repeatedly asked for the metric denominator.",
    "required_action": "State denominator and provenance for every metric.",
    "rule_effect": "activate",
    "resolves_feedback_id": "",
    "scope": '{"role_family":"applied_ai","stage":"application"}',
    "confidence": "0.95",
    "source_ref": "source-base",
    "created_at": "2026-08-24T12:00:00Z",
}


def event(**overrides):
    value = dict(BASE_EVENT)
    value.update(overrides)
    return value


class FeedbackRuleGenerationTests(unittest.TestCase):
    def test_boilerplate_does_not_create_rule(self):
        events = [event(
            feedback_id="fb-boilerplate",
            evidence_tier="boilerplate",
            category="competition_no_specific_signal",
            signal="Other candidates were a closer match.",
            evidence_excerpt="We moved forward with other candidates.",
            required_action="",
            rule_effect="monitor",
            confidence="0.2",
            source_ref="source-boilerplate",
        )]
        self.assertEqual(build_rules(events), [])

    def test_explicit_or_observed_event_activates_scoped_rule(self):
        for evidence_tier in ("explicit", "observed"):
            with self.subTest(evidence_tier=evidence_tier):
                rules = build_rules([event(evidence_tier=evidence_tier)])
                selected = select_rules(
                    rules,
                    RuleContext("applied_ai", "senior", "EEA", "application", "unknown"),
                )
                self.assertEqual(len(selected), 1)
                self.assertEqual(rules[0]["status"], "active")
                self.assertEqual(rules[0]["confidence"], 0.95)

    def test_low_confidence_direct_evidence_is_monitoring_only(self):
        rules = build_rules([event(confidence="0.74")])
        self.assertEqual(rules[0]["status"], "monitor")
        self.assertEqual(
            select_rules(
                rules,
                RuleContext("applied_ai", "senior", "EEA", "application", "unknown"),
            ),
            [],
        )

    def test_inferred_evidence_requires_two_independent_sources(self):
        first = event(
            feedback_id="fb-inferred-a",
            evidence_tier="inferred",
            confidence="0.7",
            source_ref="source-a",
        )
        second = event(
            feedback_id="fb-inferred-b",
            occurred_at="2026-08-01T12:00:00Z",
            evidence_tier="inferred",
            confidence="0.9",
            source_ref="source-b",
        )
        duplicate_source = event(
            feedback_id="fb-inferred-c",
            occurred_at="2026-08-02T12:00:00Z",
            evidence_tier="inferred",
            confidence="1.0",
            source_ref="source-a",
        )

        self.assertEqual(build_rules([first])[0]["status"], "monitor")
        self.assertEqual(
            build_rules([first, duplicate_source])[0]["status"],
            "monitor",
        )
        active = build_rules([second, first])[0]
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["confidence"], 0.8)

    def test_monitor_evidence_does_not_inflate_active_confidence(self):
        rules = build_rules([
            event(
                feedback_id="fb-active",
                confidence="0.8",
                source_ref="source-active",
            ),
            event(
                feedback_id="fb-monitor",
                occurred_at="2026-08-01T12:00:00Z",
                rule_effect="monitor",
                confidence="1.0",
                source_ref="source-monitor",
            ),
        ])
        self.assertEqual(rules[0]["status"], "active")
        self.assertEqual(rules[0]["confidence"], 0.8)

    def test_qualifying_inferred_confidence_excludes_low_direct_evidence(self):
        rules = build_rules([
            event(
                feedback_id="fb-low-direct",
                confidence="0.7",
                source_ref="source-direct",
            ),
            event(
                feedback_id="fb-inferred-a",
                occurred_at="2026-08-01T12:00:00Z",
                evidence_tier="inferred",
                confidence="0.8",
                source_ref="source-inferred-a",
            ),
            event(
                feedback_id="fb-inferred-b",
                occurred_at="2026-08-02T12:00:00Z",
                evidence_tier="inferred",
                confidence="0.9",
                source_ref="source-inferred-b",
            ),
        ])
        self.assertEqual(rules[0]["status"], "active")
        self.assertEqual(rules[0]["confidence"], 0.85)

    def test_grouping_canonicalizes_scope_and_sorts_source_ids(self):
        rules = build_rules([
            event(
                feedback_id="fb-z",
                scope='{"stage":"application","role_family":"applied_ai"}',
                source_ref="source-z",
            ),
            event(
                feedback_id="fb-a",
                occurred_at="2026-08-01T12:00:00Z",
                confidence="0.8",
                source_ref="source-a",
            ),
        ])

        self.assertEqual(len(rules), 1)
        self.assertEqual(
            rules[0]["scope"],
            {"role_family": "applied_ai", "stage": "application"},
        )
        self.assertEqual(rules[0]["source_feedback_ids"], ["fb-a", "fb-z"])
        self.assertEqual(rules[0]["confidence"], 0.95)
        self.assertEqual(rules, build_rules(reversed([
            event(
                feedback_id="fb-z",
                scope='{"stage":"application","role_family":"applied_ai"}',
                source_ref="source-z",
            ),
            event(
                feedback_id="fb-a",
                occurred_at="2026-08-01T12:00:00Z",
                confidence="0.8",
                source_ref="source-a",
            ),
        ])))

    def test_resolve_event_closes_prior_rule(self):
        events = [
            event(feedback_id="fb-open", source_ref="source-open"),
            event(
                feedback_id="fb-resolved",
                occurred_at="2026-08-24T12:00:00Z",
                signal="Metric derivation verified from source data.",
                evidence_excerpt="Recomputed 95.79% field-level F1 on 87 evaluated documents.",
                rule_effect="resolve",
                resolves_feedback_id="fb-open",
                confidence="1.0",
                source_ref="source-resolved",
            ),
        ]
        rules = build_rules(events)
        self.assertEqual(rules[0]["status"], "resolved")
        self.assertEqual(rules[0]["confidence"], 0.0)
        self.assertEqual(
            rules[0]["source_feedback_ids"],
            ["fb-open", "fb-resolved"],
        )
        self.assertEqual(rules[0]["last_updated"], "2026-08-24T12:00:00Z")
        self.assertEqual(
            select_rules(
                rules,
                RuleContext("applied_ai", "senior", "EEA", "application", "unknown"),
            ),
            [],
        )

    def test_later_activation_reopens_a_resolved_rule(self):
        rules = build_rules([
            event(feedback_id="fb-open", source_ref="source-open"),
            event(
                feedback_id="fb-resolved",
                occurred_at="2026-08-01T12:00:00Z",
                rule_effect="resolve",
                resolves_feedback_id="fb-open",
                source_ref="source-resolved",
            ),
            event(
                feedback_id="fb-reopened",
                occurred_at="2026-08-02T12:00:00Z",
                confidence="0.8",
                source_ref="source-reopened",
            ),
        ])
        self.assertEqual(rules[0]["status"], "active")
        self.assertEqual(rules[0]["confidence"], 0.8)
        self.assertEqual(rules[0]["last_updated"], "2026-08-02T12:00:00Z")

    def test_resolved_evidence_does_not_activate_a_later_weak_signal(self):
        rules = build_rules([
            event(feedback_id="fb-open", source_ref="source-open"),
            event(
                feedback_id="fb-resolved",
                occurred_at="2026-08-01T12:00:00Z",
                rule_effect="resolve",
                resolves_feedback_id="fb-open",
                source_ref="source-resolved",
            ),
            event(
                feedback_id="fb-weak",
                occurred_at="2026-08-02T12:00:00Z",
                confidence="0.5",
                source_ref="source-weak",
            ),
        ])
        self.assertEqual(rules[0]["status"], "monitor")
        self.assertEqual(rules[0]["confidence"], 0.0)

    def test_unknown_resolution_target_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "unknown feedback_id"):
            build_rules([event(
                feedback_id="fb-resolved",
                rule_effect="resolve",
                resolves_feedback_id="fb-missing",
                source_ref="source-resolved",
            )])

    def test_output_is_sorted_by_status_category_and_rule_id(self):
        rules = build_rules([
            event(
                feedback_id="fb-monitor",
                category="technical_depth",
                required_action="Add technical depth.",
                confidence="0.5",
                source_ref="source-monitor",
            ),
            event(
                feedback_id="fb-active",
                category="communication_decision_clarity",
                required_action="Choose explicitly.",
                source_ref="source-active",
            ),
        ])
        self.assertEqual(
            [(rule["status"], rule["category"], rule["rule_id"]) for rule in rules],
            sorted((rule["status"], rule["category"], rule["rule_id"]) for rule in rules),
        )


class FeedbackRuleSelectionTests(unittest.TestCase):
    def test_matching_requires_every_present_dimension_and_uses_wildcards(self):
        rules = build_rules([
            event(feedback_id="fb-global", scope="{}", source_ref="source-global"),
            event(
                feedback_id="fb-all",
                category="technical_depth",
                required_action="Use scoped technical evidence.",
                scope='{"employment_model":"employee","geography":"EEA","role_family":"applied_ai","seniority":"senior","stage":"application"}',
                source_ref="source-all",
            ),
            event(
                feedback_id="fb-wrong-stage",
                category="ml_genai_evaluation",
                required_action="Use evaluation evidence.",
                scope='{"stage":"technical"}',
                source_ref="source-wrong-stage",
            ),
            event(
                feedback_id="fb-unsupported-dimension",
                category="logistics_work_authorization",
                required_action="Check employment region.",
                scope='{"employment_region":"employee","stage":"application"}',
                source_ref="source-unsupported",
            ),
        ])

        selected = select_rules(
            rules,
            RuleContext("applied_ai", "senior", "EEA", "application", "employee"),
        )
        self.assertEqual(
            {rule["source_feedback_ids"][0] for rule in selected},
            {"fb-all", "fb-global"},
        )
        self.assertEqual(
            select_rules(
                rules,
                RuleContext("applied_ai", "senior", "EEA", "application", "b2b"),
            )[0]["source_feedback_ids"],
            ["fb-global"],
        )
        self.assertEqual(
            select_rules(
                rules,
                RuleContext("ai_platform", "senior", "EEA", "application", "employee"),
            )[0]["source_feedback_ids"],
            ["fb-global"],
        )

    def test_selection_excludes_monitoring_and_resolved_rules(self):
        active = build_rules([event(feedback_id="fb-active", source_ref="source-active")])[0]
        monitor = dict(active, rule_id="rule-monitor", status="monitor")
        resolved = dict(active, rule_id="rule-resolved", status="resolved")
        self.assertEqual(
            select_rules(
                [resolved, monitor, active],
                RuleContext(
                    "applied_ai",
                    "senior",
                    "EEA",
                    "application",
                    "unknown",
                ),
            ),
            [active],
        )


class FeedbackRuleCliTests(unittest.TestCase):
    def test_build_cli_writes_real_rules_deterministically(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "rules.json"
            argv = [
                "analytics.rules",
                "build",
                "--feedback",
                str(ROOT / "analytics" / "application_feedback.csv"),
                "--output",
                str(output),
            ]
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                main()
            first = output.read_bytes()
            with patch.object(sys, "argv", argv), redirect_stdout(io.StringIO()):
                main()
            self.assertEqual(output.read_bytes(), first)
            rules = json.loads(first)
            self.assertEqual(len(rules), 11)
            self.assertTrue(all(rule["status"] == "active" for rule in rules))
            self.assertFalse(any(
                rule["category"] == "competition_no_specific_signal"
                for rule in rules
            ))

    def test_match_cli_emits_deterministic_json(self):
        with tempfile.TemporaryDirectory() as directory:
            rules_path = Path(directory) / "rules.json"
            rules = build_rules([event()])
            rules_path.write_text(json.dumps(rules), encoding="utf-8")
            argv = [
                "analytics.rules",
                "match",
                "--rules",
                str(rules_path),
                "--role-family",
                "applied_ai",
                "--seniority",
                "senior",
                "--geography",
                "US",
                "--stage",
                "application",
                "--employment-model",
                "unknown",
            ]
            outputs = []
            for _ in range(2):
                stdout = io.StringIO()
                with patch.object(sys, "argv", argv), redirect_stdout(stdout):
                    main()
                outputs.append(stdout.getvalue())
            self.assertEqual(outputs[0], outputs[1])
            self.assertEqual(json.loads(outputs[0]), rules)

    def test_match_cli_rejects_non_normalized_context_values(self):
        valid = {
            "--role-family": "applied_ai",
            "--seniority": "senior",
            "--geography": "EEA",
            "--stage": "application",
            "--employment-model": "unknown",
        }
        invalid = {
            "--role-family": "Applied AI",
            "--seniority": "Senior Engineer",
            "--geography": "Europe",
            "--stage": "Application Stage",
            "--employment-model": "full-time",
        }
        for option, value in invalid.items():
            with self.subTest(option=option):
                arguments = dict(valid)
                arguments[option] = value
                argv = ["analytics.rules", "match", "--rules", "unused.json"]
                for name, selected_value in arguments.items():
                    argv.extend((name, selected_value))
                with patch.object(sys, "argv", argv), redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit) as raised:
                        main()
                self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
