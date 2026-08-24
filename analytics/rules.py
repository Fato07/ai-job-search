from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from analytics.feedback import STAGES, validate_feedback
from analytics.model import FEEDBACK_COLUMNS, read_csv_rows


ROLE_FAMILIES = ("ai_platform", "ai_security", "applied_ai", "forward_deployed", "other")
SENIORITIES = (
    "intern",
    "junior",
    "mid",
    "senior",
    "staff",
    "principal",
    "lead",
    "founding",
    "executive",
)
GEOGRAPHIES = ("EEA", "US", "Helsinki/Tallinn", "country-of-residence", "office-required")
EMPLOYMENT_MODELS = ("employee", "b2b", "contractor", "unknown")
_DIRECT_EVIDENCE = frozenset(("explicit", "observed"))


@dataclass(frozen=True)
class RuleContext:
    role_family: str
    seniority: str
    geography: str
    stage: str
    employment_model: str


@dataclass
class _RuleGroup:
    category: str
    scope_json: str
    required_action: str
    evidence: list[Mapping[str, str]]
    resolutions: list[Mapping[str, str]]


def _canonical_scope(value: str) -> tuple[str, dict[str, str]]:
    try:
        scope = json.loads(value)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("scope must be a JSON object") from exc
    if not isinstance(scope, dict):
        raise ValueError("scope must be a JSON object")
    if any(not isinstance(key, str) or not isinstance(item, str) for key, item in scope.items()):
        raise ValueError("scope keys and values must be strings")
    canonical = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return canonical, json.loads(canonical)


def _confidence(event: Mapping[str, str]) -> Decimal:
    try:
        value = Decimal(event["confidence"])
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("confidence must be a decimal from 0.0 to 1.0") from exc
    if not value.is_finite() or not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("confidence must be a decimal from 0.0 to 1.0")
    return value


def _activation_result(
    evidence: Sequence[Mapping[str, str]],
) -> tuple[bool, float]:
    activation_evidence = [
        item for item in evidence if item["rule_effect"] == "activate"
    ]
    direct = [
        _confidence(item)
        for item in activation_evidence
        if item["evidence_tier"] in _DIRECT_EVIDENCE
        and _confidence(item) >= Decimal("0.75")
    ]
    if direct:
        return True, float(max(direct))

    inferred_by_source: dict[str, Mapping[str, str]] = {}
    for item in sorted(
        activation_evidence,
        key=lambda row: (row["occurred_at"], row["feedback_id"]),
    ):
        if item["evidence_tier"] == "inferred":
            inferred_by_source[item["source_ref"]] = item
    if len(inferred_by_source) < 2:
        return False, 0.0
    values = [_confidence(item) for item in inferred_by_source.values()]
    confidence = sum(values, Decimal("0")) / len(values)
    return True, float(confidence)


def _rule_id(category: str, scope_json: str, required_action: str) -> str:
    material = "\x1f".join((category, scope_json, required_action))
    return f"rule-{hashlib.sha256(material.encode('utf-8')).hexdigest()}"


def _event_sort_key(event: Mapping[str, str]) -> tuple[str, int, str]:
    effect_order = 1 if event["rule_effect"] == "resolve" else 0
    return event["occurred_at"], effect_order, event["feedback_id"]


def build_rules(feedback: Iterable[Mapping[str, str]]) -> list[dict[str, object]]:
    events = [dict(item) for item in feedback]
    actionable = [
        item
        for item in events
        if item.get("evidence_tier") != "boilerplate"
    ]

    groups: dict[tuple[str, str, str], _RuleGroup] = {}
    feedback_groups: dict[str, tuple[str, str, str]] = {}
    resolutions: list[Mapping[str, str]] = []

    for item in actionable:
        if not all(isinstance(item.get(field), str) for field in (
            "feedback_id",
            "occurred_at",
            "evidence_tier",
            "category",
            "signal",
            "required_action",
            "rule_effect",
            "resolves_feedback_id",
            "scope",
            "confidence",
            "source_ref",
        )):
            raise ValueError("feedback rule fields must be strings")
        _confidence(item)
        if item["rule_effect"] == "resolve":
            resolutions.append(item)
            continue
        if not item["required_action"].strip():
            continue
        scope_json, _ = _canonical_scope(item["scope"])
        key = (item["category"], scope_json, item["required_action"])
        group = groups.setdefault(
            key,
            _RuleGroup(
                category=item["category"],
                scope_json=scope_json,
                required_action=item["required_action"],
                evidence=[],
                resolutions=[],
            ),
        )
        group.evidence.append(item)
        feedback_groups[item["feedback_id"]] = key

    for resolution in resolutions:
        target = resolution["resolves_feedback_id"]
        if target not in feedback_groups:
            raise ValueError(f"resolve references unknown feedback_id: {target!r}")
        group = groups[feedback_groups[target]]
        target_event = next(item for item in group.evidence if item["feedback_id"] == target)
        if resolution["occurred_at"] < target_event["occurred_at"]:
            raise ValueError("resolve event must not precede its target feedback")
        group.resolutions.append(resolution)

    rules: list[dict[str, object]] = []
    for group in groups.values():
        timeline = sorted((*group.evidence, *group.resolutions), key=_event_sort_key)
        seen_evidence: list[Mapping[str, str]] = []
        status = "monitor"
        for item in timeline:
            if item["rule_effect"] == "resolve":
                status = "resolved"
                seen_evidence = []
                continue
            seen_evidence.append(item)
            if item["rule_effect"] == "activate":
                active, _ = _activation_result(seen_evidence)
                status = "active" if active else "monitor"
        latest_evidence = max(
            group.evidence,
            key=lambda item: (item["occurred_at"], item["feedback_id"]),
        )
        source_feedback_ids = sorted(
            item["feedback_id"] for item in (*group.evidence, *group.resolutions)
        )
        rules.append(
            {
                "rule_id": _rule_id(
                    group.category,
                    group.scope_json,
                    group.required_action,
                ),
                "category": group.category,
                "scope": json.loads(group.scope_json),
                "trigger": latest_evidence["signal"],
                "required_action": group.required_action,
                "evidence_count": len(group.evidence),
                "evidence_tiers": sorted({item["evidence_tier"] for item in group.evidence}),
                "confidence": (
                    _activation_result(seen_evidence)[1]
                    if status == "active"
                    else 0.0
                ),
                "source_feedback_ids": source_feedback_ids,
                "last_updated": max(item["occurred_at"] for item in timeline),
                "status": status,
            }
        )

    return sorted(
        rules,
        key=lambda rule: (rule["status"], rule["category"], rule["rule_id"]),
    )


def select_rules(
    rules: Iterable[Mapping[str, object]],
    context: RuleContext,
) -> list[dict[str, object]]:
    context_values = {
        "role_family": context.role_family,
        "seniority": context.seniority,
        "geography": context.geography,
        "stage": context.stage,
        "employment_model": context.employment_model,
    }
    selected: list[dict[str, object]] = []
    for source_rule in rules:
        rule = dict(source_rule)
        if rule.get("status") != "active":
            continue
        scope = rule.get("scope")
        if not isinstance(scope, dict):
            raise ValueError("rule scope must be a JSON object")
        if all(
            dimension in context_values and context_values[dimension] == expected
            for dimension, expected in scope.items()
        ):
            selected.append(rule)
    return sorted(
        selected,
        key=lambda rule: (rule["status"], rule["category"], rule["rule_id"]),
    )


def _write_json_atomic(path: Path, value: object) -> None:
    content = json.dumps(value, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def _build_command(feedback_path: Path, output_path: Path) -> None:
    feedback = read_csv_rows(feedback_path, FEEDBACK_COLUMNS)
    application_ids = {item["application_id"] for item in feedback}
    for item in feedback:
        validate_feedback(item, application_ids)
    rules = build_rules(feedback)
    _write_json_atomic(output_path, rules)
    summary = {
        "categories": dict(sorted(Counter(rule["category"] for rule in rules).items())),
        "rules": len(rules),
        "statuses": dict(sorted(Counter(rule["status"] for rule in rules).items())),
    }
    print(json.dumps(summary, sort_keys=True))


def _match_command(rules_path: Path, context: RuleContext) -> None:
    try:
        rules = json.loads(rules_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError("rules file must contain valid JSON") from exc
    if not isinstance(rules, list):
        raise ValueError("rules file must contain a JSON array")
    print(json.dumps(select_rules(rules, context), indent=2, sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build and select scoped feedback rules")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="derive rules from validated feedback")
    build.add_argument("--feedback", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)

    match = subparsers.add_parser("match", help="select active rules for a context")
    match.add_argument("--rules", type=Path, required=True)
    match.add_argument("--role-family", choices=ROLE_FAMILIES, required=True)
    match.add_argument("--seniority", choices=SENIORITIES, required=True)
    match.add_argument("--geography", choices=GEOGRAPHIES, required=True)
    match.add_argument("--stage", choices=tuple(sorted(STAGES)), required=True)
    match.add_argument(
        "--employment-model",
        choices=EMPLOYMENT_MODELS,
        required=True,
    )
    return parser


def main() -> None:
    args = _parser().parse_args()
    if args.command == "build":
        _build_command(args.feedback, args.output)
    else:
        _match_command(
            args.rules,
            RuleContext(
                args.role_family,
                args.seniority,
                args.geography,
                args.stage,
                args.employment_model,
            ),
        )


if __name__ == "__main__":
    main()
