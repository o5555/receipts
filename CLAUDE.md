# Receipts project guidance

Read `README.md` first; it carries the current state, lanes, open decisions and next actions. This is Oscar's (Viseo AB) receipts system, not a software product: raw card statements (American Express) and Pleo exports become worklists of transactions that still need a receipt, joined against evidence in Gmail, and eventually attached in Pleo or booked in Fortnox without manual forwarding.

## Sources of truth

- Durable facts, card rules, accountant decisions and timeline: `/Users/odin/obrain/projects/pleo-api.md` (OBrain). Update OBrain when a rule or decision changes; do not keep a second copy here.
- Runtime mail access: `/Users/odin/thor/projects/email-access/gmail_dwd_read.py` (read-only list and read, needs `--reason`, no attachment download, no send). Prefer it over the claude.ai Gmail connector, which breaks across logins.
- Pleo: the `pleo` MCP server registered in Claude Code at user scope (`https://mcp.pleo.io/mcp`, OAuth callback port 19876; OAuth completes by pasting the localhost callback URL into `mcp__pleo__complete_authentication`, no tunnel needed). Viseo AB only; 5555 Media AB has no MCP. Reads are always fine. Writes (attach receipt, categorise, queue export) only after showing Oscar the batch and getting a yes. Never payouts.
- Fortnox: the `fortnox` CLI in Thor (`~/.local/bin/fortnox`), dry-run first, exact approval phrase before `--execute`. Extended 2026-08-24 with the Amex lane commands (write voucher / inbox-upload / voucher-file-connection / salary-transaction plus vouchers, inbox, salary-transactions and employees reads); the token holds all eleven scopes (`fortnox auth status` confirms; `fortnox auth setup --scopes "bookkeeping,inbox,connectfile,archive,salary,article"` re-consents if one is missing). Run fortnox calls sequentially, never parallel (rotating refresh token under a file lock).

## Commands

```bash
# Normalise Amex portal exports (dedupes overlapping statement windows by Referens)
scripts/amex_import.py data/amex/*.csv --out out/amex.normalized.json --csv out/amex.normalized.csv

# Profile a Pleo export folder (export_*.csv plus receipts/) and list rows lacking a receipt
scripts/pleo_export_profile.py data/pleo/expenses_2026-08-21 --missing-csv out/pleo-missing.csv

# Read-only Gmail receipt fetch (wraps Thor's DWD script; --reason is audited)
scripts/gmail_fetch.py links <msg_id> --reason "..."              # attachments and https links
scripts/gmail_fetch.py save  <msg_id> out/receipts --prefix 2600500 --reason "..."
scripts/gmail_fetch.py html  <msg_id> out/receipts/x.html --reason "..."   # HTML-only receipts

# Match a worklist against Gmail (read-only, cached in out/gmail-cache.json; run --dry-run first on a new ledger)
scripts/gmail_match.py out/pleo-missing-receipts-2026-08-21.csv --entity viseo --short-csv out/gmail-matches-pleo-short.csv --reason "..."
scripts/gmail_match.py --check out/receipts/MATCHING.csv --reason "..."   # regression: the known receipts must rank first

# Classify a raw Amex ledger (card rules + scripts/merchant_rules.json; --overrides = Oscar's answers as ref,tag,note)
scripts/classify.py out/amex-raw-<window>.json --prior out/amex-2026-05-06_2026-08-02.classified.json \
  --out-json out/amex-<window>.classified2.json --out-csv out/amex-<window>.classified2.csv --needs-tag-csv out/amex-needs-tag.csv

# Build the month-end Fortnox plan (writes dry-run artifacts only, no API calls; execution via the fortnox CLI after Oscar's yes)
scripts/fortnox_lane.py out/amex-<window>.classified2.csv --month YYYY-MM --out-dir out/fortnox-run-YYYY-MM \
  --receipts-dir out/receipts-amex --employee-id 02

# Fetch Gmail receipts for classified Amex business rows (read-only, audited; conservative matching)
scripts/amex_receipts.py out/amex-<window>.classified2.csv --month YYYY-MM --reason "..."

# The whole month in one command (classify + receipts + plan; prints the fortnox_execute step at the end)
scripts/month_end.py --month YYYY-MM --reason "..."

# Execute an approved plan: dry-run prints the batch and its approval code; Oscar hands the code back
scripts/fortnox_execute.py out/fortnox-run-YYYY-MM
scripts/fortnox_execute.py out/fortnox-run-YYYY-MM --execute --approve <kod>   # only after Oscar's code

# Kvittoarkiv (archive/, gitignored): one markdown page per receipt with frontmatter, Kvittotext, original file and preview
scripts/archive.py ingest pleo-export data/pleo/expenses_<date> --entity viseo        # after every new Pleo download
scripts/archive.py add <pdf> [<html>] --entity viseo --date YYYY-MM-DD --merchant "..." --amount-sek 129,00 \
  --card pleo --lane pleo --pleo-expense-id <id> --pleo-status "attached YYYY-MM-DD" --source-kind gmail --source-ref <msg_id>
scripts/archive.py sync-fortnox out/fortnox-run-YYYY-MM       # after fortnox_execute: verifikat numbers onto the pages
scripts/archive.py find --vendor wincher --month 2026-07      # search frontmatter; --json for agents
scripts/archive.py check                                       # lint plus plausibility warnings; --dry-run on any command writes nothing

# Kvittotavlan, the progress page Oscar reviews (out/dashboard.html, gitignored): rerun after every ingest, attach, month-end or execute
scripts/dashboard.py                                           # --json prints the model; --today YYYY-MM-DD fixes the date
scripts/dashboard_site.py --deploy                             # hosted version at https://kvittotavlan.vercel.app (encrypted; key in data/dashboard.key)
scripts/dashboard_site.py --print-url                          # the link with the key after #
# /plan on the same site is Kvittoplanen, rendered from docs/kvittoplanen.md: edit the markdown, then --deploy

# Offline test suite (compile + 1 283 checks + classify regression)
scripts/run_checks.sh
```

Stdlib-only Python 3, no build, no dependency manager. Classification is deliberately not in `amex_import.py`; it is a separate step that carries Oscar's tags.

## Layout

- `data/` raw inputs, gitignored. `data/amex/activity*.csv` (Amex SE portal, MM/DD/YYYY dates, sv-SE amounts with Unicode minus on credits). `data/pleo/expenses_<date>/` (Pleo Export page, Download; DD-MM-YYYY dates; receipt files named by Pleo receipt number, suffix a/b for multiple files).
- `out/` generated ledgers and queues, gitignored; `out/dashboard.html` is Kvittotavlan, regenerated by `scripts/dashboard.py`.
- `archive/` the kvittoarkiv, gitignored: `<entity>/<YYYY>/<stem>.md` plus the copied receipt files and previews, `index.md` per month. Never edit pages by hand except through `scripts/archive.py set`.
- `docs/` the published status pages: `kvittolaget.html` (overall review), `amex-kon.html`, `pleo-kon.html`. Reports, not an app.
- `reference/` the July 2026 attempt (lane proposal, closeout pack, abandoned Vercel dashboard) and research journals with Gmail message ids.

## Rules

- Tell Oscar what information or access you need before working; do not proceed on assumptions about what is in Pleo, in a mailbox, or on a card.
- Ask Oscar to tag business versus personal; never infer it for ambiguous merchants. Business card ·1022 (account -61022) defaults to business, ·2004 (-62004) to personal; Oscar's override wins. Rules for -13003 and -61006 are unresolved, see OBrain.
- Match receipts by account, amount and date. Never by the last four digits on a receipt; vendors show card tokens.
- Pleo auto-matches forwarded receipts only for charges under 40 days old; older ones must be attached on the expense.
- 5555 Media receipts must be forwarded from `oscar@5555.media`, not from viseo.se.
- Kivra fees (123,75 SEK) appear as Pleo rows without receipts and Nicolina wants a receipt for each of them; Kivra receipts exist only behind BankID in the Kivra app, so Oscar downloads them himself.
- Pleo card exports contain card purchases and payout rows only; out-of-pocket expenses appear solely as references in `Reconciled Entries`.
- Nothing here sends email, forwards to Fetch, submits Pleo expenses, or writes to Fortnox without explicit approval of the specific batch.
- Kvittotavlan (`scripts/dashboard.py`, then `scripts/dashboard_site.py --deploy`) is rerun after anything that changes `out/`, `data/pleo/` or the archive, so the page Oscar reviews never lags the data. Its numbers are computed, never typed in. The hosted page carries encrypted data only; never deploy it unencrypted and never commit `data/dashboard.key` or `out/site/`.
- Every receipt that is fetched or attached also lands in the kvittoarkiv (`scripts/archive.py add` or an ingest); `month_end.py` runs the Amex ingest itself. `scripts/archive.py check` warnings about wrong-period receipts or another company as buyer are worklist items for Oscar, not things to fix in the archive.
- Keep raw exports, receipts, PDFs and `archive/` out of git; the GitHub remote is public. Plain text, no em dashes, amounts in Swedish format (1 234,56 SEK) in anything Oscar or Trimero reads.
