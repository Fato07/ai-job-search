# /apply - Drafter-Reviewer Job Application Workflow

You are orchestrating a two-agent job application workflow. The job posting is provided below as `$ARGUMENTS` (either a URL or pasted text).

Follow these steps **exactly in order**. Do not skip steps.

**Token-efficiency rules for this workflow:**
- Never re-Read a file whose contents are already in your context from an earlier step. If you read it in Step 1, it is still available in Step 2.
- When dispatching the reviewer agent, pass draft content **inline in the agent prompt** rather than asking the agent to Read files you already have in memory.
- Run the full verification checklist exactly once, at the end (Step 6). The reviewer focuses on content critique, not verification.
- Step 5 (compile and inspect PDFs) is mandatory and non-skippable — LaTeX page-break decisions are unpredictable, and `.tex` files that look fine often produce broken PDFs (orphaned entry titles, cover letters spilling to page 2, bullet fonts mismatching).

---

## Step 0: Parse Input

- If `$ARGUMENTS` looks like a URL, use `WebFetch` to retrieve the job posting content.
- If it is pasted text, use it directly.
- Extract: **company name**, **role title**, **department** (if mentioned), **location**, and **language** of the posting (Danish or English).
- Store these for use throughout the workflow.

---

## Step 0.5: Load Relevant Feedback Rules

Classify the parsed posting with all five selector dimensions:

- `role_family`: `ai_platform | ai_security | applied_ai | forward_deployed | other`
- `seniority`: `intern | junior | mid | senior | staff | principal | lead | founding | executive`
- `geography`: `EEA | US | Helsinki/Tallinn | country-of-residence | office-required`
- `stage`: `application | screen | technical | onsite | offer | post_process`
- `employment_model`: `employee | b2b | contractor | unknown`

Use only evidence in the posting to classify these values. Use `unknown` for `employment_model` when the posting does not establish it. The current `/apply` lifecycle stage is `application`; in historical rules, `scope.stage` records where the evidence surfaced. Query all pre-offer evidence stages that can constrain application, reviewer, and interview defensibility: `application`, `screen`, and `technical`.

For a Senior Applied AI employee role in the EEA, run these exact commands with the same role family, seniority, geography, and employment model:

```bash
python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage application \
  --employment-model employee

python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage screen \
  --employment-model employee

python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage technical \
  --employment-model employee
```

Substitute only the enumerated posting values for another role. Keep every returned rule verbatim, annotate its contextual copy with `origin_stages` containing the selector stage or stages that returned it, then union the three outputs by exact `rule_id`. Do not sum `evidence_count` when deduplicating the same rule. Sort the union by `rule_id` for deterministic downstream review.

Keep this union JSON in context as the **Applicable Historical Rules** checklist for evaluation, drafting, review, and final verification. The origin stage is provenance for where the feedback surfaced, not a reason to ignore an otherwise exact role/seniority/geography/employment-model match during pre-offer preparation. Do not broaden any other scope dimension. If the union is empty, continue without inventing lessons or rules.

---

## Step 1: DRAFTER - Evaluate Fit

Read the evaluation framework:
- `.claude/skills/job-application-assistant/04-job-evaluation.md`
- `.claude/skills/job-application-assistant/01-candidate-profile.md`

Using the framework from `04-job-evaluation.md`, evaluate the job posting against the candidate's profile. If the salary lookup tool is configured, run:

```bash
python salary_lookup.py "<Company Name>" --json
```

If the posting specifies a city, add `--city "<City>"` to narrow results. Parse the JSON output and include the salary benchmark in the evaluation. If the tool is not configured or returns an error, skip the salary benchmark.

Present the evaluation to the user with:

1. **Skills match** - which required/preferred skills match vs. gaps
2. **Experience match** - how work history maps to the role
3. **Behavioral/culture match** - how behavioral profile fits the role/company culture
4. **Logistics** - a separate pass/fail assessment, never folded into technical fit
5. **Salary benchmark** - salary index for the company (if available)
6. **Raw relevance score** - a relevance score, not a hiring probability, with the current calibration warning from `04-job-evaluation.md`
7. **Applicable Historical Rules** - visible rule IDs, origin stages, categories, required actions, and evidence counts from the Step 0.5 union JSON

If a hard logistics gate fails, recommend **do not apply** regardless of the raw technical relevance score. Never change a logistics result to make the score or recommendation more favorable.

After presenting the evaluation, ask the user:
> "Should I proceed with drafting the CV and cover letter for this role?"

**If the user says no, stop here.** If yes, continue to Step 2.

---

## Step 2: DRAFTER - Draft CV + Cover Letter

You already have `01-candidate-profile.md` and `04-job-evaluation.md` in context from Step 1. **Do not re-read them.**

Read only the reference files you do not yet have:
- `.claude/skills/job-application-assistant/03-writing-style.md`
- `.claude/skills/job-application-assistant/05-cv-templates.md`
- `.claude/skills/job-application-assistant/06-cover-letter-templates.md`

Also read the most recent existing CV and cover letter files for concrete structural reference (one of each is enough):
- Read any existing `cv/main_*.tex` file as a LaTeX template reference
- Read any existing `cover_letters/cover_*.tex` or `cover_letters/Cover_*.tex` file as a template reference

Treat the Step 0.5 **Applicable Historical Rules** union JSON as a drafting checklist. Preserve each matched rule's exact `rule_id`, `origin_stages`, scope, required action, and evidence count. All `application`, `screen`, and `technical` origin rules in the exact-scoped union can constrain pre-offer drafting and interview defensibility. Address a rule only with defensible candidate evidence; leave unsupported requirements visible for review rather than fabricating experience. An empty union adds no inferred requirements.

### CV (`cv/main_<company>.tex`)
- Always in **English**
- Follow the moderncv/banking format from `05-cv-templates.md`
- Tailor the profile statement and experience bullets to the specific role
- Reframe skills and achievements to match job requirements
- Keep to 2 pages

### Cover Letter (`cover_letters/cover_<company>_<role>.tex`)
- **Match the language of the job posting** (Danish posting -> Danish cover letter, English posting -> English cover letter)
- Follow the structure from `06-cover-letter-templates.md`
- Use the `cover.cls` template
- Tailor the opening paragraph to the specific role and company
- Address to a named person if available in the posting, otherwise "Dear Hiring Manager" (or equivalent in posting language)
- Keep to approximately one page
- Any mention of agentic coding or AI tooling must reference **Claude Code** by name

Write both files to disk. Keep the exact text of both drafts in working memory — you will pass them inline to the reviewer in Step 3 and revise them in Step 4 without re-reading.

---

## Step 3: REVIEWER - Research & Critique

Use the **Agent tool** to spawn a `general-purpose` reviewer agent. The reviewer gets a fresh context, so pass the drafts **inline in the prompt** below (do not make the reviewer Read them). Scope the reviewer's file reads to content-critique essentials only — the reviewer does not need the LaTeX template files (`05`, `06`) to critique content, since those govern structural/LaTeX concerns the drafter already applied.

Replace `<COMPANY>`, `<ROLE>`, `<INSERT_JOB_POSTING_TEXT_HERE>`, `<INSERT_CV_DRAFT_HERE>`, `<INSERT_COVER_LETTER_DRAFT_HERE>`, and `<INSERT_APPLICABLE_HISTORICAL_RULES_JSON_HERE>` with actual values before dispatching.

```
You are a hiring manager proxy reviewing a job application. Your job is to make the application as targeted and compelling as possible.

## Your Tasks

### 1. Research the Company
Use WebSearch and WebFetch to research:
- The company's website, mission, and recent news
- The specific department or team (if mentioned in the posting)
- Any recent projects, press releases, or strategic initiatives relevant to the role
- Company culture and values

### 2. Read Reference Materials (content-critique only)
Read these four files — and only these — to ground your critique:
- `.claude/skills/job-application-assistant/01-candidate-profile.md`
- `.claude/skills/job-application-assistant/02-behavioral-profile.md` — use this specifically to check whether the cover letter's voice matches the candidate's natural register. A "Collaborator" PI profile, for example, should not be given a combative, solo-hero tone; a "Persuader" profile should not be given over-hedged, apologetic phrasing.
- `.claude/skills/job-application-assistant/03-writing-style.md`
- `.claude/skills/job-application-assistant/04-job-evaluation.md`

Do NOT read `05-cv-templates.md` or `06-cover-letter-templates.md` — those govern LaTeX structure the drafter already applied and are not needed for content critique.

### 3. Drafts to Review
Both drafts are provided inline below. Do NOT use the Read tool on the draft files — use these exact texts.

<CV_DRAFT file="cv/main_<COMPANY>.tex">
<INSERT_CV_DRAFT_HERE>
</CV_DRAFT>

<COVER_LETTER_DRAFT file="cover_letters/cover_<COMPANY>_<ROLE>.tex">
<INSERT_COVER_LETTER_DRAFT_HERE>
</COVER_LETTER_DRAFT>

### 4. Job Posting
<JOB_POSTING>
<INSERT_JOB_POSTING_TEXT_HERE>
</JOB_POSTING>

### 5. Applicable Historical Rules
The exact union JSON from Step 0.5 is provided below. Each rule retains where its evidence surfaced in `origin_stages`. Review every rule in this union; do not add global lessons or broaden role family, seniority, geography, or employment-model scope.

<APPLICABLE_HISTORICAL_RULES>
<INSERT_APPLICABLE_HISTORICAL_RULES_JSON_HERE>
</APPLICABLE_HISTORICAL_RULES>

### 6. Produce Feedback

Return your feedback in **three parts**:

**Part A — Structured edits (preferred format whenever possible):**
A JSON array of concrete edits the drafter can apply directly without re-reading the files. Each edit is an object:
```json
{
  "file": "cv/main_<COMPANY>.tex" | "cover_letters/cover_<COMPANY>_<ROLE>.tex",
  "old_string": "<exact text currently in the draft>",
  "new_string": "<replacement text>",
  "reason": "<one-line rationale: keyword match / company angle / reframing / style>"
}
```
Only use this format when you can quote the exact `old_string` from the drafts above. Make `old_string` unique — include enough surrounding context so it matches exactly once per file.

**Part B — Narrative suggestions (for judgment calls that are not mechanical edits):**
Prose suggestions grouped by category. Produce each category even if your finding is "no issues" — silence on a category can be mistaken for skipping it.
- **Missed keywords/requirements** — what to add and roughly where, if it cannot be expressed as a clean string replacement
- **Company/department-specific angles** — connections between experience and the company's strategic priorities, based on your research
- **Action-oriented reframing** — identify passive, generic, or low-energy statements and suggest action-oriented rewrites. Use this category especially for structural weakness that doesn't fit a single-sentence swap (e.g., "the whole opening paragraph reads as passive — restructure around your single strongest match to the posting").
- **Tone and style issues** — check against `03-writing-style.md` AND `02-behavioral-profile.md`. Flag any issues with tone, formality, or voice (cliches, hedging, over-humility, inconsistent register), and specifically flag any mismatch between the letter's voice and the candidate's natural register as described in the behavioral profile.

**Part C — Applicable Historical Rules:**
Return one row for every rule in `<APPLICABLE_HISTORICAL_RULES>`, preserving its exact `rule_id` and `origin_stages` and using exactly one status:

| Rule | Origin Stage(s) | Status | Evidence / Reason |
|---|---|---|---|
| `<rule_id>` | `<origin_stages>` | `addressed` | Exact draft text that addresses the rule |
| `<rule_id>` | `<origin_stages>` | `not_applicable` | Exact non-stage scope reason the parsed posting context does not apply |
| `<rule_id>` | `<origin_stages>` | `blocked` | Specific defensible evidence the candidate lacks |

Use only `addressed`, `not_applicable`, or `blocked`. An `addressed` row must quote exact evidence from one of the inline drafts. A `not_applicable` row must name a mismatched non-stage scope dimension; origin stage alone is not a reason to ignore a pre-offer rule. A `blocked` row must state the evidence gap and cannot be repaired by fabricating experience. If the union JSON is empty, return an empty Part C table.

**CRITICAL RULE:** All suggestions must be grounded in actual profile data. Do NOT suggest fabricating skills, experience, or achievements. If a requirement is a gap, say so honestly and suggest how to frame adjacent experience instead.

Do **not** run a verification checklist — the drafter will do that in the final step. Focus on content critique.

Return Part A, Part B, and Part C together as a single structured message.
```

---

## Step 4: DRAFTER - Revise Based on Feedback

Once the reviewer agent returns its feedback:

1. **Apply Part A (structured edits) directly with the Edit tool.** Do NOT re-read the draft files — you already have them in context from Step 2, and the reviewer's `old_string` values were quoted from that same text. For each edit in the JSON array, call `Edit` with the given `file`, `old_string`, and `new_string`. Skip any whose rationale would require fabricating content.
2. **Apply Part B (narrative suggestions)** using judgment. These need interpretation, not mechanical replacement. Walk through every Part B category the reviewer returned and address it:
   - **Missed keywords/requirements:** add the keyword or capability where it fits naturally in the CV or cover letter. Prefer the experience bullets (concrete evidence) over the profile statement (abstract claim).
   - **Company/department-specific angles:** weave the reviewer's research into the cover letter opening or motivation paragraph. Verify every company claim via WebFetch/WebSearch before including it — do not trust reviewer research at face value.
   - **Action-oriented reframing:** rewrite passive or generic phrasing (CV profile statement, cover letter opening, bullet leads). Structural weakness that the reviewer flagged without a clean JSON edit lives here.
   - **Tone and style issues:** apply the writing-style-guide fixes (no em-dashes, no cliches, no apologetic hedging, consistent first-person active voice).
   Use Edit for targeted changes; only re-read a file if an edit fails because the surrounding text has shifted.
3. Do NOT incorporate any suggestion that would fabricate skills or experience. If a posting requirement is a genuine gap, acknowledge it honestly and frame adjacent experience instead.
4. **Process Part C (Applicable Historical Rules) without changing its status vocabulary:**
   - `addressed`: retain the reviewer's exact draft evidence and confirm the quoted text remains in the final draft after edits.
   - `not_applicable`: retain the exact scope reason; do not force the rule into the draft.
   - `blocked`: retain the specific evidence gap. Do not fabricate candidate evidence or silently relabel the rule `addressed`.
   Track as **affected rule IDs** the exact `rule_id` values with final status `addressed` or `blocked`; exclude `not_applicable` rules.

After all edits are applied, the two files on disk are the final drafts.

---

## Step 5: DRAFTER - Compile & Inspect PDFs (MANDATORY)

**Never skip this step.** The `.tex` files looking fine is not sufficient — LaTeX page-break decisions are unpredictable and commonly produce broken layouts (orphaned job titles separated from their bullets, cover letters spilling to 2 pages, bullet fonts not matching body text). Compile both documents and visually verify the PDFs before presenting.

### 5a. Compile

```bash
cd cv && lualatex -interaction=nonstopmode main_<company>.tex
cd ../cover_letters && xelatex -interaction=nonstopmode cover_<company>_<role>.tex
```

- CV uses **lualatex** — pdflatex fails on modern MiKTeX with fontawesome5 font-expansion errors. lualatex handles the same sources cleanly.
- Cover letter uses **xelatex** — cover.cls requires fontspec.

If either compile fails, fix the error and re-compile until clean.

### 5b. Inspect layout

Read both PDFs via the Read tool and verify:

**CV (`cv/main_<company>.pdf`):**
- [ ] Exactly 2 pages (not 1, not 3)
- [ ] No orphaned `\cventry` titles — a job/education title line must never sit alone at the bottom of page 1 with its bullets on page 2. This is the most common failure.
- [ ] Section headings are not isolated at the top of page 2 with only 1-2 lines below
- [ ] No awkward whitespace gaps

**Cover letter (`cover_letters/cover_<company>_<role>.pdf`):**
- [ ] Exactly 1 page
- [ ] Signature block visible, not cut off or pushed to a second page
- [ ] Bullet list font matches surrounding body text (both should be Raleway-Medium)

### 5c. Iterate until clean

If the layout has problems, edit the `.tex` files and recompile. Common fixes (see `05-cv-templates.md` and `06-cover-letter-templates.md` for full details):

- **Orphaned CV entry title:** `\usepackage{needspace}` in preamble, then `\needspace{5\baselineskip}` immediately before the problematic `\cventry`
- **CV spills to page 3 with only a trailing section:** `\enlargethispage{2-3\baselineskip}` before a late section
- **Substantial content on page 3:** cut content using **relevance-weighted cutting** (see `05-cv-templates.md` → "Relevance-weighted cutting"). Score each candidate line by (a) relevance to THIS posting's keywords and responsibilities, (b) uniqueness (is it duplicated elsewhere?), (c) narrative load (does the cover letter depend on it?). Cut the lowest-total-score line first, regardless of section. Do NOT mechanically apply a static section-based priority order — an older-role bullet that hits posting keywords is worth more than a recent-role bullet that does not.
- **Cover letter itemize breaks compile or uses wrong font:** close `\lettercontent{}` before the list, wrap the list in `{\raggedright\fontspec[Path = OpenFonts/fonts/raleway/]{Raleway-Medium}\fontsize{11pt}{13pt}\selectfont \begin{itemize}...\end{itemize}\par}`
- **Cover letter spills to 2 pages:** trim using the same relevance-weighted logic. First cut: sentences that restate what a bullet already said. Second cut: a bullet that does not hit posting keywords. Last resort: a bullet that does hit posting keywords. Never reduce geometry or line spacing.

Do not proceed to Step 6 until both PDFs pass inspection.

### 5d. Clean up build artifacts

After the final clean compile, delete the `.aux`, `.log`, `.out` files (keep the `.tex` and `.pdf`).

---

## Step 6: Present Final Output

Run the full verification checklist from `CLAUDE.md` now — this is the **only** verification pass in the workflow. Re-read both files once here to verify final state on disk matches your mental model after the Step 4 and Step 5 edits.

### Verification Checklist
Report pass/fail for each item in the CLAUDE.md verification checklist (factual accuracy, targeting, consistency, quality).

### Applicable Historical Rules
Report every rule from the Step 0.5 union JSON using the reviewer's final Part C status and exact evidence, scope reason, or evidence gap:

| Rule | Origin Stage(s) | Status | Evidence / Reason |
|---|---|---|---|
| `rule-7e6e6b7cebb6f56bd63cb5e9ec90ef76ae83c225d2fdfc54411c936ea2e340e9` | `technical` | `addressed` | CV bullet states 87 documents, field-level F1, model-derived labels, and false-accept rate |

Allowed statuses are exactly `addressed`, `not_applicable`, and `blocked`. A `blocked` rule remains blocked; never create evidence during verification. Also report the ordered list of affected rule IDs (`addressed` and `blocked`; exclude `not_applicable`).

### Structured Tracker Notes
When creating or updating the application row in `job_search_tracker.csv`, use the row's current normalized `application_id` and the current normalized tracker columns. Do not write a legacy-schema row or identify the row by a legacy date/company fallback.

For an existing row, reuse its exact `application_id`. For a new normalized row, generate the ID with the current `analytics.model.stable_application_id(discovered_at, company, role)` contract and write every current normalized column.

Write the final rule table and affected IDs into the normalized row's `notes` field as structured JSON under a `feedback_rules` key:

```json
{
  "feedback_rules": {
    "context": {
      "role_family": "<enum>",
      "seniority": "<enum>",
      "geography": "<enum>",
      "stage": "application",
      "employment_model": "<enum>",
      "queried_origin_stages": ["application", "screen", "technical"]
    },
    "affected_rule_ids": ["<addressed-or-blocked-rule-id>"],
    "review": [
      {
        "rule_id": "<exact-rule-id>",
        "origin_stages": ["<application | screen | technical>"],
        "status": "addressed | not_applicable | blocked",
        "evidence_or_reason": "<exact draft evidence, non-stage scope reason, or evidence gap>"
      }
    ]
  }
}
```

Merge this object with any existing notes content without discarding unrelated notes. Preserve an empty `affected_rule_ids` list and empty `review` list when the three-stage union is empty; do not invent entries.

### Key Tailoring Decisions
Summarize 3-5 key decisions made to tailor the application:
- What was emphasized and why
- What company-specific angles were incorporated
- What the reviewer suggested that was most impactful
- Any gaps that were acknowledged or reframed

### Files Created
List the files written:
- `cv/main_<company>.tex`
- `cover_letters/cover_<company>_<role>.tex`

Tell the user: "Both files are ready for your review. Open them to check the final output before compiling."
