# Job Application Analytics and Feedback Loop Design

Date: 2026-08-24
Status: Approved in chat for specification

## 1. Problem

The repository tracks applications in `job_search_tracker.csv`, but outcomes and feedback are embedded in free-text status and notes fields. The application workflow in `.claude/commands/apply.md` performs per-application drafting and review, yet it does not systematically consume lessons from prior outcomes. Gmail contains additional status and feedback evidence that requires manual reconciliation.

The current dataset contains 99 tracker rows, including 72 submitted-like records, 25 rejections, and four interview-stage outcomes. The recorded average is 1.38 submitted applications per calendar day, with a peak of 11. Five records contain ATS spam-block signals. Existing fit scores do not separate outcomes well: rejected applications average 85.0, while non-rejected applications average 86.1.

The requested outcome is one coherent system that:

1. captures all useful feedback from inception;
2. converts evidence into bounded, reusable application rules;
3. screens 100 opportunities per day without mass-submitting low-quality applications;
4. exposes the full application funnel and feedback intelligence in a polished local dashboard; and
5. feeds relevant lessons into every future `/apply` run.

## 2. Scope Decision

The application lifecycle is the parent system:

`discover -> screen -> tailor -> submit -> status -> interview -> feedback -> calibration -> next application`

The solution will be shared across three concrete consumers:

- on-demand Gmail reconciliation;
- the analytics dashboard; and
- the `/apply` workflow.

A dashboard-only implementation is rejected because it would duplicate classification logic and would not improve future applications. A database-backed live application is also rejected for now because this is a single-user repository with fewer than 100 applications and no concurrent writers.

The selected architecture is a structured feedback ledger plus a generated, self-contained dashboard. `job_search_tracker.csv` remains the canonical application record.

## 3. Goals

### 3.1 Feedback learning

- Preserve the provenance and strength of every feedback signal.
- Separate explicit employer feedback from interview observations, inference, and boilerplate.
- Prevent generic rejection language from becoming a global application rule.
- Convert high-quality evidence into specific, testable actions for future applications.
- Apply only rules relevant to the new role, location, seniority, and application stage.

### 3.2 Daily throughput

- Screen 100 opportunities per day.
- Submit only roles that pass quality, logistics, duplication, and evidence gates.
- Keep submitted volume visible but subordinate to qualified volume and conversion.
- Prevent unattended form submission and ATS-spam behavior.

### 3.3 Analytics

- Show the full funnel from screening through offer.
- Calibrate predicted fit against actual outcomes.
- Expose actionable feedback patterns and unresolved gaps.
- Support daily operations: screening progress, drafting queue, follow-ups, stale applications, and ambiguous Gmail matches.
- Work locally without a server, account, database, or external runtime dependency.

## 4. Non-goals

- Automatically submitting applications.
- Optimizing for 100 submitted applications per day.
- Storing complete Gmail message bodies.
- Treating every rejection as evidence of a candidate weakness.
- Replacing the existing CV, cover-letter, reviewer, or PDF-verification workflow.
- Adding authentication, hosting, multi-user collaboration, or a database.
- Predicting hiring outcomes with a machine-learning model from the current small dataset.

## 5. Architecture

### 5.1 Components

1. **Application tracker**
   - Existing `job_search_tracker.csv`.
   - Remains the source of truth for one current row per discovered opportunity across screening and application stages.
   - Migrates from ambiguous free-text-only analytics to stable identity and normalized lifecycle fields while preserving evidence-rich notes.

2. **Lifecycle event ledger**
   - New append-only `analytics/application_events.csv`.
   - Stores every known stage transition and its timestamp.
   - Provides the historical event stream required for funnel, velocity, aging, and time-to-stage analytics.

3. **Feedback ledger**
   - New append-only `analytics/application_feedback.csv`.
   - Stores one row per feedback signal, not one row per application or lifecycle transition.
   - Stores short evidence excerpts and source references, never complete emails.

4. **Derived feedback rules**
   - Generated `analytics/feedback_rules.json`.
   - Contains active, scoped rules derived from ledger evidence.
   - Never edited manually.

5. **Reconciliation review queue**
   - Generated `analytics/reconciliation_review.csv`.
   - Holds Gmail messages that cannot be matched uniquely and confidently.
   - A queued item cannot update tracker status, lifecycle events, or feedback rules.

6. **On-demand Gmail synchronizer**
   - Reads the Composio Gmail connection alias `job-search`.
   - Performs metadata-first discovery and fetches full content only for candidate status/feedback messages.
   - Reconciles only high-confidence unique matches.

7. **Analytics builder**
   - Reads the tracker, lifecycle events, feedback ledger, derived rules, and review queue.
   - Produces a deterministic analytics snapshot.
   - Embeds the snapshot, styles, and interaction code into `dashboard/index.html`.

8. **Feedback-aware application workflow**
   - Extends `.claude/commands/apply.md` and the job-application skill references.
   - Loads only relevant active feedback rules after parsing a posting.
   - Requires the reviewer and final verification step to check those rules.

### 5.2 Proposed file layout

```text
analytics/
  application_events.csv
  application_feedback.csv
  feedback_rules.json
  reconciliation_review.csv
  gmail_checkpoint.json
  model.py
  gmail_sync.py
  rules.py

dashboard/
  build.py
  template.html
  index.html

tests/
  fixtures/job_analytics/
  test_analytics_model.py
  test_gmail_reconciliation.py
  test_feedback_rules.py
  test_dashboard_build.py
```

Python modules use the standard library unless an existing repository dependency already solves the need. The generated dashboard has no network dependency and does not load chart libraries, fonts, or assets from a CDN.

## 6. Data Model

### 6.1 Stable application identity

Add `application_id` to `job_search_tracker.csv`.

Format:

`app-<YYYYMMDD>-<company-slug>-<role-slug>-<short-hash>`

The ID is generated once and remains stable when display text changes. New rows require a unique ID. Every feedback event joins through this ID.

### 6.2 Normalized tracker fields

The tracker migration replaces ambiguous analytics fields rather than adding competing interpretations. All repository consumers are updated in the same change.

| Field | Purpose |
|---|---|
| `application_id` | Stable identifier. |
| `discovered_at` | Date the opportunity entered the system; migrated from the existing `date` field. |
| `company` | Canonical company display name. |
| `role` | Canonical role title. |
| `role_family` | Normalized target family such as `applied_ai`, `forward_deployed`, `ai_platform`, `ai_security`, or `other`. |
| `geography` | Normalized country/region plus remote status. |
| `logistics_status` | `pass`, `sponsorship_required`, `relocation_required`, `blocked`, or `unknown`. |
| `screening_decision` | `pending`, `rejected`, or `qualified`. |
| `screening_reason` | Normalized hard-gate reason when rejected. |
| `submitted_at` | ISO date/time when an application was submitted, blank when not submitted. |
| `stage` | `prospect`, `qualified`, `drafting`, `submitted`, `response`, `interview`, `offer`, or `closed`. |
| `status` | Human-readable current status. |
| `status_updated_at` | Timestamp of the latest known lifecycle transition. |
| `fit_score` | Nullable numeric score from 0 to 100. |
| `fit_label` | Human-readable calibration label or caveat. |
| existing descriptive fields | Sector, role type, channel, contact, notes, artifacts, and source remain available. |

### 6.3 Lifecycle event ledger

`analytics/application_events.csv` contains:

| Column | Purpose |
|---|---|
| `event_id` | Stable event identifier. |
| `application_id` | Foreign key to the tracker. |
| `occurred_at` | ISO-8601 event time. |
| `event_type` | `discovered`, `screened`, `qualified`, `drafting`, `submitted`, `received`, `viewed`, `follow_up`, `interview`, `rejected`, `withdrawn`, or `offer`. |
| `source` | `tracker_backfill`, `gmail`, `user`, `browser`, or `workflow`. |
| `detail` | Short human-readable event detail. |
| `source_ref` | SHA-256 hash of the source message, page, or workflow reference. |
| `created_at` | Ledger insertion time. |

Tracker `stage`, `status`, and `status_updated_at` reflect the latest valid event. Historical analytics read the event ledger, never reverse-engineer dates from current status text.

### 6.4 Feedback ledger columns

`analytics/application_feedback.csv` contains:

| Column | Purpose |
|---|---|
| `feedback_id` | Stable event identifier. |
| `application_id` | Foreign key to the tracker. |
| `occurred_at` | ISO-8601 event time. |
| `stage` | `application`, `screen`, `technical`, `onsite`, `offer`, or `post_process`. |
| `source` | `employer_email`, `recruiter_message`, `interview_transcript`, `candidate_postmortem`, or `tracker_backfill`. |
| `evidence_tier` | `explicit`, `observed`, `inferred`, or `boilerplate`. |
| `category` | Normalized feedback category. |
| `signal` | Short normalized finding. |
| `evidence_excerpt` | Maximum 280-character evidence excerpt. |
| `required_action` | Specific action for future applications. |
| `scope` | JSON-compatible scope encoded as a compact string: role family, seniority, geography, stage, or global. |
| `confidence` | Decimal from 0.0 to 1.0. |
| `source_ref` | SHA-256 hash of the originating message/thread/transcript reference. |
| `created_at` | Ledger insertion time. |

### 6.5 Feedback categories

Initial categories are intentionally small:

- `logistics_work_authorization`
- `role_seniority_alignment`
- `technical_depth`
- `ml_genai_evaluation`
- `metric_rigor_provenance`
- `leadership_people_evidence`
- `communication_decision_clarity`
- `company_domain_evidence`
- `portfolio_open_source_proof`
- `application_quality`
- `competition_no_specific_signal`

A new category is added only when an event cannot be represented without losing actionable meaning.

### 6.6 Evidence tiers

1. **Explicit**: the employer or recruiter states a concrete reason.
2. **Observed**: a transcript or postmortem documents a concrete answer, omission, or correction.
3. **Inferred**: the outcome suggests a possible cause but no direct evidence confirms it.
4. **Boilerplate**: generic competition, timing, or fit language with no candidate-specific information.

Evidence tier controls whether a rule can become active. Boilerplate cannot create an application rule.

## 7. Feedback Rule Generation

### 7.1 Activation rules

- One explicit event may create a role- or stage-scoped rule when the required action is concrete.
- One observed interview event may create a scoped rule when the transcript contains direct evidence.
- Inferred evidence requires at least two independent events in the same category and scope before activation.
- Boilerplate contributes to funnel analytics only.
- Logistics rules remain geography- and employment-model-specific. They never become global candidate weaknesses.
- A rule is inactive if its required action has been resolved and verified by later evidence.

### 7.2 Rule structure

Each generated rule contains:

- `rule_id`
- `category`
- `scope`
- `trigger`
- `required_action`
- `evidence_count`
- `evidence_tiers`
- `confidence`
- `source_feedback_ids`
- `last_updated`
- `status` (`active`, `monitor`, or `resolved`)

### 7.3 Initial high-value rules

The inception backfill must encode at least these evidence-backed lessons:

- For ML/GenAI evaluation roles, lead with hands-on experimentation and evaluation evidence, not only agent/security framing.
- Every headline metric must include denominator, unit of analysis, provenance, and failure-cost interpretation; the candidate must be able to derive it under probing.
- For Lead roles, provide exact team size, ownership boundary, decision, and outcome. Theory of leadership is insufficient.
- Behavioral interview answers require a named situation, action, disagreement, and result.
- Technical trade-off questions require an explicit choice, decision criteria, and rejected alternative.
- Do not rely on public benchmarks when task-specific evaluation evidence exists.
- Work-authorization and relocation failures filter roles before drafting; they do not reduce technical-fit calibration.
- Generic “closer match” rejections do not create new candidate-deficit rules.

## 8. Gmail Synchronization

### 8.1 Command behavior

The primary refresh command will support:

```text
python dashboard/build.py --sync-gmail
```

This command:

1. verifies the Composio profile for account alias `job-search` resolves to `fathindos.fd@gmail.com`;
2. loads the last successful checkpoint with a seven-day overlap window;
3. fetches candidate message metadata using application/status queries and tracked company terms;
4. fetches full message content only for likely status or feedback messages;
5. normalizes company, role, sender, timestamp, and outcome;
6. scores candidate tracker matches;
7. updates only unique high-confidence matches;
8. appends new feedback events idempotently;
9. writes ambiguous items to the review queue;
10. regenerates feedback rules and the dashboard; and
11. prints a concise reconciliation summary.

The first run starts at the earliest tracker date and performs an inception backfill.

### 8.2 Matching constraints

A message can update a tracker row only when:

- the company match is exact or a known alias;
- the role match is exact or uniquely close after normalization;
- the message date is not earlier than the application date;
- the status/outcome language is unambiguous; and
- no second tracker row has an equivalent match score.

The synchronizer records its reasons. Ambiguity always produces a review item rather than a guess.

### 8.3 Privacy and safety

- Complete Gmail bodies remain in Composio/Gmail and process memory only.
- The repository stores only a short excerpt and hashed source reference.
- Security codes, access tokens, personal addresses, and unrelated messages are never persisted.
- Sync is read-only with respect to Gmail.
- Tracker and ledger writes are atomic through temporary files and replacement.

## 9. Daily Screening Workflow

### 9.1 Target definition

The daily target is 100 opportunities screened, not 100 applications submitted.

The dashboard records separate counts for:

- discovered;
- screened;
- rejected by hard gate;
- qualified;
- drafting;
- ready for review;
- submitted;
- responded;
- interviewed; and
- offered.

### 9.2 Hard gates

An opportunity cannot enter the drafting queue when any condition fails:

- role is closed or duplicated;
- work authorization, location, or required office attendance is incompatible;
- role is outside the approved target families without an explicit strategic reason;
- company cap would exceed two concurrent active applications;
- required experience would force an unsupported claim; or
- source/posting evidence is insufficient to confirm the role.

### 9.3 Quality gate

Before submission, every application must have:

- a verified live posting;
- a logistics pass;
- at least three evidence-backed requirement matches;
- explicit handling of material gaps;
- company-specific motivation supported by verified sources;
- all relevant active feedback rules checked;
- a reviewed CV and cover letter when required; and
- the existing PDF verification pass when documents are generated.

The dashboard may display a soft capacity of up to 20 quality-passing submissions per day, but it must not create a quota that bypasses the gate.

## 10. Dashboard Information Architecture

### 10.1 Global controls

- Date range
- Role family
- Geography
- Channel
- Current stage/status
- Fit band
- Evidence tier
- Feedback category

Filters update every metric, chart, and table consistently.

### 10.2 Command center

The landing view answers what to do today:

- screened progress toward 100;
- hard-gate rejection count and reasons;
- qualified queue;
- ready-to-submit queue;
- submissions completed today;
- follow-ups due;
- stale active applications;
- unresolved Gmail reconciliation items; and
- highest-priority feedback actions before the next application.

### 10.3 Funnel and velocity

- Funnel: screened -> qualified -> submitted -> response -> interview -> offer.
- Daily and weekly volume trends.
- Rolling response and interview conversion.
- Median and distribution of time to first response and decision.
- Active pipeline aging.

### 10.4 Calibration

- Predicted fit versus actual outcome.
- Conversion by fit band.
- Conversion by role family, seniority, location, channel, and logistics status.
- Explicit comparison of technical rejections versus logistics filters.
- Data sufficiency warnings when a segment is too small for a stable conclusion.

### 10.5 Feedback intelligence

- Actionable feedback by category and evidence tier.
- Recurring gaps by role family and interview stage.
- Active, monitoring, and resolved rules.
- Evidence lineage from rule to feedback events and application.
- Boilerplate rejection count shown separately from actionable feedback.
- “What changes the next application?” panel with the exact currently active actions.

### 10.6 Pipeline explorer

A searchable, sortable table exposes:

- company and role;
- application date;
- status and age;
- role family and geography;
- channel;
- fit score;
- latest feedback signal;
- feedback evidence tier;
- next action; and
- source link.

### 10.7 Data quality

- missing or non-numeric fit scores;
- ambiguous status strings;
- applications found in Gmail but missing from the tracker;
- tracker rows with no stable ID;
- duplicate roles/URLs;
- feedback events without a valid application ID;
- stale active applications; and
- reconciliation queue size.

## 11. Visual and Interaction Requirements

- Dense but readable command-center layout.
- Clear hierarchy, restrained color, and no decorative glass effects.
- Status colors are never the only carrier of meaning.
- Keyboard-accessible controls and tables.
- Visible focus states.
- Responsive layouts for desktop, tablet, and narrow mobile widths.
- Charts rendered with native SVG and accessible text summaries.
- Reduced-motion support.
- Empty, loading, error, and no-data states for every section.
- The generated `dashboard/index.html` is self-contained and usable by opening it directly.

## 12. Integration with `/apply`

After parsing a posting, `/apply` will:

1. read `analytics/feedback_rules.json`;
2. select active rules matching role family, seniority, geography, and stage;
3. show the raw fit score and a calibration note rather than treating the score as a reliable probability;
4. include applicable rules in drafting instructions;
5. pass applicable rules to the reviewer;
6. require each rule to be marked `addressed`, `not_applicable`, or `blocked` during final verification; and
7. record which rules affected the application in tracker notes or a structured application analytics field.

The workflow must not use a rejection to invent experience. When a rule exposes a genuine gap, the workflow either presents adjacent evidence honestly or recommends not applying.

## 13. Error Handling

- Missing Composio CLI or disconnected Gmail: build from local data and report that Gmail sync was skipped.
- Wrong Gmail account: stop sync before reading messages.
- Malformed tracker row: fail the build with row and column details; do not emit a misleading dashboard.
- Ambiguous Gmail match: queue for review and continue.
- Duplicate feedback source reference: ignore idempotently.
- Invalid feedback rule: exclude it from generated output and fail tests/build with a precise error.
- Dashboard build failure: retain the last valid `dashboard/index.html`.

## 14. Verification Strategy

### 14.1 Data and unit tests

- Stable application ID generation and uniqueness.
- Existing 99-row tracker migration without row loss.
- Status normalization across current tracker conventions.
- Gmail company/role matching, including multiple applications at one company.
- Ambiguous-match rejection.
- Idempotent feedback ingestion.
- Evidence-tier activation rules.
- Boilerplate exclusion from active rules.
- Funnel, conversion, and aging calculations.
- Deterministic dashboard output from fixed fixtures.

### 14.2 Integration tests

- Cached Composio response fixture for inception backfill.
- Incremental sync with overlap and duplicate messages.
- Tracker update, lifecycle-event append, feedback append, and rule regeneration as one atomic refresh.
- Failure leaves prior files intact.

### 14.3 Browser verification

Run the generated dashboard in a real browser and verify:

- initial render without console errors;
- all global filters;
- funnel and time-series updates;
- table sorting and search;
- feedback-rule lineage;
- mobile and desktop layouts;
- keyboard navigation and focus states;
- color contrast and non-color status labels; and
- empty/error states.

## 15. Acceptance Criteria

1. All 99 current tracker rows receive stable application IDs and normalized lifecycle fields without data loss.
2. Known historical stage transitions are backfilled into the lifecycle event ledger with source provenance.
3. All inception feedback evidence is represented in the feedback ledger with provenance and evidence tier.
4. Explicit interview feedback from Nordea and observed postmortems from Wise and Dragonfly produce scoped active rules.
5. Generic rejection messages do not produce candidate-deficit rules.
6. Logistics rejections remain geography/employment-model scoped.
7. The `job-search` Composio Gmail connection can be synced on demand without persisting full message bodies.
8. Ambiguous Gmail messages cannot silently alter tracker status or append lifecycle events.
9. The dashboard opens directly as one self-contained HTML file.
10. The command center tracks progress toward 100 screened opportunities per day.
11. The dashboard exposes funnel, cohorts, calibration, feedback intelligence, pipeline, and data-quality views.
12. `/apply` consumes relevant feedback rules and reports how each was handled.
13. No automation submits applications or bypasses the quality gate.
14. All data tests pass and the dashboard is verified in a real browser.

## 16. Revisit Triggers

Migrate from CSV/JSON files to SQLite only when at least one condition becomes true:

- more than 10,000 applications;
- multiple users or concurrent writers;
- transactional joins become unreliable or slow;
- hosted access is required; or
- scheduled unattended synchronization becomes a firm requirement.

The design is falsified if structured feedback cannot improve rule relevance without frequent manual correction. In that case, keep the ledger for analytics but stop feeding derived rules into `/apply` until the evidence and scoping model are corrected.
