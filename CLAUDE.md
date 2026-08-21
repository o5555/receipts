# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A personal receipts/expense-reconciliation workspace for Oscar (Viseo AB), not a software product. It turns raw card statements (American Express) and Pleo expense exports into worklists of transactions that still need a receipt, and joins them against evidence found in Gmail. The scripts are small stdlib-only Python 3; there is no build, test suite, linter, or dependency manager.

## Commands

```bash
# Normalise Amex portal exports (dedupes overlapping statement windows by Referens)
scripts/amex_import.py data/amex/*.csv --out out/amex.normalized.json --csv out/amex.normalized.csv

# Profile a Pleo export folder (export_*.csv + receipts/) and list rows lacking a receipt
scripts/pleo_export_profile.py data/pleo/expenses_2026-08-21 --missing-csv out/pleo-missing.csv
```

Both print a summary to stderr/stdout and accept no other configuration.

## Layout and data flow

- `data/` — raw inputs, gitignored. `data/amex/activity*.csv` are Amex SE portal downloads (MM/DD/YYYY dates, sv-SE amounts with Unicode minus on credits). `data/pleo/expenses_<date>/` is an unzipped Pleo "Export page → Download" (DD-MM-YYYY dates; receipt files named by Pleo receipt number, suffix `a`/`b` for multiple files).
- `scripts/` — parsers only. Classification (business / personal / ignore) is deliberately **not** done in `amex_import.py`; `*.classified.*` outputs in `out/` came from a separate step and carry the tag, the reason (`why`), whether the row is already in Pleo, and the dashboard id.
- `out/` — generated ledgers and queues (`amex-*.normalized.*`, `amex-*.classified.*`, `amex-queue-*.csv`, `pleo-missing-receipts-*.csv`), gitignored.
- `docs/` — self-contained HTML status pages written in Swedish titles (`kvittolaget.html` = overall state, `amex-kon.html` = Amex queue, `pleo-kon.html` = Pleo queue). They are reports, not an app.
- `reference/` — history from the July 2026 attempt: `receipt-ops-proposal-2026-07-06.md` (the lane model below), `expense-closeout-pack-2026-07-06/` (accountant pack, CSVs gitignored), `receipts-dashboard-vercel/` (an abandoned Vercel/Supabase dashboard with a `bin/receipts` CLI; JSON state gitignored), and `reference/evidence/` (workflow journals, gitignored).

## Domain rules worth knowing

- Card accounts: `-61022` (card ending 1022) is the business Amex, `-62004` (ending 2004) is personal. Business card defaults to business; Oscar's override wins.
- The unit of work is a transaction outcome, not a PDF. Lanes from the proposal: Pleo-card missing receipt (forward to `forward@fetch.pleo.io` / Pleo Fetch), Pleo-card denied receipt (upload full invoice PDF to the existing expense), external-card spend (Pleo Pocket reimbursement only if wanted, otherwise Fortnox/accountant pack), personal reconciliation kept separate.
- Kivra service fees (123.75 SEK, ~twice a month) are never chased; the accountant handles them.
- Pleo card-expense exports contain card purchases and payout rows only; out-of-pocket expenses appear solely as references in `Reconciled Entries`.
- Nothing in this repo should send email, forward to Fetch, submit Pleo expenses, or write to Fortnox without explicit approval of the specific batch. Scripts and pages are read-only worklists by design.
- Amounts are SEK unless a `foreign` object/`Orig. currency` says otherwise; keep sv-SE parsing (`parse_amount` / `amt`) when adding new inputs.
