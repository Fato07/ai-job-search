# Job Evaluation Framework

<!-- SETUP: Skill match areas and career goals are personalized by running /setup -->

## Scoring Dimensions

Evaluate each job posting against these five dimensions:

### 1. Technical Skills Match (0-100)
How well do the required/preferred skills align with the candidate's capabilities?

| Score | Meaning |
|-------|---------|
| 80-100 | Core requirements are primary skills |
| 60-79 | Most requirements match, 1-2 gaps that are learnable |
| 40-59 | Partial match, significant upskilling needed |
| 0-39 | Fundamental mismatch |

**Strong match areas:** agentic AI (multi-agent orchestration, tool-calling, agent memory), MCP server design, RAG, LLM integration & multi-model routing, eval harnesses/benchmarks, Python (FastAPI/Django), TypeScript/Next.js, LLM/agent security (OWASP LLM Top-10, threat modeling)
**Moderate match areas:** Go, Kubernetes (fundamentals), classic MLOps/model-serving, AWS/Azure depth, large-scale distributed data infra
**Weak match areas:** ML research / model training from scratch, deep-learning research, published academic ML, mobile/native app dev

### 2. Experience Match (0-100)
Does work history align with what they're looking for?

| Score | Meaning |
|-------|---------|
| 80-100 | Direct experience in the same domain and role type |
| 60-79 | Related experience, transferable skills clear |
| 40-59 | Adjacent experience, would need to make the case |
| 0-39 | Unrelated experience |

**Strong:** production AI agents & RAG for enterprises; secure full-stack delivery (Python + Next.js); founder-level architecture-to-deployment; agent identity/verification; vertical AI in regulated domains (aviation, fintech, healthcare)
**Moderate:** frontend leadership/perf (fintech), voice AI integration, developer-tooling/MCP, GTM/knowledge products
**Entry-level:** formal ML research, very-large-org platform SWE, roles requiring years in a single big-tech codebase

### 3. Behavioral/Culture Fit (0-100)
Does the role and company culture match the behavioral profile?

| Score | Meaning |
|-------|---------|
| 80-100 | Culture strongly matches behavioral preferences |
| 60-79 | Mixed signals but mostly compatible |
| 40-59 | Some friction areas |
| 0-39 | Significant culture mismatch |

**Red flags to research:** Department disorganization, work dominated by maintenance over development, poor chemistry with leadership, culture mismatches. Check reviews, media coverage, LinkedIn connections, and network contacts for insider perspective.

### 4. Location & Logistics (Pass/Fail + Notes)
- Within commute range: PASS
- Remote with occasional office: PASS
- Requires relocation: FLAG (remote-first; only for exceptional roles/comp)
- Frequent international travel: FLAG (discuss with user)
- A confirmed work-authorization barrier, mandatory location/attendance requirement the candidate cannot meet, or required employment model the candidate cannot use: **FAIL**
- Missing logistics evidence: **FLAG/UNKNOWN**, never a guessed pass

A hard logistics **FAIL** produces a `do not apply` recommendation regardless of technical relevance. Keep logistics separate; do not lower or inflate a technical score to encode a logistics result.

### 5. Career Alignment & Motivation (0-100)
Does this role advance career goals and contain tasks that energize?

| Score | Meaning |
|-------|---------|
| 80-100 | Strongly aligned with career direction, clear growth path |
| 60-79 | Good role but only partially aligned with long-term goals |
| 40-59 | Decent job but doesn't build toward career goals |
| 0-39 | Dead end or backwards step |

**Career goals:**
- Become a recognized Applied/Agentic AI Engineer (or Forward-Deployed Engineer) at a top AI company, shipping production agent systems
- Grow into Staff/Founding-Engineer scope: own agent architecture, evals, and security end-to-end
- Maximize compensation via US/UK-remote or contract while keeping high autonomy; deepen the AI-security niche as a durable moat

**Motivation filter:** Evaluate not just whether you *can* do the tasks, but whether the tasks will *energize* you. Consider:
- Tasks that energize: greenfield agent/RAG systems, MCP/tool design, eval & benchmark work, threat-modeling and hardening, direct founder/user contact, shipping thin verticals fast
- Tasks that drain: pure maintenance of legacy code, rigid process-heavy orgs, low-autonomy ticket-shuffling, work with no measurable outcome
- Non-task factors: leadership style, department culture, company values, degree of autonomy

**Life situation alignment:** Consider personal constraints:
- **Security**: runs a B2B entity (CodesDevs); values strong cash comp; open to equity at the right stage
- **Flexibility**: fully remote, EEA (Tallinn) timezone; can flex to US/UK overlap for the right role
- **Professional development**: wants frontier agent/eval/security problems and peers who raise the bar

### 6. Salary Benchmark (Optional)

If the salary lookup tool is configured (`salary_data.json` exists), look up the company:
```
python salary_lookup.py "<Company Name>" --json
```

If a city is known from the posting, add `--city "<City>"` to narrow results.

Present findings as:
```
### Salary Benchmark
| Metric | Value |
|--------|-------|
| [Category] index | XX.X (+/-X.X% vs baseline) |
| Overall index | XX.X (+/-X.X% vs baseline) |
```

Interpret results relative to the baseline defined in the data file's metadata. For index-based data, higher typically means above-market compensation.

If the salary tool is not configured, skip this section.

## Score Calibration and Historical Feedback

- The weighted raw fit score is a **relevance score**, not a hiring probability, interview probability, or calibrated forecast.
- Always show logistics separately from the scored dimensions. A technically strong role can still be a logistics failure.
- Include this warning with every score: **Current raw-score calibration warning: the current application dataset has not shown meaningful outcome separation by raw fit score.**
- Use the exact three-stage applicable-rules union loaded in Step 0.5. Historical `scope.stage` records where evidence surfaced, so query `application`, `screen`, and `technical` with the same role family, seniority, geography, and employment model. Union by `rule_id` without summing `evidence_count`.
- Show the union's matched-rule count, every rule's `origin_stages`, and every rule's `evidence_count`; do not infer a rule when all three selector outputs are `[]`.
- Preserve the posting context across `role_family`, `seniority`, `geography`, and `employment_model`. Origin stage alone does not disqualify a pre-offer rule, but no other exact scope dimension may be broadened.
- Carry each matched rule's exact `rule_id`, origin stages, category, required action, scope, and evidence count into drafting and review. The downstream reviewer vocabulary is exactly `addressed`, `not_applicable`, and `blocked`; evaluation must not invent evidence to pre-label a rule `addressed`.

## Output Format

Present the evaluation as:

```
## Job Fit Evaluation: [Role] at [Company]

### Raw Relevance

| Dimension | Score | Notes |
|-----------|-------|-------|
| Technical Skills | XX/100 | [brief note] |
| Experience Match | XX/100 | [brief note] |
| Behavioral Fit | XX/100 | [brief note] |
| Career Alignment | XX/100 | [brief note] |

**Raw Relevance Score: XX/100** (weighted average of scored dimensions; not a hiring probability)

**Current raw-score calibration warning:** the current application dataset has not shown meaningful outcome separation by raw fit score.

### Logistics

| Gate | Status | Evidence |
|------|--------|----------|
| Location / attendance | PASS / FLAG / FAIL | [posting and candidate evidence] |
| Work authorization | PASS / FLAG / FAIL | [posting and candidate evidence] |
| Employment model | PASS / FLAG / FAIL | [employee / b2b / contractor / unknown and evidence] |

**Hard logistics result:** [PASS / FAIL]. If `FAIL`, the recommendation is `do not apply` regardless of the Raw Relevance Score.

### Applicable Historical Rules

**Matched union rules: N; total evidence count: N**

| Rule ID | Origin Stage(s) | Category | Evidence Count | Required Action |
|---------|-----------------|----------|----------------|-----------------|
| [exact `rule_id`] | [application / screen / technical] | [category] | [rule `evidence_count`] | [exact required action] |

If all three selector outputs are `[]`, report `Matched union rules: 0; total evidence count: 0` and an empty table. Do not invent a lesson.

### Verdict: [Strong Fit / Good Fit / Moderate Fit / Weak Fit / Poor Fit]

### Key Strengths for This Role
- [bullet points]

### Gaps to Address
- [bullet points]

### Recommendation
[1-2 sentences: apply/skip/apply with caveats]

### Company Research Checklist
- [ ] Checked company website (mission, values, recent news)
- [ ] Checked review sites (Glassdoor, Jobindex, etc.)
- [ ] Checked LinkedIn for team size, recent hires, connections
- [ ] Checked media for restructuring, growth, or workplace issues
- [ ] Identified network contacts who may know the team/manager
```

## Weighting
- Technical Skills: 30%
- Experience Match: 25%
- Behavioral Fit: 15%
- Career Alignment: 30%

(Location is pass/fail, not weighted)

## Thresholds
- **Strong Fit** (75+): High relevance; apply when logistics passes and tailor everything
- **Good Fit** (60-74): Good relevance; apply when logistics passes and address gaps in the cover letter
- **Moderate Fit** (45-59): Moderate relevance; consider carefully and discuss with the user
- **Weak Fit** (30-44): Low relevance; probably skip unless strategic reasons
- **Poor Fit** (<30): Very low relevance; skip

These are relevance bands, not outcome probabilities. A hard logistics `FAIL` overrides every band with `do not apply`.

## Pre-Application: Call the Employer (Best Practice)

Before writing the application, consider whether the candidate should call the contact person listed in the posting. **Only call if there are substantive questions** - never call just to "be remembered."

### When to Suggest Calling
- The posting has unclear or ambiguous requirements
- It's unclear which competencies are essential vs. nice-to-have
- The role description is vague about day-to-day tasks
- There's a named contact person who invites questions

### Good Questions to Ask
- "What are the primary challenges in this role?"
- "How is time typically divided across the listed responsibilities?"
- "Which competencies are most critical for success in this position?"
- "What does success look like in the first 6-12 months?"

### Rules for the Call
- Prepare a 30-second "elevator pitch" about your background in case they ask
- The call's purpose is **gathering information**, not delivering a pitch
- Take notes - use what you learn to tailor the application
- Reference the conversation naturally in the cover letter ("After speaking with [name], I was especially drawn to...")
