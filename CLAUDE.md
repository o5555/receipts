# Receipts project guidance

Read `README.md` first; it carries the current state, lanes, open decisions and next actions. This is Oscar's (Viseo AB) receipts system, not a software product: raw card statements (American Express) and Pleo exports become worklists of transactions that still need a receipt, joined against evidence in Gmail, and eventually attached in Pleo or booked in Fortnox without manual forwarding.

## Sources of truth

- Durable facts, card rules, accountant decisions and timeline: `/Users/odin/obrain/projects/pleo-api.md` (OBrain). Update OBrain when a rule or decision changes; do not keep a second copy here.
- Runtime mail access: `/Users/odin/thor/projects/email-access/gmail_dwd_read.py` (read-only list and read, needs `--reason`, no attachment download, no send). Prefer it over the claude.ai Gmail connector, which breaks across logins.
- Pleo: the `pleo` MCP server registered in Claude Code at user scope (`https://mcp.pleo.io/mcp`, OAuth callback port 19876; OAuth completes by pasting the localhost callback URL into `mcp__pleo__complete_authentication`, no tunnel needed). Viseo AB only; 5555 Media AB has no MCP. Reads are always fine. Writes (attach receipt, categorise, queue export) only after showing Oscar the batch and getting a yes. Never payouts.
- Fortnox: the `fortnox` CLI in Thor (`~/.local/bin/fortnox`), dry-run first, exact approval phrase before `--execute`. Token lacks bookkeeping and inbox scopes today.

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
scripts/gmail_match.py --check out/receipts/MATCHING.csv --reason "..."   # regression: the 9 known receipts must rank first
```

Stdlib-only Python 3, no build, no dependency manager. Classification is deliberately not in `amex_import.py`; it is a separate step that carries Oscar's tags.

## Layout

- `data/` raw inputs, gitignored. `data/amex/activity*.csv` (Amex SE portal, MM/DD/YYYY dates, sv-SE amounts with Unicode minus on credits). `data/pleo/expenses_<date>/` (Pleo Export page, Download; DD-MM-YYYY dates; receipt files named by Pleo receipt number, suffix a/b for multiple files).
- `out/` generated ledgers and queues, gitignored.
- `docs/` the published status pages: `kvittolaget.html` (overall review), `amex-kon.html`, `pleo-kon.html`. Reports, not an app.
- `reference/` the July 2026 attempt (lane proposal, closeout pack, abandoned Vercel dashboard) and research journals with Gmail message ids.

## Rules

- Tell Oscar what information or access you need before working; do not proceed on assumptions about what is in Pleo, in a mailbox, or on a card.
- Ask Oscar to tag business versus personal; never infer it for ambiguous merchants. Business card ·1022 (account -61022) defaults to business, ·2004 (-62004) to personal; Oscar's override wins. Rules for -13003 and -61006 are unresolved, see OBrain.
- Match receipts by account, amount and date. Never by the last four digits on a receipt; vendors show card tokens.
- Pleo auto-matches forwarded receipts only for charges under 40 days old; older ones must be attached on the expense.
- 5555 Media receipts must be forwarded from `oscar@5555.media`, not from viseo.se.
- Kivra fees (123,75 SEK) do appear as Pleo rows without receipts and the accountant has listed them as missing; the old "never chase Kivra" rule is unconfirmed until Trimero answers.
- Pleo card exports contain card purchases and payout rows only; out-of-pocket expenses appear solely as references in `Reconciled Entries`.
- Nothing here sends email, forwards to Fetch, submits Pleo expenses, or writes to Fortnox without explicit approval of the specific batch.
- Keep raw exports, receipts and PDFs out of git. Plain text, no em dashes, amounts in Swedish format (1 234,56 SEK) in anything Oscar or Trimero reads.
