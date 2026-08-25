# /html-report - Build the Local Analytics Dashboard

Build the existing self-contained analytics dashboard from the normalized local ledgers. This command is a thin route to the reviewed Python implementation; it does not maintain a second CSV parser or status model.

## Step 0: Parse Arguments

- No argument: build from local state.
- `--sync-gmail`: refresh Gmail through the configured, mailbox-gated analytics reconciliation flow before building.
- `--today YYYY-MM-DD`: pass the explicit snapshot date through for deterministic local verification.
- Reject any other argument. The output is always `dashboard/index.html`.

## Step 1: Run the Reviewed Builder
Run `python3 -m analytics.init` before building so a fresh clone has canonical empty inputs and malformed existing state fails explicitly.


Run:

```bash
python3 -m dashboard.build [--sync-gmail] [--today YYYY-MM-DD]
```

`dashboard.build` strictly reads the normalized 24-column tracker, including final `deadline`, through `analytics.model.read_tracker_rows`. It also reads `analytics/application_events.csv`, feedback, rules, and the reconciliation review queue. The event ledger is the lifecycle history: funnels come from recorded lifecycle events rather than inferring earlier stages from current status. Rejection reporting keeps candidate-initiated outcomes such as withdrawal and declined offers distinct from employer rejection.

Do not reimplement CSV parsing, status normalization, HTML escaping, lifecycle aggregation, or report rendering in this command. Legacy 13/14-column trackers must first be migrated with:

```bash
python3 -m analytics.migrate job_search_tracker.csv --apply
```

## Step 2: Confirm

Report the builder's JSON summary and the local output path:

> **Dashboard generated:** `dashboard/index.html`
>
> Open it directly in a browser. It is self-contained and makes no external requests.

The generated file contains personal application data and remains ignored by git.

## Important Rules

1. **Read-only source data unless Gmail sync was explicitly requested.**
2. **One operating schema.** Never accept, append, or patch a legacy tracker header in this command.
3. **Local-only output.** Never commit `dashboard/index.html` or any analytics state ledger.
4. **No duplicate semantics.** Dashboard behavior belongs to `dashboard.build` and its tests.
