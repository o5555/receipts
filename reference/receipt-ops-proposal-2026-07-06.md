---
name: "receipt-ops-automation"
description: "Automate monthly receipts across Pleo, company cards, and Fortnox review"
status: proposal
version: "v1"
date: "2026-07-06T15:32:39.599Z"
---

# Receipt Ops Automation

## Purpose

Run Oscar/Viseo monthly receipts with the least manual work while preserving accounting correctness. The system must avoid duplicate expenses, avoid claiming VAT without full invoices, and separate business expenses from personal card mistakes.

## First Principles

The unit of work is not a PDF. The unit of work is a transaction outcome:

- Existing Pleo-card expense: attach or replace receipt evidence, no reimbursement.
- Existing Pleo-card expense with wrong/denied receipt: attach the full invoice PDF, then resolve the accounting comment.
- External company-card spend such as Amex/SAS Mastercard: either create a Pleo Pocket expense if reimbursement is required, or route to Fortnox/accountant pack if Pleo is not the source of record.
- Personal card or wrong-card spend: reconcile separately, do not book as normal company expense unless reclassified by Oscar/accountant.

Pleo is only the right automation target when the transaction already exists in Pleo or Oscar explicitly wants Pocket reimbursement through Pleo. If a transaction does not exist in Pleo and Pleo forces manual amount/date/category entry, use a Fortnox/accountant review pack as the fallback route.

## Lanes

### Lane A: Pleo Card, Missing Receipt

Use when Pleo has an existing card transaction marked missing receipt.

Inputs:
- Pleo missing receipt list: vendor, date, amount, currency, user, card/org.
- Gmail search results or portal invoices.

Automation target:
- Prefer Gmail native Forward to `forward@fetch.pleo.io` when the receipt email has the correct PDF attachment.
- Use Pleo Fetch matching by amount/date/currency.
- Do not create Pocket expenses.

Agent work:
1. Scrape or manually transcribe Pleo missing receipt list.
2. Search Gmail by vendor/date/amount.
3. Build an action list with direct Gmail links and status `in-gmail`, `portal`, `skip`, or `blocked`.
4. For `in-gmail`, ask Oscar to native-forward the thread, or perform browser-controlled forward only after explicit approval.
5. For portal-only, download PDF to the shared Receipts folder, then forward/attach according to matching behavior.
6. Recheck Pleo after Fetch has had time to match.

Known exception:
- Kivra service fees, 123.75 SEK about twice per month, should not be chased. Accountant handles via Fortnox inbox.

### Lane B: Pleo Card, Denied or Wrong Receipt

Use when Pleo has an existing card transaction but the accountant denied it because Fetch attached email body/screenshot instead of full PDF invoice.

Automation target:
- Manual Pleo attachment upload on the existing expense.
- Do not re-forward to Fetch if Fetch already picked the wrong part.

Agent work:
1. Locate the proper invoice PDF in Gmail, Drive, or vendor portal.
2. Download it into the agent-readable Receipts folder.
3. Open Pleo expense in Oscar's logged-in browser.
4. Use the hidden file input for `Lägg till kvitto`; never click native file picker tiles.
5. Attach the PDF.
6. Optionally click `Klar` on the accountant comment after upload, if approved.

Guardrails:
- Keep the bad Fetch screenshot unless removal is clearly needed. Full invoice presence is the priority.
- Do not enter passwords, BankID, or financial credentials.

### Lane C: External Company Card / Pocket Expense

Use when spend is on company Amex/SAS Mastercard and must be reimbursed through Pleo Pocket.

Automation target:
- Pleo browser automation, not API and not Fetch.
- Create out-of-pocket expense only when Oscar/accountant wants Pleo reimbursement.

Agent work:
1. Confirm card and business/personal tag in dashboard.
2. Retrieve the receipt PDF from Gmail or portal.
3. In Pleo, add expense with date, amount, merchant, category, receipt.
4. Submit.
5. Verify reimbursement balance increased by exactly the amount.
6. Mark row done only after balance verification.

Important distinction:
- Fetch does not create Pocket expenses for external-card transactions.
- If the external transaction is not intended for Pleo reimbursement, route to Fortnox/accountant pack instead.

### Lane D: Fortnox / Accountant Pack Fallback

Use when Pleo is not the transaction system of record or when forcing Pleo would create manual double work.

Automation target:
- Produce review pack first, then Fortnox dry-run payloads only after accountant booking rules are known.

Agent work:
1. Export dashboard rows into:
   - business booking candidates
   - business ready with PDFs
   - missing business invoices
   - personal reconciliation
   - ignore/refund/reconcile
   - done/pending evidence
2. Include PDF folder with normalized filenames.
3. Ask accountant/Oscar for exact booking treatment before any Fortnox write.
4. Run only dry-run Fortnox payloads first.
5. Execute Fortnox writes only after Oscar approves exact payloads.

## System Architecture

### Control Plane

Use the Receipts Dashboard as the operating cockpit.

Minimum durable fields:
- `id`
- `source`
- `lane`: `pleo-card-missing`, `pleo-card-denied`, `external-card-pocket`, `fortnox-pack`, `personal-reconcile`, `skip`
- `merchant`
- `date`
- `amount`
- `currency`
- `company`: `Viseo AB`, `5555 Media AB`, or unknown
- `card_account`
- `card_ending`
- `pleO_org` where known
- `tag`: `business`, `personal`, `ignore`, `needs-tag`
- `status`: `new`, `in-gmail`, `portal`, `ready`, `submitted`, `done`, `blocked`, `skip`, `needs-review`
- `receipt_file`
- `evidence_source`
- `gmail_url`
- `portal_url`
- `action`
- `operator_note`
- `updated_at`

### CLI

Provide an agent-compatible CLI. Commands should be scriptable and idempotent.

Core commands:
- `receipts import-amex <csv>`
- `receipts import-pleo-missing <csv|json|screenshot-ocr>`
- `receipts import-pleo-denied <csv|json|manual-list>`
- `receipts list --lane <lane> --status <status>`
- `receipts search-gmail <id>`
- `receipts attach-file <id> <path>`
- `receipts make-action-list --lane pleo-card-missing`
- `receipts make-accountant-pack`
- `receipts mark <id> --status done --note ...`
- `receipts summary`

### Browser Automation

Use browser automation only for user-session tasks:
- Pleo missing/denied list scrape.
- Pleo existing-expense file upload.
- Pleo Pocket expense creation and balance verification.
- Vendor portal downloads.

Browser automation must never perform external sends, Pleo submissions, or Fortnox writes without explicit approval of the current batch.

### Gmail / Fetch

Use Gmail search to find candidate threads. The agent can prepare links and evidence, but Gmail native forwarding should be treated as an external send action.

Approved send path:
- Show exact thread/vendor/amount/date list.
- Ask Oscar to approve browser-based native forwarding or do it himself.
- Forward to `forward@fetch.pleo.io` only for existing Pleo-card expenses.

Never rely on the Gmail connector to forward attachments; it cannot send with attachments.

### Apps Script Backup

Use Apps Script only for predictable sender allowlist forwarding and only after testing in dry-run/backfill mode.

Rules:
- Preserve attachments.
- Sender allowlist only.
- Avoid old Gmail filters to prevent double/triple forwarding.
- Keep logs of forwarded message IDs.

## Monthly Cycle

1. On the first business day, import statements and/or scrape Pleo missing receipt lists.
2. Normalize rows into dashboard lanes.
3. Classify business/personal/ignore. Company cards default business after card split; Oscar override wins.
4. Resolve Pleo-card missing receipts first through Fetch/native forward.
5. Resolve denied Pleo receipts by uploading full PDFs to existing expenses.
6. For external company-card rows, decide Pocket versus Fortnox/accountant pack.
7. Chase missing business invoices.
8. Export personal reconciliation separately.
9. Produce monthly closeout summary and remaining blockers.

## Implementation Order

1. Normalize dashboard schema with lane/status model.
2. Add CLI commands for lanes and action-list generation.
3. Add Gmail search action-list helper.
4. Add accountant-pack generator as a first-class command.
5. Add Pleo browser runbooks as scripts/checklists, with approval gates.
6. Add Apps Script forwarding only after old Gmail filters are removed.
7. Add Supabase persistence when DB schema credentials are available, but do not block the workflow on Supabase.

## Safety Rules

- No Fortnox write without exact dry-run payload approval.
- No Pleo submission without approval for the batch and post-submit verification.
- No email forwarding without approval or Oscar manual action.
- No passwords, BankID, or financial credentials handled by the agent.
- Personal rows are not company expenses unless Oscar/accountant reclassifies them.
- Do not chase Kivra service fees; accountant handles via Fortnox inbox.