# /gmail-sync - Reconcile Gmail with Application Analytics

Use the reviewed analytics Gmail adapter and atomic refresh transaction. Do not maintain a second message parser, tracker writer, checkpoint, or status convention in this command.

## Step 0: Preflight
Run `python3 -m analytics.init` first. It creates or validates the ignored tracker and ledgers and fails closed on malformed existing state.


1. Require the local ignored file `analytics/config.json`.
2. If it is missing or still contains example values, tell the user exactly:

   > Copy `analytics/config.example.json` to `analytics/config.json`, then edit `gmail_account_alias` and `gmail_expected_address`.

3. Confirm `job_search_tracker.csv` and the canonical analytics ledgers exist. Do not originate an application here.
4. Never run a live Gmail sync in tests. Tests use synthetic `candidate@example.test` and UTC.

## Step 1: Run the Reviewed Sync

Run:

```bash
python3 -m analytics.refresh --sync-gmail
```

The implementation verifies the configured Composio account alias and exact mailbox profile before any message search. Its queries include archived mail while excluding sent mail and drafts. It hashes source identities, strips addresses and sensitive payloads, applies high-confidence receipt/interview/rejection lifecycle matches atomically, and sends ambiguous messages to `analytics/reconciliation_review.csv`.

**Offer gate:** after sanitization, every standalone `offer`, `offered`, `offering`, or `offers` token enters reconciliation with reason `offer_requires_manual_confirmation`, even for unique high-confidence matches and even when the phrase concerns feedback, accommodations, assistance, or support. Gmail never writes tracker/events/feedback for these messages. This conservative false-positive tradeoff eliminates natural-language offer false negatives: ignore non-job review items, or after human confirmation record a genuine offer only through `/outcome` / `python3 -m analytics.record transition`.

For automatic non-offer matches, the normalized writer updates only `stage`, `status`, `status_updated_at`, and `submitted_at`. It preserves all non-lifecycle tracker fields, including `deadline`, stable `application_id`, screening data, fit data, notes, and document paths. It never appends or patches a legacy header.

## Step 2: Present the Result

Report the command's JSON summary:

- messages scanned and matched;
- lifecycle events and feedback added;
- tracker rows updated;
- pending reconciliation items;
- checkpoint timestamp.

Do not display raw message bodies, Gmail message IDs, email addresses, or unredacted subjects/senders.

## Step 3: Review Ambiguous Messages

If `analytics/reconciliation_review.csv` has pending rows, present only the redacted review fields and stable `review_id`. Ask the user whether each item should be resolved or ignored. Record the explicit decision with one of:

```bash
python3 -m analytics.refresh --review-id review-<sha256> --review-status resolved
python3 -m analytics.refresh --review-id review-<sha256> --review-status ignored
```

Resolving review state does not invent a tracker match. If the evidence does not uniquely identify an application, leave the tracker unchanged and keep the item pending until the user supplies enough context.

## Step 4: Rebuild the Dashboard

After a successful sync or review decision, run:

```bash
python3 -m dashboard.build
```

Report `dashboard/index.html` as the local output.

## Important Rules

1. **Gmail is read-only.** Never send, label, archive, delete, or modify mailbox content.
2. **Mailbox gate first.** Never search messages before alias and exact-address verification.
3. **One normalized writer.** All tracker writes go through `analytics.refresh`; never edit CSV rows here.
4. **Atomic state.** Tracker, events, feedback, rules, review queue, and checkpoint advance together or not at all.
5. **Privacy.** All Gmail-derived state and the dashboard are local ignored files and must never be committed.
6. **No live-network tests.** Exercise fixtures and synthetic accounts only.
