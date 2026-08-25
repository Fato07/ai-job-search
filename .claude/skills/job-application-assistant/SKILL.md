---
name: job-application-assistant
description: >
  Assists with job applications: evaluating job postings, tailoring CVs, writing cover letters,
  and preparing for interviews. Triggers on keywords like: job posting, job application, CV,
  cover letter, resume, interview prep, job fit, career, application, apply, ansøgning, stilling
allowed-tools: Read, Glob, Grep, WebFetch, WebSearch, Bash, Edit, Write, AskUserQuestion, Agent
framework_version: 1.3.4
---

# Job Application Assistant

---

## Workflow

When the user provides a job posting (URL or text), follow this workflow:

### Step 0: Parse the Posting
- Extract company, role, department, location, posting language, and explicit logistics constraints.
- Classify only from posting evidence; do not infer candidate experience or logistics eligibility.

### Step 0.5: Load Relevant Feedback Rules
- Classify the five selector dimensions with these exact enums:
  - `role_family`: `ai_platform | ai_security | applied_ai | forward_deployed | other`
  - `seniority`: `intern | junior | mid | senior | staff | principal | lead | founding | executive`
  - `geography`: `EEA | US | Helsinki/Tallinn | country-of-residence | office-required`
  - `stage`: `application | screen | technical | onsite | offer | post_process`
  - `employment_model`: `employee | b2b | contractor | unknown`
- The current `/apply` stage is `application`. Historical `scope.stage` is where evidence surfaced, so query `application`, `screen`, and `technical` with the same role family, seniority, geography, and employment model:

```bash
python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --feedback analytics/application_feedback.csv \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage application \
  --employment-model employee

python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --feedback analytics/application_feedback.csv \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage screen \
  --employment-model employee

python3 -m analytics.rules match \
  --rules analytics/feedback_rules.json \
  --feedback analytics/application_feedback.csv \
  --role-family applied_ai \
  --seniority senior \
  --geography EEA \
  --stage technical \
  --employment-model employee
```

- Substitute only the exact posting enum values. Keep rules verbatim, add `origin_stages` from the query provenance, union by exact `rule_id` without summing `evidence_count`, and sort by `rule_id`.
- Keep the union JSON in context through evaluation, drafting, review, and verification. All three pre-offer origin stages can constrain defensibility; do not broaden another scope dimension.
- If the three-stage union is empty, continue without inventing feedback rules or lessons.

### Step 1: Research & Evaluate Fit
- Fetch the job posting content (use WebFetch for URLs). **A 403 is not a dead end** - follow the escalation order in `09-web-research.md` before concluding a page is unavailable, and prefer the employer's own careers posting over an aggregator listing
- Keep the **full posting text verbatim** for Step 3b to archive - never a summary
- Analyze the posting for required competencies, keywords, and priorities
- Research the company (website, LinkedIn, mission, recent news), per `09-web-research.md`
- Score the posting against the candidate's profile using the framework in `04-job-evaluation.md`; present the raw score as relevance, never as a hiring probability
- Present logistics separately, show the applicable historical-rule count and evidence counts, and include the current raw-score calibration warning
- Recommend `do not apply` when a hard logistics gate fails, regardless of technical relevance
- Present the evaluation table and verdict
- Suggest whether the candidate should call the employer before applying (see `04-job-evaluation.md` for guidance)
- Ask the user if they want to proceed with an application

### Step 2: Tailor CV
- Before writing either document, derive `<company>_<role>` once by the **Subfolder naming** rule in `documents/README.md`; reuse that exact value for the CV, cover letter, and Step 3b archive path. If the rule says to stop because the derived name is empty, stop before creating any file.
- Read the most relevant existing CV variant from `cv/` as a starting point
- Follow the guidelines in `05-cv-templates.md`
- Create `cv/main_<company>_<role>.tex` with tailored content
- Adjust: profile statement, skills section, experience bullet emphasis, section order
- Draft against the exact three-stage applicable-rules union JSON. Preserve `origin_stages`; use defensible candidate evidence only and never manufacture support for a blocked rule.

### Step 3: Write Cover Letter
- Follow the writing style rules in `03-writing-style.md` (critical: no em-dashes, no cliches)
- Follow the template structure in `06-cover-letter-templates.md`
- Create `cover_letters/cover_<company>_<role>.tex`
- Ensure the letter connects specific experience to the role requirements
- Apply the evidence-defensibility requirements in `03-writing-style.md`, including metric derivation, Lead evidence, behavioral disagreement, explicit trade-offs, and task-specific evaluation.

### Step 3b: Record the Application
- Run this once both documents exist. A CV or cover letter drafted alone is not yet an application.
- Follow **`/apply` Step 6b** (`.claude/commands/apply.md`) exactly: same normalized header, same match-then-update rule, same `drafting` row, same posting archive, same prohibition on touching `job_scraper/seen_jobs.json`. It is stated there once so the two paths cannot drift. Five of its values are named in `/apply`'s own terms: `cv_file`/`cover_letter_file` are the paths written in Steps 2 and 3 here, `source` is the posting URL from Step 1, `deadline` is the application deadline from the posting text Step 1 keeps verbatim (empty when none is stated; never guess), `status_updated_at` is the current timestamp, and the posting text item 7 archives is the one Step 1 read.
- This step exists here because `/scrape` Step 5 routes straight into this skill. Without it, that path writes two documents and records nothing.

### Step 4: Review Applicable Historical Rules
- Pass the exact union JSON, including each rule's `origin_stages`, and both exact draft texts to the reviewer.
- Return one row per rule with exactly one status: `addressed` with exact draft evidence, `not_applicable` with a mismatched non-stage scope dimension, or `blocked` with the specific defensible evidence gap.
- Origin stage alone does not make a pre-offer rule `not_applicable`. A `blocked` rule remains blocked unless real candidate evidence supports it; never fabricate experience to change the status.
- Affected rule IDs are the exact IDs with final status `addressed` or `blocked`; exclude `not_applicable`.

### Step 5: Interview Preparation
- Follow the framework in `07-interview-prep.md`
- Prepare STAR-format answers for likely questions
- Identify role-specific talking points
- Draft questions the candidate should ask the interviewer

### Step 6: Verify and Record
- Present the final historical-rule table with origin stages, statuses `addressed`, `not_applicable`, or `blocked`, and exact evidence or reason.
- Reuse the exact `application_id` when updating a current normalized tracker row. For a new row, generate it with `analytics.model.stable_application_id(discovered_at, company, role)` and write every current normalized column; never write the legacy schema.
- Merge a structured `feedback_rules` object into tracker `notes` with the current five-dimension context, `queried_origin_stages: [application, screen, technical]`, affected rule IDs, each rule's origin stages, and every rule-review row. Preserve other notes and preserve empty arrays when the union is empty.

---

## Reference Files

| File | Purpose |
|------|---------|
| `01-candidate-profile.md` | Education, experience, skills, publications, awards |
| `02-behavioral-profile.md` | Behavioral assessment, strengths, ideal environments |
| `03-writing-style.md` | Tone, structure, do's and don'ts |
| `04-job-evaluation.md` | Scoring framework for job fit |
| `05-cv-templates.md` | LaTeX CV structure and tailoring rules |
| `06-cover-letter-templates.md` | LaTeX cover letter structure and tailoring rules |
| `07-interview-prep.md` | STAR examples, tough questions, roleplay guidelines |
| `08-application-forms.md` | Portal free-text fields: self-introduction, project entries, character-limited pitches |
| `09-web-research.md` | Fetching postings and company pages: trust boundary, the WebFetch 403 fallback, escalation order, claim verification |

---

## Quick Commands

The user may also ask for individual steps without the full workflow:
- "Evaluate this job posting" - Steps 0, 0.5, and 1
- "Write a CV for [company]" - Steps 0, 0.5, and 2
- "Write a cover letter for [role] at [company]" - Steps 0, 0.5, and 3
- "Help me prepare for an interview at [company]" - Step 5 only
- "What jobs should I look for?" - Career strategy discussion using profile + evaluation framework
