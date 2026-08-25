# Job Application Analytics and Feedback Loop Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a local analytics and learning system that screens 100 opportunities per day, reconciles Gmail feedback through Composio, converts evidence into scoped rules, improves future `/apply` runs, and renders the complete application history in one self-contained dashboard.

**Architecture:** `job_search_tracker.csv` remains the current-state application source of truth after a clean schema migration. Append-only lifecycle and feedback ledgers preserve history; deterministic rule generation feeds both the dashboard and `/apply`; an on-demand, read-only Composio adapter reconciles Gmail; a standard-library Python builder embeds the full snapshot into a standalone HTML dashboard.

**Tech Stack:** Python 3 standard library (`csv`, `dataclasses`, `datetime`, `hashlib`, `html`, `json`, `pathlib`, `re`, `subprocess`, `tempfile`, `unittest`), vanilla HTML/CSS/JavaScript, native SVG, Composio CLI.

**Spec:** `docs/superpowers/specs/2026-08-24-job-application-analytics-feedback-loop-design.md`

## Global Constraints

- The daily target is exactly 100 opportunities screened, not 100 submitted applications.
- Never automate application submission or bypass quality gates.
- Load the Composio Gmail account alias and expected mailbox from ignored `analytics/config.json`; tests use `candidate@example.test`.
- Gmail synchronization is read-only and on demand.
- Never persist complete Gmail bodies, security codes, access tokens, personal addresses, or unrelated messages.
- Persist evidence excerpts at a maximum of 280 characters and hash source references with SHA-256.
- Ambiguous Gmail matches go to `analytics/reconciliation_review.csv` and cannot alter tracker status, lifecycle events, or feedback rules.
- All tracker, event, feedback, checkpoint, review-queue, rule, and dashboard writes are atomic.
- Use the Python standard library unless an existing repository dependency already satisfies the requirement.
- `dashboard/index.html` must be self-contained and make no external network requests.
- Migrate all tracker consumers in the same cutover; do not keep a second legacy analytics convention.
- Generic rejection boilerplate cannot create a candidate-deficit rule.
- Logistics feedback remains scoped to geography and employment model.
- Do not claim outcome prediction from the current sample size.

## Locked File Structure

```text
analytics/
  __init__.py                 # Package marker and public version
  config.json                 # Daily screening target and safety gates
  model.py                    # CSV schemas, IDs, validation, atomic writes
  migrate.py                  # One-time tracker normalization
  events.py                   # Lifecycle event backfill and append logic
  screening.py                # Candidate-batch ingestion and screening records
  feedback.py                 # Feedback validation, inception backfill, append logic
  rules.py                    # Deterministic rule generation and selection
  gmail_sync.py               # Composio adapter, message parser, matcher, proposals
  refresh.py                  # Atomic orchestration across all data files
  application_events.csv      # Append-only lifecycle ledger
  application_feedback.csv    # Append-only feedback ledger
  feedback_rules.json         # Generated scoped rules
  reconciliation_review.csv   # Ambiguous Gmail matches
  gmail_checkpoint.json       # Incremental-sync checkpoint

dashboard/
  __init__.py                 # Package marker
  build.py                    # Snapshot aggregation, HTML rendering, CLI
  template.html               # Source HTML/CSS/JS with one data marker
  index.html                  # Generated self-contained dashboard

tests/
  __init__.py
  fixtures/job_analytics/
    legacy_tracker.csv
    gmail_inline.json
    gmail_spilled.json
    gmail_messages.json
  test_analytics_model.py
  test_tracker_migration.py
  test_lifecycle_events.py
  test_screening_ingest.py
  test_feedback_ledger.py
  test_feedback_rules.py
  test_gmail_reconciliation.py
  test_atomic_refresh.py
  test_dashboard_build.py
```

---

### Task 1: CSV Model, Stable IDs, and Atomic Writes

**Files:**
- Create: `analytics/__init__.py`
- Create: `analytics/model.py`
- Create: `tests/__init__.py`
- Create: `tests/test_analytics_model.py`

**Interfaces:**
- Produces: `TRACKER_COLUMNS`, `EVENT_COLUMNS`, `FEEDBACK_COLUMNS`, `REVIEW_COLUMNS`
- Produces: `slugify(value: str) -> str`
- Produces: `stable_application_id(discovered_at: str, company: str, role: str) -> str`
- Produces: `hash_source_ref(value: str) -> str`
- Produces: `read_csv_rows(path: Path, required: Collection[str]) -> list[dict[str, str]]`
- Produces: `write_csv_atomic(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None`
- Produces: `validate_rows(rows, columns, unique_key=None) -> None`

- [ ] **Step 1: Create the failing model tests**

```python
# tests/test_analytics_model.py
import csv
import tempfile
import unittest
from pathlib import Path

from analytics.model import (
    TRACKER_COLUMNS,
    hash_source_ref,
    read_csv_rows,
    stable_application_id,
    write_csv_atomic,
)


class AnalyticsModelTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the tests and verify the import fails**

Run:

```bash
python3 -m unittest tests.test_analytics_model -v
```

Expected: `ModuleNotFoundError: No module named 'analytics.model'`.

- [ ] **Step 3: Implement the schemas and primitives**

```python
# analytics/model.py
from __future__ import annotations

import csv
import hashlib
import os
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Collection, Iterable, Mapping, Sequence

TRACKER_COLUMNS = (
    "application_id", "discovered_at", "company", "sector", "role",
    "role_family", "role_type", "geography", "logistics_status", "channel",
    "screening_decision", "screening_reason", "submitted_at", "stage",
    "status", "status_updated_at", "contact_person", "fit_score", "fit_label",
    "notes", "cv_file", "cover_letter_file", "source",
)
EVENT_COLUMNS = (
    "event_id", "application_id", "occurred_at", "event_type", "source",
    "detail", "source_ref", "created_at",
)
FEEDBACK_COLUMNS = (
    "feedback_id", "application_id", "occurred_at", "stage", "source",
    "evidence_tier", "category", "signal", "evidence_excerpt",
    "required_action", "rule_effect", "resolves_feedback_id", "scope",
    "confidence", "source_ref", "created_at",
)
REVIEW_COLUMNS = (
    "review_id", "occurred_at", "sender", "subject", "company", "role",
    "candidate_application_ids", "reason", "source_ref", "status",
)


def slugify(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", normalized.lower())).strip("-")


def hash_source_ref(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def stable_application_id(discovered_at: str, company: str, role: str) -> str:
    date_part = discovered_at.replace("-", "")
    base = f"{discovered_at}\x1f{company.strip().casefold()}\x1f{role.strip().casefold()}"
    digest = hashlib.sha256(base.encode("utf-8")).hexdigest()[:8]
    return f"app-{date_part}-{slugify(company)}-{slugify(role)}-{digest}"


def validate_rows(rows, columns, unique_key=None) -> None:
    expected = set(columns)
    seen = set()
    for index, row in enumerate(rows, start=2):
        if set(row) != expected:
            raise ValueError(f"row {index} columns differ from schema")
        if unique_key:
            value = row[unique_key]
            if not value or value in seen:
                raise ValueError(f"row {index} invalid {unique_key}: {value!r}")
            seen.add(value)


def read_csv_rows(path: Path, required: Collection[str]) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = set(required) - set(reader.fieldnames or ())
        if missing:
            raise ValueError(f"{path}: missing columns {sorted(missing)}")
        return [dict(row) for row in reader]


def write_csv_atomic(path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, object]]) -> None:
    materialized = [{column: str(row.get(column, "")) for column in columns} for row in rows]
    validate_rows(materialized, columns)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", newline="", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(materialized)
        temp_path = Path(handle.name)
    os.replace(temp_path, path)
```

Create `analytics/__init__.py` with `__version__ = "1.0.0"` and an empty `tests/__init__.py`.

- [ ] **Step 4: Run the focused test**

Run:

```bash
python3 -m unittest tests.test_analytics_model -v
```

Expected: three tests pass.

- [ ] **Step 5: Commit the model foundation**

```bash
git add analytics/__init__.py analytics/model.py tests/__init__.py tests/test_analytics_model.py
git commit -m "feat: add analytics data model"
```

---

### Task 2: Clean Tracker Schema Migration

**Files:**
- Create: `analytics/config.json`
- Create: `analytics/migrate.py`
- Create: `tests/fixtures/job_analytics/legacy_tracker.csv`
- Create: `tests/test_tracker_migration.py`
- Modify: `job_search_tracker.csv`

**Interfaces:**
- Consumes: model schemas and atomic writes from Task 1
- Produces: `MigrationReport(row_count: int, warnings: Sequence[str])`
- Produces: `migrate_row(row: Mapping[str, str]) -> dict[str, str]`
- Produces: `migrate_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]`
- Produces: `migrate_tracker(path: Path, apply: bool) -> MigrationReport`

- [ ] **Step 1: Add a representative legacy fixture and failing migration tests**

Use `tests/fixtures/job_analytics/legacy_tracker.csv` as the representative legacy input. It is test-only migration data; the operating tracker and documentation use only the canonical normalized schema.

```python
# tests/test_tracker_migration.py
import unittest
from pathlib import Path

from analytics.migrate import migrate_rows
from analytics.model import read_csv_rows

FIXTURES = Path("tests/fixtures/job_analytics")


class TrackerMigrationTests(unittest.TestCase):
    def test_migration_preserves_rows_and_normalizes_fields(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date", "fit_rating"})
        migrated = migrate_rows(legacy)
        self.assertEqual(len(migrated), len(legacy))
        self.assertEqual(migrated[0]["discovered_at"], legacy[0]["date"])
        self.assertEqual(migrated[0]["fit_score"], "92")
        self.assertEqual(migrated[0]["fit_label"], "Strong")
        self.assertEqual(migrated[0]["role_family"], "forward_deployed")
        self.assertEqual(migrated[1]["stage"], "closed")
        self.assertEqual(migrated[2]["screening_decision"], "rejected")
        self.assertEqual(len({row["application_id"] for row in migrated}), len(migrated))

    def test_migration_is_idempotent(self):
        legacy = read_csv_rows(FIXTURES / "legacy_tracker.csv", {"date"})
        once = migrate_rows(legacy)
        twice = migrate_rows(once)
        self.assertEqual(once, twice)
```

- [ ] **Step 2: Run the migration tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_tracker_migration -v
```

Expected: import failure for `analytics.migrate`.

- [ ] **Step 3: Implement deterministic migration**

`analytics/migrate.py` must:

- accept both the 13-column legacy schema and the normalized schema;
- split `fit_rating` with `r"(\d{1,3})(?:/100)?\s*(.*)"`;
- infer role families from ordered keyword sets: `forward deployed|deployment` → `forward_deployed`, `security` → `ai_security`, `platform|infrastructure|sdk|developer tooling` → `ai_platform`, `applied ai|ai engineer|agent` → `applied_ai`, otherwise `other`;
- infer stage from status in this order: offer, interview/intro call, rejected/closed, submitted/confirmed/outreach/form submitted/viewed, qualified/ready/queued, prospect;
- set `screening_decision` to `qualified` for submitted/interview/offer/closed-after-submission records, `rejected` for hold/skip/not-applied/location-blocked/manual-blocked records, otherwise `pending`;
- derive `submitted_at` from an explicit `SUBMITTED YYYY-MM-DD` phrase, otherwise use `discovered_at` only for rows whose stage proves submission;
- derive `status_updated_at` from the latest ISO date in `status`, falling back to `discovered_at`;
- preserve all descriptive text fields unchanged; and
- reject duplicate IDs.

Expose a CLI:

```python
if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("tracker", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    report = migrate_tracker(args.tracker, apply=args.apply)
    print(json.dumps(dataclasses.asdict(report), indent=2))
```

Create `analytics/config.json`:

```json
{
  "daily_screening_target": 100,
  "daily_submission_soft_capacity": 20,
  "max_active_applications_per_company": 2,
  "gmail_account_alias": "job-search",
  "gmail_expected_address": "candidate@example.test",
  "gmail_overlap_days": 7
}
```

- [ ] **Step 4: Run fixture tests and a dry migration against the real tracker**

Run:

```bash
python3 -m unittest tests.test_tracker_migration -v
python3 -m analytics.migrate job_search_tracker.csv
```

Expected: tests pass; dry-run reports `row_count: 99`, no duplicate-ID error, and does not change the tracker.

- [ ] **Step 5: Apply the real tracker migration and validate it**

Run:

```bash
python3 -m analytics.migrate job_search_tracker.csv --apply
python3 -c 'import csv; r=list(csv.DictReader(open("job_search_tracker.csv", encoding="utf-8"))); assert len(r)==99; assert len({x["application_id"] for x in r})==99; print("99 normalized applications")'
```

Expected: `99 normalized applications`.

- [ ] **Step 6: Commit the migration**

```bash
git add analytics/config.json analytics/migrate.py job_search_tracker.csv tests/fixtures/job_analytics/legacy_tracker.csv tests/test_tracker_migration.py
git commit -m "feat: normalize application tracker schema"
```

---

### Task 3: Lifecycle Events and Screening-Batch Ingestion

**Files:**
- Create: `analytics/events.py`
- Create: `analytics/screening.py`
- Create: `analytics/application_events.csv`
- Create: `tests/test_lifecycle_events.py`
- Create: `tests/test_screening_ingest.py`

**Interfaces:**
- Consumes: normalized tracker rows from Task 2
- Produces: `event_id(application_id, event_type, occurred_at, source_ref) -> str`
- Produces: `backfill_events(applications, now) -> list[dict[str, str]]`
- Produces: `merge_events(existing, incoming, application_ids) -> list[dict[str, str]]`
- Produces: `ingest_screening_rows(candidates, applications, events, now) -> tuple[list, list, IngestSummary]`

- [ ] **Step 1: Write failing lifecycle tests**

```python
# tests/test_lifecycle_events.py
import unittest
from datetime import datetime, timezone

from analytics.events import backfill_events, merge_events


class LifecycleEventTests(unittest.TestCase):
    def test_backfill_creates_discovery_submission_and_rejection(self):
        application = {
            "application_id": "app-1",
            "discovered_at": "2026-08-20",
            "submitted_at": "2026-08-20",
            "status_updated_at": "2026-08-21",
            "stage": "closed",
            "status": "REJECTED 2026-08-21",
            "notes": "Gmail rejection received 2026-08-21.",
        }
        events = backfill_events([application], datetime(2026, 8, 24, tzinfo=timezone.utc))
        self.assertEqual([event["event_type"] for event in events], [
            "discovered", "submitted", "rejected"
        ])

    def test_merge_is_idempotent_by_event_id(self):
        event = {
            "event_id": "evt-1",
            "application_id": "app-1",
            "occurred_at": "2026-08-20T00:00:00Z",
            "event_type": "submitted",
            "source": "tracker_backfill",
            "detail": "Submitted",
            "source_ref": "source-1",
            "created_at": "2026-08-24T00:00:00Z",
        }
        self.assertEqual(
            merge_events([event], [event], {"app-1"}),
            [event],
        )
```

- [ ] **Step 2: Write failing screening-ingest tests**

```python
# tests/test_screening_ingest.py
import unittest
from datetime import datetime, timezone

from analytics.screening import ingest_screening_rows


class ScreeningIngestTests(unittest.TestCase):
    def test_duplicate_source_url_does_not_create_second_application(self):
        existing = [{"application_id": "app-1", "source": "https://jobs.test/1"}]
        candidate = {
            "discovered_at": "2026-08-24",
            "company": "TestCo",
            "role": "Applied AI Engineer",
            "source": "https://jobs.test/1",
            "screening_decision": "qualified",
        }
        applications, events, summary = ingest_screening_rows(
            [candidate], existing, [], datetime(2026, 8, 24, tzinfo=timezone.utc)
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(summary.duplicates, 1)
        self.assertEqual(events, [])
```

- [ ] **Step 3: Run both test modules and verify failure**

Run:

```bash
python3 -m unittest tests.test_lifecycle_events tests.test_screening_ingest -v
```

Expected: imports fail for `analytics.events` and `analytics.screening`.

- [ ] **Step 4: Implement lifecycle events**

Use the exact event columns from `analytics.model.EVENT_COLUMNS`. Generate IDs from application ID, event type, timestamp, and source reference. Sort by `(occurred_at, application_id, event_type, event_id)`.

Backfill rules:

- every application gets `discovered`;
- non-empty `submitted_at` gets `submitted`;
- `CONFIRMED` or `RECEIVED` status gets `received` at `status_updated_at`;
- interview/intro-call stage or evidence gets `interview`;
- rejected status gets `rejected`;
- offer status gets `offer`;
- every event gets `source=tracker_backfill` and a hashed reference derived from the application ID plus source status text.
- dated phrases in notes create `viewed`, `follow_up`, `interview`, `received`, or `rejected` events only when the phrase and ISO date occur in the same sentence;
- undated prose never invents an event timestamp;

`merge_events` validates foreign keys and deduplicates by `event_id`.

- [ ] **Step 5: Implement candidate-batch ingestion**

`analytics.screening.ingest_screening_rows` must:

The screening input CSV header is exactly:

```csv
discovered_at,company,sector,role,role_family,role_type,geography,logistics_status,channel,screening_decision,screening_reason,fit_score,fit_label,source
```

- accept normalized candidate dictionaries;
- deduplicate first by canonical source URL, then by normalized company + role;
- create one tracker row per new candidate;
- create `discovered` and `screened` events;
- create `qualified` only when `screening_decision=qualified`;
- reject invalid decisions outside `pending|rejected|qualified`;
- never create `submitted` events; and
- return an immutable `IngestSummary(imported, duplicates, qualified, rejected)` dataclass.

Add a CLI accepting an input CSV and atomically updating the tracker and event ledger.

- [ ] **Step 6: Run focused tests and backfill the real event ledger**

Run:

```bash
python3 -m unittest tests.test_lifecycle_events tests.test_screening_ingest -v
python3 -m analytics.events backfill --tracker job_search_tracker.csv --events analytics/application_events.csv
```

Expected: tests pass; the real ledger contains exactly 99 `discovered` events, 25 `rejected` events, and at least four `interview` events.

- [ ] **Step 7: Commit lifecycle tracking**

```bash
git add analytics/events.py analytics/screening.py analytics/application_events.csv tests/test_lifecycle_events.py tests/test_screening_ingest.py
git commit -m "feat: add application lifecycle events"
```

---

### Task 4: Inception Feedback Ledger

**Files:**
- Create: `analytics/feedback.py`
- Create: `analytics/application_feedback.csv`
- Create: `tests/test_feedback_ledger.py`

**Interfaces:**
- Consumes: normalized applications and lifecycle events
- Produces: `feedback_id(application_id, occurred_at, category, source_ref) -> str`
- Produces: `validate_feedback(event, application_ids) -> None`
- Produces: `merge_feedback(existing, incoming, application_ids) -> list[dict[str, str]]`
- Produces: `seed_inception_feedback(applications, now) -> list[dict[str, str]]`

- [ ] **Step 1: Write failing validation and inception-coverage tests**

```python
# tests/test_feedback_ledger.py
import unittest
from datetime import datetime, timezone

from analytics.feedback import merge_feedback, seed_inception_feedback


class FeedbackLedgerTests(unittest.TestCase):
    def test_rejected_applications_have_at_least_one_feedback_event(self):
        applications = [
            {
                "application_id": "app-example_robotics",
                "company": "Example Robotics",
                "role": "Senior Applied AI Engineer",
                "stage": "closed",
                "status": "REJECTED 2026-08-24",
                "status_updated_at": "2026-08-24",
                "notes": "Example Robotics selected applicants closer to current project requirements.",
            }
        ]
        events = seed_inception_feedback(
            applications, datetime(2026, 8, 24, tzinfo=timezone.utc)
        )
        self.assertEqual({event["application_id"] for event in events}, {"app-example_robotics"})

    def test_excerpt_limit_and_foreign_key_are_enforced(self):
        event = {
            "feedback_id": "fb-1",
            "application_id": "missing",
            "evidence_excerpt": "x" * 281,
        }
        with self.assertRaises(ValueError):
            merge_feedback([], [event], {"app-1"})
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```bash
python3 -m unittest tests.test_feedback_ledger -v
```

Expected: import failure for `analytics.feedback`.

- [ ] **Step 3: Implement feedback validation and append-only merging**

Validation must enforce:

- exact `FEEDBACK_COLUMNS` keys;
- valid application foreign key;
- valid evidence tier, category, rule effect, and confidence range;
- excerpt length at most 280 characters;
- `resolve` requires a non-empty `resolves_feedback_id`;
- duplicate `feedback_id` or `source_ref` is idempotently ignored only when all persisted fields match; conflicting duplicates raise.

- [ ] **Step 4: Encode the inception backfill**

`seed_inception_feedback` creates:

- one `boilerplate/competition_no_specific_signal` event for every rejection without specific evidence;
- explicit scoped logistics events only when the local feedback ledger contains explicit evidence;
- explicit role-alignment events only when the local feedback ledger contains explicit evidence;
- observed interview postmortems remain local analytics state rather than hardcoded tracked fixtures;
- inferred feedback only when the tracker contains a concrete, labeled inference; and
- generic closer-match decisions remain boilerplate-only.

Required high-value actions must use these exact meanings:

1. ML/GenAI evaluation roles lead with hands-on experimentation and evaluation evidence.
2. Every headline metric includes denominator, unit of analysis, provenance, and failure-cost interpretation.
3. Lead-role evidence names team size, ownership boundary, decision, and outcome.
4. Behavioral answers name a situation, action, disagreement, and result.
5. Trade-off answers choose explicitly, state criteria, and reject an alternative.
6. Task-specific evaluation evidence outranks public benchmarks.
7. Logistics filters run before drafting and never reduce technical-fit calibration.

- [ ] **Step 5: Run the focused test and seed the real ledger**

Run:

```bash
python3 -m unittest tests.test_feedback_ledger -v
python3 -m analytics.feedback backfill --tracker job_search_tracker.csv --output analytics/application_feedback.csv
```

Expected: every rejected synthetic application ID is represented; actionable fixture events remain scoped; no excerpt exceeds 280 characters.

- [ ] **Step 6: Commit the feedback ledger**

```bash
git add analytics/feedback.py analytics/application_feedback.csv tests/test_feedback_ledger.py
git commit -m "feat: backfill application feedback evidence"
```

---

### Task 5: Feedback Rule Generation and Selection

**Files:**
- Create: `analytics/rules.py`
- Create: `analytics/feedback_rules.json`
- Create: `tests/test_feedback_rules.py`

**Interfaces:**
- Consumes: feedback dictionaries from Task 4
- Produces: `RuleContext(role_family: str, seniority: str, geography: str, stage: str)`
- Produces: `build_rules(feedback: Iterable[Mapping[str, str]]) -> list[dict[str, object]]`
- Produces: `select_rules(rules, context: RuleContext) -> list[dict[str, object]]`
- CLI: `python3 -m analytics.rules build --feedback analytics/application_feedback.csv --output analytics/feedback_rules.json`
- CLI: `python3 -m analytics.rules match --rules analytics/feedback_rules.json --role-family applied_ai --seniority senior --geography EEA --stage application`

- [ ] **Step 1: Write failing activation, boilerplate, scope, and resolution tests**

```python
# tests/test_feedback_rules.py
import unittest

from analytics.rules import RuleContext, build_rules, select_rules

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


class FeedbackRuleTests(unittest.TestCase):
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
        rules = build_rules([event()])
        selected = select_rules(
            rules,
            RuleContext("applied_ai", "senior", "EEA", "application"),
        )
        self.assertEqual(len(selected), 1)

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
        self.assertEqual(
            select_rules(
                rules,
                RuleContext("applied_ai", "senior", "EEA", "application"),
            ),
            [],
        )
```

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_feedback_rules -v
```

Expected: import failure for `analytics.rules`.

- [ ] **Step 3: Implement deterministic rule generation**

Use grouping key `(category, canonical_scope_json, required_action)`.

Activation rules:

- exclude boilerplate;
- activate one explicit or observed event with `confidence >= 0.75`;
- require two independent source references for inferred evidence;
- apply resolve events after activation events in timestamp order;
- calculate confidence as the maximum explicit/observed confidence or the mean of qualifying inferred events;
- sort output by `(status, category, rule_id)`; and
- serialize source feedback IDs in sorted order.

Scope matching treats absent dimensions as wildcards. A rule matches only when every dimension present in its scope equals the corresponding `RuleContext` value.

- [ ] **Step 4: Run the tests and build real rules**

Run:

```bash
python3 -m unittest tests.test_feedback_rules -v
python3 -m analytics.rules build --feedback analytics/application_feedback.csv --output analytics/feedback_rules.json
```

Expected: active rules include `ml_genai_evaluation`, `metric_rigor_provenance`, `leadership_people_evidence`, `communication_decision_clarity`, and `technical_depth`; no `competition_no_specific_signal` rule exists.

- [ ] **Step 5: Commit the rule engine**

```bash
git add analytics/rules.py analytics/feedback_rules.json tests/test_feedback_rules.py
git commit -m "feat: derive scoped application feedback rules"
```

---

### Task 6: Composio Gmail Parser and Deterministic Reconciliation

**Files:**
- Create: `analytics/gmail_sync.py`
- Create: `analytics/reconciliation_review.csv`
- Create: `analytics/gmail_checkpoint.json`
- Create: `tests/fixtures/job_analytics/gmail_inline.json`
- Create: `tests/fixtures/job_analytics/gmail_spilled.json`
- Create: `tests/fixtures/job_analytics/gmail_messages.json`
- Create: `tests/test_gmail_reconciliation.py`

**Interfaces:**
- Consumes: normalized tracker, event model, feedback model
- Produces: `ComposioClient.execute(slug: str, data: Mapping[str, object]) -> dict`
- Produces: `unwrap_composio_result(result: Mapping[str, object]) -> dict`
- Produces: `verify_mailbox(client, expected_address: str) -> None`
- Produces: `classify_message(message: Mapping[str, object]) -> MailSignal | None`
- Produces: `match_application(signal: MailSignal, applications) -> MatchResult`
- Produces: immutable `SyncProposal(events, feedback, tracker_updates, review_items, checkpoint)`

- [ ] **Step 1: Capture sanitized fixtures**

Create fixtures with no real message IDs, security codes, addresses, or unrelated message bodies. Include:

- inline Composio `data.messages` response;
- `storedInFile=true` response pointing to a temporary test payload;
- Example Robotics rejection;
- Example Decision rejection whose email role says US while the tracker says Berlin/London;
- Example Bank with two applications and one role-specific rejection;
- Example Equity, Example Data, and Example Mobility rejections;
- one application confirmation;
- one ambiguous same-company message; and
- one unrelated newsletter.

- [ ] **Step 2: Write failing adapter, classifier, and matcher tests**

```python
# tests/test_gmail_reconciliation.py
import json
import tempfile
import unittest
from pathlib import Path

from analytics.gmail_sync import (
    classify_message,
    match_application,
    unwrap_composio_result,
)


class GmailReconciliationTests(unittest.TestCase):
    def test_spilled_composio_output_is_loaded(self):
        with tempfile.TemporaryDirectory() as tmp:
            payload = Path(tmp) / "payload.json"
            payload.write_text(json.dumps({"data": {"messages": [{"subject": "x"}]}}))
            result = {"storedInFile": True, "outputFilePath": str(payload)}
            self.assertEqual(
                unwrap_composio_result(result)["data"]["messages"][0]["subject"],
                "x",
            )

    def test_role_specific_example_bank_rejection_matches_only_security_role(self):
        signal = classify_message({
            "subject": "Thank you for your job application!",
            "sender": "Example Bank Group <no-reply@example.test>",
            "messageTimestamp": "2026-08-21T07:35:34Z",
            "messageText": "Senior Security Engineer (Data Platform). We will not progress.",
            "messageId": "fixture-example_bank",
        })
        applications = [
            {"application_id": "ai", "company": "Example Bank Bank", "role": "AI Platform Engineer", "discovered_at": "2026-08-16"},
            {"application_id": "security", "company": "Example Bank Bank", "role": "Senior Security Engineer (Data Platform)", "discovered_at": "2026-08-16"},
        ]
        self.assertEqual(match_application(signal, applications).application_id, "security")
```

- [ ] **Step 3: Run the tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_gmail_reconciliation -v
```

Expected: import failure for `analytics.gmail_sync`.

- [ ] **Step 4: Implement the Composio adapter without a shell**

```python
class ComposioClient:
    def __init__(self, account: str = "job-search"):
        self.account = account

    def execute(self, slug: str, data):
        completed = subprocess.run(
            [
                "composio", "execute", slug,
                "--account", self.account,
                "-d", json.dumps(data, separators=(",", ":")),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        return unwrap_composio_result(json.loads(completed.stdout))
```

`unwrap_composio_result` handles inline, parallel, and `storedInFile` responses. Reject non-successful results with the Composio error string.

`verify_mailbox` executes `GMAIL_GET_PROFILE` with `user_id=me` and requires exact case-insensitive equality with the locally configured expected address.

- [ ] **Step 5: Implement message classification**

`MailSignal` fields:

```python
@dataclass(frozen=True)
class MailSignal:
    occurred_at: str
    company: str
    role: str
    event_type: str
    evidence_tier: str
    category: str
    signal: str
    excerpt: str
    required_action: str
    source_ref: str
    sender: str
    subject: str
```

Classification requirements:

- exact rejection phrases map to `event_type=rejected`;
- confirmation phrases map to `received`;
- interview invitations map to `interview`;
- generic “closer match/other candidates” maps to boilerplate;
- explicit logistics or skill language maps to the relevant category;
- excerpts are HTML-stripped, whitespace-normalized, and truncated to 280 characters;
- unrelated messages return `None`; and
- source references hash the real message ID/thread ID before persistence.

- [ ] **Step 6: Implement unique matching and ambiguity handling**

Scoring:

- canonical company exact/alias match: `0.50`;
- canonical role exact match: `0.35`;
- strong token-overlap role match: `0.25`;
- message date on/after discovery: `0.10`;
- recognized ATS/company sender: `0.05`.

Auto-match requires score `>= 0.85`, a unique best application, and a margin `>= 0.20` over second place. Otherwise return a `MatchResult` with `application_id=None`, candidates, score, and reason for the review queue.

- [ ] **Step 7: Run tests and commit reconciliation logic**

Run:

```bash
python3 -m unittest tests.test_gmail_reconciliation -v
```

Expected: all adapter, classification, role disambiguation, privacy, and ambiguity tests pass.

```bash
git add analytics/gmail_sync.py analytics/reconciliation_review.csv analytics/gmail_checkpoint.json tests/fixtures/job_analytics/gmail_inline.json tests/fixtures/job_analytics/gmail_spilled.json tests/fixtures/job_analytics/gmail_messages.json tests/test_gmail_reconciliation.py
git commit -m "feat: reconcile Gmail application feedback"
```

---

### Task 7: Atomic On-Demand Refresh

**Files:**
- Create: `analytics/refresh.py`
- Create: `tests/test_atomic_refresh.py`
- Modify: `analytics/gmail_sync.py`
- Modify: `analytics/events.py`
- Modify: `analytics/feedback.py`
- Modify: `analytics/rules.py`

**Interfaces:**
- Consumes: `SyncProposal` from Task 6
- Produces: `RefreshPaths(root, tracker, events, feedback, rules, review, checkpoint)` dataclass with `for_root(root: Path)` and `mutable_files()`
- Produces: `RefreshSummary(scanned, matched, events_added, feedback_added, tracker_updates, review_items, checkpoint)`
- Produces: `refresh(paths: RefreshPaths, client: ComposioClient | None, sync_gmail: bool, now: datetime) -> RefreshSummary`

- [ ] **Step 1: Write a failing atomicity test**

```python
# tests/test_atomic_refresh.py
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from analytics.model import (
    EVENT_COLUMNS,
    FEEDBACK_COLUMNS,
    REVIEW_COLUMNS,
    TRACKER_COLUMNS,
    write_csv_atomic,
)
from analytics.refresh import RefreshPaths, refresh

FIXED_NOW = datetime(2026, 8, 24, tzinfo=timezone.utc)


class AtomicRefreshTests(unittest.TestCase):
    def test_failure_keeps_every_original_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            paths = RefreshPaths.for_root(Path(tmp))
            application = {column: "" for column in TRACKER_COLUMNS}
            application.update(
                application_id="app-1",
                discovered_at="2026-08-24",
                company="TestCo",
                role="Applied AI Engineer",
                stage="prospect",
                status="PROSPECT",
            )
            write_csv_atomic(paths.tracker, TRACKER_COLUMNS, [application])
            write_csv_atomic(paths.events, EVENT_COLUMNS, [])
            write_csv_atomic(paths.feedback, FEEDBACK_COLUMNS, [])
            write_csv_atomic(paths.review, REVIEW_COLUMNS, [])
            paths.rules.write_text("[]\\n", encoding="utf-8")
            paths.checkpoint.write_text(
                json.dumps({"last_successful_at": None}) + "\\n",
                encoding="utf-8",
            )
            before = {path: path.read_bytes() for path in paths.mutable_files()}

            with patch(
                "analytics.refresh.validate_staged_files",
                side_effect=ValueError("bad"),
            ):
                with self.assertRaisesRegex(ValueError, "bad"):
                    refresh(paths, client=None, sync_gmail=False, now=FIXED_NOW)

            self.assertEqual(
                {path: path.read_bytes() for path in paths.mutable_files()},
                before,
            )
```

- [ ] **Step 2: Run the test and verify failure**

Run:

```bash
python3 -m unittest tests.test_atomic_refresh -v
```

Expected: import failure for `analytics.refresh`.

- [ ] **Step 3: Implement staged refresh orchestration**

Refresh order inside one temporary directory:

1. copy current mutable files;
2. optionally verify mailbox and fetch metadata with a seven-day checkpoint overlap;
   - When `last_successful_at` is null, start at the earliest `discovered_at` in the tracker and perform the inception scan.
3. fetch full content only for candidate messages;
4. build a `SyncProposal`;
5. apply high-confidence tracker updates;
6. append lifecycle events;
7. append feedback events;
8. append review-queue items;
9. rebuild feedback rules;
10. validate schemas, unique IDs, foreign keys, checkpoint monotonicity, and privacy limits;
11. replace all destination files; and
12. return the summary.

If any step fails, replace nothing. Use `os.replace` only after all staged files validate.

- [ ] **Step 4: Add the refresh CLI and dry-run mode**

```text
python3 -m analytics.refresh --sync-gmail --dry-run
python3 -m analytics.refresh --sync-gmail
```

Dry-run performs the live read and prints the proposed summary without replacing files or advancing the checkpoint.

- [ ] **Step 5: Run tests and verify the live mailbox gate**

Run:

```bash
python3 -m unittest tests.test_atomic_refresh tests.test_gmail_reconciliation -v
python3 -m analytics.refresh --sync-gmail --dry-run
```

Expected: tests pass with `candidate@example.test`; a separately authorized local dry-run reports scan/match/review counts and writes nothing.

- [ ] **Step 6: Commit atomic refresh**

```bash
git add analytics/refresh.py analytics/gmail_sync.py analytics/events.py analytics/feedback.py analytics/rules.py tests/test_atomic_refresh.py
git commit -m "feat: add atomic analytics refresh"
```

---

### Task 8: Deterministic Analytics Snapshot Builder

**Files:**
- Create: `dashboard/__init__.py`
- Create: `dashboard/build.py`
- Create: `tests/test_dashboard_build.py`

**Interfaces:**
- Consumes: all normalized local data and `analytics/config.json`
- Produces: `build_snapshot(applications, events, feedback, rules, review_items, config, today) -> dict`
- Produces: `render_dashboard(snapshot: Mapping[str, object], template: str) -> str`
- CLI: `python3 -m dashboard.build [--sync-gmail] [--today YYYY-MM-DD]`

- [ ] **Step 1: Write failing snapshot and deterministic-render tests**

```python
# tests/test_dashboard_build.py
import unittest
from datetime import date

from dashboard.build import build_snapshot, render_dashboard

APPLICATIONS = [
    {
        "application_id": "app-1",
        "company": "Alpha",
        "role": "Applied AI Engineer",
        "role_family": "applied_ai",
        "geography": "EEA Remote",
        "channel": "Ashby",
        "stage": "submitted",
        "fit_score": "90",
        "status": "SUBMITTED 2026-08-24",
        "status_updated_at": "2026-08-24",
    },
    {
        "application_id": "app-2",
        "company": "Beta",
        "role": "AI Platform Engineer",
        "role_family": "ai_platform",
        "geography": "EEA",
        "channel": "Teamtailor",
        "stage": "qualified",
        "fit_score": "85",
        "status": "QUALIFIED",
        "status_updated_at": "2026-08-24",
    },
]
EVENTS = [
    {"application_id": "app-1", "event_type": "screened", "occurred_at": "2026-08-24T08:00:00Z"},
    {"application_id": "app-1", "event_type": "submitted", "occurred_at": "2026-08-24T09:00:00Z"},
    {"application_id": "app-2", "event_type": "screened", "occurred_at": "2026-08-24T10:00:00Z"},
    {"application_id": "app-2", "event_type": "qualified", "occurred_at": "2026-08-24T10:05:00Z"},
]
FEEDBACK = [{
    "feedback_id": "fb-1",
    "application_id": "app-1",
    "category": "metric_rigor_provenance",
    "evidence_tier": "observed",
}]
RULES = [{
    "rule_id": "rule-1",
    "category": "metric_rigor_provenance",
    "status": "active",
}]


class DashboardBuildTests(unittest.TestCase):
    def test_snapshot_separates_screened_and_submitted(self):
        snapshot = build_snapshot(
            applications=APPLICATIONS,
            events=EVENTS,
            feedback=FEEDBACK,
            rules=RULES,
            review_items=[],
            config={"daily_screening_target": 100, "daily_submission_soft_capacity": 20},
            today=date(2026, 8, 24),
        )
        self.assertEqual(snapshot["today"]["screening_target"], 100)
        self.assertEqual(snapshot["today"]["screened"], 2)
        self.assertEqual(snapshot["today"]["submitted"], 1)

    def test_render_is_deterministic_and_self_contained(self):
        template = "<html><script>window.DATA=__DASHBOARD_DATA__</script></html>"
        first = render_dashboard({"b": 2, "a": 1}, template)
        second = render_dashboard({"a": 1, "b": 2}, template)
        self.assertEqual(first, second)
        self.assertNotIn("__DASHBOARD_DATA__", first)
        self.assertNotIn("https://", first)
```

Define compact module-level fixtures with two screened records, one submitted event, one feedback event, and one active rule.

- [ ] **Step 2: Run the tests and verify failure**

Run:

```bash
python3 -m unittest tests.test_dashboard_build -v
```

Expected: import failure for `dashboard.build`.

- [ ] **Step 3: Implement all snapshot contracts**

The snapshot must include:

- `meta`: generated time, data range, record counts, warnings;
- `today`: target, screened, rejected by gate, qualified, drafting, ready, submitted, follow-ups, stale, review queue;
- `funnel`: discovered, screened, qualified, submitted, responded, interviewed, offered;
- `daily_series` and `weekly_cohorts`;
- `response_metrics`: response rate, interview rate, offer rate, median time to response, median time to decision;
- `calibration`: outcomes by fit band, role family, geography, channel, logistics status, seniority;
- `feedback`: category/evidence-tier counts, active/monitor/resolved rules, lineage;
- `pipeline`: normalized application rows with age and next action;
- `data_quality`: missing scores, ambiguous statuses, duplicates, stale rows, orphaned events/feedback, review queue;
- `filters`: all distinct filter values.

Use event timestamps for time-to-stage. If a segment contains fewer than five submitted applications, include `insufficient_sample=true` and do not emit a conversion conclusion.

- [ ] **Step 4: Implement deterministic template rendering and CLI orchestration**

- Serialize JSON with `sort_keys=True`, compact separators, and safe `<` escaping as `\u003c`.
- Require exactly one `__DASHBOARD_DATA__` marker.
- Write `dashboard/index.html` atomically.
- When `--sync-gmail` is present, call `analytics.refresh.refresh` before loading data.
- Default `today` to the local calendar date; tests pass `--today` or a date object.

- [ ] **Step 5: Run focused tests**

Run:

```bash
python3 -m unittest tests.test_dashboard_build -v
```

Expected: all aggregation and rendering tests pass.

- [ ] **Step 6: Commit snapshot generation**

```bash
git add dashboard/__init__.py dashboard/build.py tests/test_dashboard_build.py
git commit -m "feat: build application analytics snapshot"
```

---

### Task 9: Self-Contained Interactive Dashboard

**Files:**
- Create: `dashboard/template.html`
- Create: `dashboard/index.html`
- Modify: `tests/test_dashboard_build.py`

**Interfaces:**
- Consumes: embedded `window.__JOB_ANALYTICS__` snapshot from Task 8
- Produces: directly openable, keyboard-accessible dashboard
- JavaScript functions: `applyFilters(state)`, `renderCommandCenter(data)`, `renderFunnel(data)`, `renderTimeSeries(data)`, `renderCalibration(data)`, `renderFeedback(data)`, `renderPipeline(data)`, `renderDataQuality(data)`

- [ ] **Step 1: Extend tests for self-containment and required landmarks**

Add assertions that generated HTML:

- has no `src="http`, `href="http`, `@import`, or external font reference;
- contains `main`, `nav`, one `h1`, filter labels, and a live status region;
- contains `prefers-reduced-motion`;
- contains the seven required renderer names; and
- contains no unexpanded data marker.

Run:

```bash
python3 -m unittest tests.test_dashboard_build -v
```

Expected: failures because the template does not exist.

- [ ] **Step 2: Build the semantic shell and design tokens**

Use CSS custom properties for a restrained light/dark command-center palette, spacing, typography, borders, focus rings, and status colors. Use system fonts only. Required regions:

```html
<body>
  <a class="skip-link" href="#main">Skip to analytics</a>
  <header class="site-header">
    <p class="eyebrow">Application intelligence</p>
    <h1>Job search command center</h1>
    <p id="snapshot-meta"></p>
  </header>
  <nav aria-label="Dashboard sections">
    <a href="#command-center">Today</a>
    <a href="#funnel">Funnel</a>
    <a href="#calibration">Calibration</a>
    <a href="#feedback">Feedback</a>
    <a href="#pipeline">Pipeline</a>
    <a href="#data-quality">Data quality</a>
  </nav>
  <main id="main">
    <section id="command-center"><h2>Today</h2><div id="command-kpis"></div></section>
    <section id="funnel"><h2>Funnel and velocity</h2><div id="funnel-chart"></div></section>
    <section id="calibration"><h2>Fit calibration</h2><div id="calibration-chart"></div></section>
    <section id="feedback"><h2>Feedback intelligence</h2><div id="feedback-list"></div></section>
    <section id="pipeline"><h2>Pipeline explorer</h2><div id="pipeline-table"></div></section>
    <section id="data-quality"><h2>Data quality</h2><div id="quality-list"></div></section>
  </main>
  <div id="status" role="status" aria-live="polite"></div>
</body>
```

Use a dense 12-column desktop grid, 6-column tablet grid, and single-column layout below 720px. Cards must not rely on shadow-only separation.

- [ ] **Step 3: Implement global filtering as one state transition**

The filter state contains date range, role family, geography, channel, stage, fit band, evidence tier, and feedback category. `applyFilters` returns filtered application IDs first; every view derives from that same set. Reset restores the full snapshot. Update the live region with the visible application count.

- [ ] **Step 4: Implement native SVG charts with text equivalents**

Create reusable functions for:

- funnel bars;
- daily/weekly line series;
- fit-band conversion bars;
- feedback-category bars; and
- pipeline aging distribution.

Each chart must have:

- `<svg role="img" aria-labelledby="chart-title chart-description">`;
- title and description nodes;
- visible axis labels;
- a sibling text summary/table; and
- no animation when reduced motion is requested.

- [ ] **Step 5: Implement the command center and feedback lineage**

Command-center cards show `screened / 100`, gate rejections, qualified, drafting, ready, submitted, follow-ups, stale applications, and ambiguous matches. Feedback rows expand to show evidence tier, excerpt, required action, source application, confidence, and active/monitor/resolved state.

- [ ] **Step 6: Implement the pipeline explorer and data-quality actions**

- searchable table;
- sortable headers implemented as real buttons with `aria-sort`;
- pagination at 50 rows;
- direct source links with safe `rel="noreferrer"`;
- status text plus icon/label, not color alone;
- data-quality issues linking to the affected row or review item; and
- clear empty states for every filtered view.

- [ ] **Step 7: Generate and test the dashboard**

Run:

```bash
python3 -m dashboard.build --today 2026-08-24
python3 -m unittest tests.test_dashboard_build -v
```

Expected: `dashboard/index.html` is generated; tests pass; the file contains all 99 applications and no external dependency.

- [ ] **Step 8: Verify the actual surface in a browser**

Open `dashboard/index.html` with the browser tool and verify:

- no console errors;
- desktop at 1440×1000;
- narrow mobile at 390×844;
- keyboard traversal through filters, nav, expandable feedback, and sortable table;
- reset and combined filters;
- funnel and KPI updates;
- no-data filter state;
- data-quality list; and
- screenshot appearance at desktop and mobile widths.

- [ ] **Step 9: Commit the dashboard**

```bash
git add dashboard/template.html dashboard/index.html tests/test_dashboard_build.py
git commit -m "feat: add job application analytics dashboard"
```

---

### Task 10: Feed Scoped Rules into `/apply`

**Files:**
- Modify: `.claude/commands/apply.md`
- Modify: `.claude/skills/job-application-assistant/SKILL.md`
- Modify: `.claude/skills/job-application-assistant/03-writing-style.md`
- Modify: `.claude/skills/job-application-assistant/04-job-evaluation.md`

**Interfaces:**
- Consumes: `python3 -m analytics.rules match` JSON output
- Produces: applicable-rule checklist passed to drafter, reviewer, and final verification

- [ ] **Step 1: Add feedback-rule selection after posting parse**

Insert a new `/apply` step after input parsing and before fit evaluation:

```markdown
## Step 0.5: Load Relevant Feedback Rules

Classify the posting's role family, seniority, geography, and current stage. Run the selector with those parsed values. For a Senior Applied AI role in the EEA, the exact command is:

python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage application

Use the same enumerated values for other postings. Keep the returned JSON in context. Do not apply rules whose scope does not match. If no rules match, continue without inventing lessons.
```

- [ ] **Step 2: Calibrate fit evaluation**

Add to `04-job-evaluation.md`:

- show raw fit score as a relevance score, not a hiring probability;
- list logistics separately from technical fit;
- show applicable historical rules and evidence count;
- warn that the current dataset has not shown meaningful outcome separation by raw fit score; and
- recommend `do not apply` when a hard logistics gate fails, regardless of technical score.

- [ ] **Step 3: Add evidence-defensibility rules to drafting**

Add to `03-writing-style.md`:

- every metric must include denominator, unit of analysis, provenance, and failure-cost interpretation;
- every metric must pass an interview derivation test;
- Lead-role claims name team size, ownership boundary, decision, and outcome;
- behavioral examples use situation, action, disagreement, and result;
- trade-off claims state the selected option, criteria, and rejected alternative; and
- task-specific evaluation evidence takes precedence over generic benchmark claims.

- [ ] **Step 4: Pass the applicable checklist to the reviewer**

Extend the reviewer prompt with an `Applicable Historical Rules` section. Require each rule to return one of:

- `addressed` with exact draft evidence;
- `not_applicable` with scope reason; or
- `blocked` because the candidate lacks defensible evidence.

A blocked rule cannot be fixed by fabricating experience.

- [ ] **Step 5: Extend final verification output**

Add a table:

```markdown
| Rule | Status | Evidence / Reason |
|---|---|---|
| metric_rigor_provenance | addressed | CV bullet states 87 documents, field-level F1, model-derived labels, and false-accept rate |
```

Record the IDs of rules that affected the application in the tracker notes when the application row is created or updated.

- [ ] **Step 6: Update the skill entry point and verify consistency**

Update `SKILL.md` so the quick workflow names the feedback-rule step. Read all four modified files and confirm the same command, role-family labels, rule statuses, and stage names appear everywhere.

- [ ] **Step 7: Exercise rule selection against three contexts**

Run:

```bash
python3 -m analytics.rules match --rules analytics/feedback_rules.json --role-family applied_ai --seniority senior --geography EEA --stage application
python3 -m analytics.rules match --rules analytics/feedback_rules.json --role-family ai_platform --seniority lead --geography EEA --stage application
python3 -m analytics.rules match --rules analytics/feedback_rules.json --role-family ai_security --seniority senior --geography US --stage application
```

Expected: applied-AI output includes metric/evaluation rules; Lead output includes leadership evidence; US output includes scoped logistics warnings when supported, with no unrelated global deficit rule.

- [ ] **Step 8: Commit workflow integration**

```bash
git add .claude/commands/apply.md .claude/skills/job-application-assistant/SKILL.md .claude/skills/job-application-assistant/03-writing-style.md .claude/skills/job-application-assistant/04-job-evaluation.md
git commit -m "feat: apply historical feedback to job applications"
```

---

### Task 11: End-to-End Refresh, Documentation, and Release Verification

**Files:**
- Modify: `README.md`
- Modify: `dashboard/index.html`
- Modify only if verification exposes defects: files from Tasks 1–10

**Interfaces:**
- Verifies the complete user flow: Composio Gmail → tracker/events/feedback/rules → dashboard → `/apply` rule selection

- [ ] **Step 1: Document the two operating commands**

Add a concise README section:

```markdown
## Application analytics

Refresh from local data:

python3 -m dashboard.build

Read new recruiter feedback from the Composio `job-search` Gmail connection, reconcile high-confidence matches, queue ambiguous messages, rebuild feedback rules, and regenerate the dashboard:

python3 -m dashboard.build --sync-gmail

Open `dashboard/index.html` directly. The daily target is 100 screened opportunities; submission remains quality-gated and manual.
```

Also document the candidate-batch CSV import command from `analytics.screening` and its required header.

- [ ] **Step 2: Run the complete test suite**

Run:

```bash
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

Expected: all tests pass with zero failures and zero errors.

- [ ] **Step 3: Run an on-demand live Gmail refresh**

Run:

```bash
python3 -m dashboard.build --sync-gmail
```

Expected:

- configured mailbox confirmed;
- no complete email body persisted;
- matched messages update only unique applications;
- ambiguous messages appear in `analytics/reconciliation_review.csv`;
- checkpoint advances only after all files validate;
- `dashboard/index.html` regenerates successfully.

- [ ] **Step 4: Verify data invariants after the live refresh**

Run:

```bash
python3 -c '
import csv, json
from pathlib import Path
apps=list(csv.DictReader(Path("job_search_tracker.csv").open(encoding="utf-8")))
events=list(csv.DictReader(Path("analytics/application_events.csv").open(encoding="utf-8")))
feedback=list(csv.DictReader(Path("analytics/application_feedback.csv").open(encoding="utf-8")))
rules=json.loads(Path("analytics/feedback_rules.json").read_text(encoding="utf-8"))
ids={row["application_id"] for row in apps}
assert len(apps) >= 99 and len(ids) == len(apps)
assert all(row["application_id"] in ids for row in events)
assert all(row["application_id"] in ids for row in feedback)
assert all(len(row["evidence_excerpt"]) <= 280 for row in feedback)
assert not any(rule["category"] == "competition_no_specific_signal" for rule in rules)
print(len(apps), len(events), len(feedback), len(rules))
'
```

Expected: prints four positive counts and exits zero.

- [ ] **Step 5: Perform final browser verification**

Open the generated file with the browser tool. Verify the actual current-data surface at desktop and mobile widths, exercise every filter, inspect console output, and confirm that the command center uses 100 screened as the target while submissions remain a separate metric.

- [ ] **Step 6: Review cleanup obligations**

Confirm:

- no temporary Composio output or complete message body entered the repository;
- no duplicate legacy tracker schema remains in code or docs;
- generated dashboard matches the committed template and current data;
- `analytics/reconciliation_review.csv` contains no secrets;
- no test fixture contains real message IDs or personal content; and
- all changed commands in README execute exactly as written.

- [ ] **Step 7: Commit the verified operating workflow**

```bash
git add README.md dashboard/index.html analytics/application_events.csv analytics/application_feedback.csv analytics/feedback_rules.json analytics/reconciliation_review.csv analytics/gmail_checkpoint.json job_search_tracker.csv
git commit -m "chore: finalize application analytics workflow"
```

- [ ] **Step 8: Report release evidence**

Report:

- number of applications migrated;
- lifecycle event count;
- feedback event count by evidence tier;
- active/monitor/resolved rule counts;
- Gmail matched versus queued counts;
- complete unit-test result;
- browser verification dimensions and flows; and
- exact dashboard path.
