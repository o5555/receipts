# Expense closeout pack, Amex and KINTO

Generated: 2026-07-06
Source dashboard: /Users/odin/thor/reports/receipts-dashboard-vercel

## First-principles route

The sample KINTO/Oderland transactions do not exist in Pleo. That means Pleo is not the reliable source of record for this backlog. Do not manually recreate these in Pleo unless the accountant explicitly wants that workflow.

Use this pack as the accountant/Fortnox review basis. No Fortnox write has been made.

## AMEX totals

- AMEX rows: 75
- AMEX total: 187 808,20 SEK
- Business rows: 28, 126 873,12 SEK
- Personal-tagged rows: 43, 76 394,08 SEK
- Ignore/refund/reconcile status rows: 6, −15 121,89 SEK. This overlaps with personal-tagged rows where a personal card mistake is already marked ignore/reconcile.

## Business split

- Needs booking/review now: 21, 41 414,33 SEK
- Ready with PDF evidence: 11, 5 117,00 SEK
- Missing invoice/receipt: 9, 35 352,10 SEK
- Blocked: 1, 945,23 SEK
- Already done in Pleo/payout evidence: 5, 82 850,79 SEK
- Pending payout evidence: 2, 2 608,00 SEK

## Files

- all-amex-rows.csv: full AMEX queue.
- business-booking-candidates.csv: business AMEX rows not done/pending/ignored.
- business-ready-with-pdfs.csv: business rows where evidence is already available.
- business-missing-invoices.csv: business rows to fetch/request invoice for.
- missing-business-invoices.txt: short action list for invoice chasing.
- personal-reconciliation.csv: personal rows, should not be booked as company expenses without reclassification.
- personal-on-business-card.csv: personal rows on business card, likely repayment/reconciliation items.
- ignore-refunds-reconcile.csv: refunds/credits/ignore rows.
- business-done-or-pending.csv: rows already matched to Pleo/payout evidence or pending payout.
- kinto-source-matched-to-amex.csv: KINTO PDFs that match AMEX KINTO rows by date/amount.
- kinto-source-unmatched-review.csv: KINTO source receipts not present in the AMEX exports, review for reimbursement or missing AMEX export coverage.
- available-pdfs/business-amex/: PDFs already tied to business AMEX rows.
- available-pdfs/kinto-source-review/: unmatched KINTO PDFs for review.

## Recommended next actions

1. Accountant decision: confirm whether AMEX backlog should be booked directly in Fortnox instead of recreated in Pleo.
2. If yes, use business-booking-candidates.csv as the working set.
3. Chase the invoices in business-missing-invoices.csv before booking VAT-bearing expenses.
4. Treat personal-reconciliation.csv separately as owner/employee settlement, not normal company expense VAT claims.
5. Keep blocked Masinov open until the company-formatted invoice arrives.

## Guardrail

Fortnox writes require exact dry-run payload approval before execution. This pack is review material only.
