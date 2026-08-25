# /notion-sync - Push Ranked Jobs and Applications to a Notion Database

You are publishing a **read-only view** of the job search into the user's Notion workspace: one database row per job, with a detailed page per shortlisted match. The repo files stay the system of record - `job_scraper/seen_jobs.json` owns scraped/ranked jobs and `job_search_tracker.csv` owns applications. Notion is a disposable presentation layer on top of them; nothing ever syncs back.

This command requires the **Notion MCP server** (OAuth). It reads state, upserts pages, and stops - it never ranks, applies, or edits repo files. Notion is the in-tree reference binding; the sync contract itself is tool-agnostic (see "Adapting to Another Tool" at the end - only the two sections marked *(Notion binding)* are tool-specific).

## Lane: `/html-report` vs `/notion-sync`

Both present the same tracker data; they own different moments. `/html-report` is the **deep-review lane**: a self-contained offline dashboard with charts and a filterable table, regenerated at your desk. `/notion-sync` is the **glanceable lane**: the current state of the pipeline, reachable anywhere Notion runs (desktop, web, phone). They compose rather than compete - after `/outcome` records a result, re-run either or both to refresh the views.

Follow these steps **in order**.

---

## Step 0: Parse Input

`$ARGUMENTS` may contain:

- Nothing → sync ranked jobs with score ≥ 60 (Good Fit and above) plus every tracked application
- `--min-score <N>` → override the score threshold
- `--all` → sync every ranked job regardless of score
- `--rebuild` → re-fetch and rewrite page bodies too (see Step 5 - normally bodies are write-once)

---

## Step 1: Preflight the Connection *(Notion binding)*

The command is **silently optional**: when the destination is not reachable, the outcome is one clear message and a clean exit - nothing else in the framework notices this command exists.

1. Check that Notion MCP tools are available in this session (tool names starting with `mcp__notion__` or similar). Determine this from the session's own tool list **only** - never by running shell commands like `claude mcp list`, which would interrupt the user with a permission prompt before the graceful exit. If the tools are not available, stop and tell the user how to connect:
   > Notion MCP isn't connected. Run `claude mcp add --transport http notion https://mcp.notion.com/mcp`, then start a **new session** (servers added mid-session are only picked up on restart), run `/mcp` there to complete the OAuth login, and re-run `/notion-sync`.
2. Verify the connection with one cheap call (e.g. a workspace search). An auth error → tell the user to re-authenticate via `/mcp` and stop. Never retry in a loop.
3. The Notion MCP server is interactively authenticated, so "connected but not authenticable right now" (expired OAuth, headless/CI context where the login flow cannot run) gets the same graceful exit as "not configured": state the reason in one line and stop. This includes the configured-but-unauthenticated state where the server exposes only its auth handshake and no data tools - **never initiate the OAuth flow from this command and never ask whether to authenticate now**; the one line points at `/mcp` and the command ends there. Authenticating is the user's move, made outside this command.

---

## Step 2: Build the Sync Set (local data only - no external calls yet)

Validate the cheap, local precondition before creating anything external. A run with nothing to sync must exit with **zero side effects** - no database created, no state file written.

1. Read `job_scraper/seen_jobs.json` and `job_search_tracker.csv` (either may be missing). A tracker that exists must have this exact canonical header:
   `application_id,discovered_at,company,sector,role,role_family,role_type,geography,logistics_status,channel,screening_decision,screening_reason,submitted_at,stage,status,status_updated_at,contact_person,fit_score,fit_label,notes,cv_file,cover_letter_file,source,deadline`
   If it is legacy, stop and run `python3 -m analytics.migrate job_search_tracker.csv --apply`; never patch a header or read a second operating schema.
2. Select `seen_jobs.json` entries with status `ranked` whose `rank_score` meets the threshold from Step 0. `--all` lifts the threshold entirely.
3. Every tracker row joins the sync set, keyed as `application:<application_id>`. Match to `seen_jobs.json` case-insensitively on company + role only to enrich the row; never replace or derive the stable tracker identity. Ranked-only jobs use `seen:<seen_jobs key>`.
4. **Status precedence:** the tracker wins. Use its normalized `stage` and `status`; jobs only in `seen_jobs.json` keep their stored status. **Deadline precedence: the tracker wins too** - the tracker's `deadline` overrides the scraper value even when it is empty, because unknown deadline is represented by an empty canonical field. Jobs only in `seen_jobs.json` keep the scraper's stored deadline. Omit the property when neither states one, and **never reconcile the two by picking the earlier or later date**.
5. **If the sync set is empty** (no ranked entries meet the threshold and there are no tracker rows), say "Nothing to sync - run `/scrape` and `/rank` first" (or, when jobs exist but all score below the threshold, say so and suggest `--min-score`/`--all`) and **stop**.
6. State the counts before touching the destination: how many rows will be created or checked, and the threshold in effect.

---

## Step 3: Load Sync State and Locate the Database *(Notion binding)*

1. Read `job_scraper/notion_sync.json`. Structure:
   ```json
   { "database_id": "...", "database_url": "...", "last_sync": "YYYY-MM-DD" }
   ```
2. If it exists, verify the database id still resolves in Notion. If the database was deleted, treat this as a first run.
3. **First run:** search the workspace for a database named "Job Search Pipeline". If none exists, ask the user where to create it (top-level page or an existing page they name), then create it with exactly these properties:

   | Property | Type | Values / notes |
   |----------|------|----------------|
   | Name | title | `<Role> — <Company>` |
   | Company | rich text | |
   | Score | number | tracker `fit_score`, falling back to ranked-only `rank_score`; blank when neither is numeric |
   | Verdict | select | tracker `fit_label`, falling back to Strong Fit / Good Fit / Moderate Fit / Weak Fit / Poor Fit |
   | Status | select | `ranked` / `drafted` / `applied` / `interview` / `offer` / `hired` / `rejected` / `no_response` / `offer_declined` / `withdrawn` / `expired`; normalized tracker `status` wins |
   | Fit | select | high / medium / low (scraper quick-fit) |
   | Deadline | date | tracker `deadline` column, falling back only for ranked-only jobs; omit when unknown |
   | First seen | date | tracker `discovered_at`, or scraper first-seen date |
   | Ranked | date | `rank_date` from `seen_jobs.json`; omit when not ranked |
   | Applied on | date | tracker `submitted_at`; omit when empty, and omit when the `stage` is `drafting` |
   | Channel | select | tracker `channel` column (e.g. portal / email / referral); options grow as values appear |
   | CV file | rich text | tracker `cv_file` column - the filename only, never document content |
   | Cover letter | rich text | tracker `cover_letter_file` column - the filename only, never document content |
   | URL | url | tracker `source`, falling back to the ranked posting URL |
   | Key | rich text | `application:<application_id>` or `seen:<seen_jobs key>`; stable dedup anchor, never edited by hand |

   The tracker-sourced properties stay empty when their canonical fields are empty. CV file and Cover letter fill in once `/apply` records the draft; Applied on stays empty until `submitted_at` is recorded by `/outcome`. Only filenames ever sync; document contents stay local.

4. **Existing database with missing properties:** if the located database predates a schema addition (a property from the table above does not exist), add the missing properties to the database before upserting. Never remove or retype existing properties.
5. Write `job_scraper/notion_sync.json` with the database id and URL. This file is personal state and is gitignored - never commit it.

---

## Step 4: Upsert Database Rows

For each job in the sync set:

1. Query the database for a page whose `Key` equals the job's key.
2. **No match** → create the page with all properties from the Step 3 table, then write its body (Step 5).
3. **Match** → update **properties only**: Status, Score, Verdict, Deadline, Ranked, Applied on, Channel, CV file, Cover letter. Properties are the always-current surface (bodies are write-once), so tracker updates recorded by `/outcome` reach the destination exclusively through them. Do not touch the page body - the user may have added their own notes there, and clobbering them breaks trust in the whole view. (`--rebuild` is the sole exception.)
4. Never delete or archive pages, even for jobs that turned `expired` - set Status to `expired` instead. Rows the user added to the database by hand (no `Key` value) are invisible to this command.

**Normalise the Status value before writing.** The tracker may hold legacy space spellings (`no response`, `offer declined`) from before the canonical forms were locked. Map them to `no_response` / `offer_declined` per the **Tracker status vocabulary** in `/outcome` before setting Status on create or update - never push a space form to Notion, which would auto-create a separate select option per unique string. Pre-existing space-form options in an existing database simply go unused; Notion never auto-removes select options.

Batch politely: if the MCP server rate-limits, back off and continue; report any page that failed rather than retrying indefinitely.

---

## Step 5: Write the Detail Page (new pages only)

The page body is what makes a row worth clicking. Build it **only from stored data and actually fetched content**:

1. **Fit summary** - a short section from stored score/verdict/first-seen/ranked fields. If the job is in the tracker, add the application timeline from `discovered_at`, `submitted_at`, `status_updated_at`, `stage`, `status`, and dated `notes`; name `cv_file`/`cover_letter_file` by filename only. **When `stage` is `drafting`, write "drafted YYYY-MM-DD, not yet submitted" instead of an applied date, and call the files drafts rather than submitted documents.**
2. **The posting** - WebFetch the job URL and write a readable digest: what the role is, key requirements, practical details (location, deadline, salary if stated). Retry a 403 with browser headers per `.claude/skills/job-application-assistant/09-web-research.md` first. If the fetch still fails or redirects to a listing page, write "Posting no longer available (checked YYYY-MM-DD)" - **never reconstruct a posting from memory**.
3. **Links** - the posting URL; derive `<company>_<role>` by the **Subfolder naming** rule in `documents/README.md`, and if that archive exists locally, name its path (plain text - the destination cannot link into the filesystem).

Keep the page under ~40 blocks; this is a briefing, not a mirror of the posting.

---

## Step 6: Report

```
## Pipeline Sync - YYYY-MM-DD

Database: <database_url>
Synced <N> jobs (threshold: score ≥ <T>): <C> created, <U> updated, <S> unchanged, <F> failed.

| | Title | Company | Status | Score |
|---|-------|---------|--------|-------|
| ✚ | ... | ... | ranked | 78 |
| ↻ | ... | ... | interview | 71 |
```

List failures with their error and the suggestion to re-run - the upsert is idempotent, so a re-run only touches what failed. Update `last_sync` in the sync-state file.

Remind the user once (first run only): the repo files remain the source of truth - edits made in the destination never flow back, and `/outcome` is still how application results get recorded.

---

## Important Rules

1. **One-way, always.** Destination content never flows back into `seen_jobs.json`, the tracker, or any repo file. This command reads repo state and writes the destination - both repo files are read-only to it, and the gitignored sync-state file is its **only** local write.
2. **Idempotent upsert on `Key`.** Re-running creates nothing twice; matching is on the stored key, never on fuzzy title comparison.
3. **Page bodies are write-once.** Property updates keep rows current; bodies belong to the user after creation. Only `--rebuild` may rewrite them, and it says so before doing it.
4. **Never fabricate.** A dead posting URL gets an explicit "unavailable" note, not a reconstruction. Every page claim traces to stored state or fetched content.
5. **Job data only.** The candidate profile, behavioral notes, and evaluation framework never sync - this is a pipeline view, not a profile export.
6. **Documents never leave the machine.** CVs and cover letters sync as **filenames only** - never upload, attach, or embed the documents themselves, nor HTML/text renditions of their content, into the destination. The local repo and `documents/applications/` archive are the only home for application documents; the row's page names them so the user knows what to open locally.

---

## Adapting to Another Tool (forks)

The sync contract is tool-agnostic; only the two sections marked *(Notion binding)* are tool-specific. A fork targeting a different destination (Airtable, Google Sheets, Linear, ...) keeps Steps 0, 2, 4, 5, and 6 and every Important Rule unchanged - build the same sync set, upsert on the same `Key`, keep bodies write-once and documents local - and swaps only:

- **Step 1** (connection preflight) for the target tool's MCP server or access check, keeping the silently-optional bar: not configured or not authenticable both end in one message and a clean exit
- **Step 3** (locate/create the database) for the equivalent container in the target tool, using the same property table and a renamed sync-state file

Like the portal skills, tool bindings beyond this Notion reference live in forks, where their maintainers can test them against a live workspace.
