# Receipts

Viseo's receipts system: get every receipt to where the accountant needs it without Oscar forwarding anything by hand. Started 2026-08-21 after a full review of the old setup. This folder is the code and working data; durable rules and decisions live in OBrain at `/Users/odin/obrain/projects/pleo-api.md` and are the source of truth when the two disagree.

## State on 2026-08-21 (read this first)

- Pleo Fetch has been disconnected for both entities since 2026-04-29/30. Nothing auto-matches. Every match since came from Oscar forwarding emails to `forward@fetch.pleo.io`.
- Viseo AB upgraded Pleo from Essential to Advanced on 2026-08-21 and granted Pleo MCP access. The MCP server is registered in Claude Code at user scope (`pleo`, `https://mcp.pleo.io/mcp`, OAuth callback port 19876) and still needs Oscar's one-time login. No tunnel is needed: the session's `mcp__pleo__authenticate` tool returns an authorization URL, Oscar opens it on any device, approves, and pastes the resulting `http://localhost:19876/callback?code=...&state=...` address (the page itself fails to load, the URL is still valid) back into the session for `mcp__pleo__complete_authentication`.
- 5555 Media AB is on Pleo's free plan, has no MCP, and will most likely leave Pleo. It keeps the forward-to-Fetch path (from `oscar@5555.media`, never from viseo.se) until then.
- Oscar's mailbox `oscar.sandstrom@viseo.se` aggregates `oscar@5555.media` and the personal Gmail. Thor has read-only access to it via `/Users/odin/thor/projects/email-access/gmail_dwd_read.py` (search and read, no attachment download implemented, no send). The claude.ai Gmail connector is unreliable across logins; prefer the local script.
- The Amex feed is a manual portal CSV export. There is no machine feed for a Swedish Amex card (see OBrain source `sources/2026-08-21-pleo-alternative-research.md`).
- The accountant is Nicolina Ytre-Eide at Trimero Accounting AB. Q2 is blocked on the 2026-03-12 Bazoom receipt (59 426,85 SEK) whose invoice exists only in the Bazoom portal, and on the 104 133,43 SEK reimbursement of 2026-04-23 which Pleo paid but has not exported.

## Layout

- `data/amex/` Amex portal exports `activity*.csv` (gitignored). Drop new ones here.
- `data/pleo/` Pleo downloads, unzipped, one folder per download (gitignored).
- `out/` generated ledgers and worklists (gitignored).
- `scripts/amex_import.py` normalises Amex exports into one de-duplicated ledger. Tested on the three May to August 2026 files: 149 rows, 137 charges, 263 984,07 SEK.
- `scripts/pleo_export_profile.py` profiles a Pleo download and lists rows without receipts. Tested on the 2026-08-21 export: 181 rows, 30 without receipts.
- `scripts/gmail_fetch.py` read-only receipt fetcher on top of Thor's Gmail DWD script (`links`, `save`, `html` subcommands, `--reason` required, audited). On 2026-08-21 it saved the 9 Gmail-located Pleo receipts (Vercel, Google One x2, Firecrawl x2, Supabase, Anthropic Max, Apple, Brave) into `out/receipts/`; HTML-only receipts were rendered to PDF. `out/receipts/MATCHING.csv` maps each Pleo receipt number and expense id to the file to attach.
- `scripts/gmail_match.py` read-only Gmail receipt matcher (built 2026-08-21, 1 688 lines, stdlib plus the DWD module). Auto-detects every ledger format in `out/`, resolves the vendor via `scripts/vendor_aliases.json` (32 vendors, editable), searches a date window around the charge, scores amount (SEK or foreign) + date + sender + subject + attachment, never card digits, and routes each row: `confirm` (already forwarded to Fetch), `forward` (Pleo row under 40 days), `attach` (Pleo row 40 days or older), `pack` (tagged-business Amex row), `portal`, `none`. Caches lists and message extracts in `out/gmail-cache.json` (no body text stored). `--check out/receipts/MATCHING.csv` is the regression: 9 known receipts must rank first. `--short-csv` writes the person-facing list. Every API call carries `--reason` into Thor's audit log, retries included.
- `docs/` the three review pages as published on 2026-08-21: `kvittolaget.html` (setup review and decisions), `amex-kon.html` (Amex worklist), `pleo-kon.html` (Pleo missing-receipt worklist).
- `reference/` prior art: the June dashboard CLIs and API code, the July closeout pack README and manifest, the July lane proposal, and the research journals with Gmail message ids (`reference/evidence/`, gitignored).

## Lanes (design agreed in July 2026, mechanics updated August)

- Lane 0, prevention: reconnect Fetch, move recurring SaaS to Pleo vendor cards (Advanced allows 100), schedule automatic reimbursements.
- Lane A, Pleo card missing receipt: daily, list missing via MCP, find the PDF in Gmail, attach via MCP. Zero touch.
- Lane B, denied or wrong receipt: attach the real PDF via MCP, resolve the comment.
- Lane C, company spend on Amex: Oscar's decision pending between Pocket reimbursement, a Fortnox pack to Trimero, or moving the spend to Pleo cards. The Efter Pleo research recommends an in-house Amex lane straight into Fortnox.
- Lane D, portal-only vendors: shrink first, then Playwright with sessions from 1Password.

Safety rules: no payout or finance-impacting write without Oscar's approval per batch; Fortnox writes dry-run first; never paste credentials; personal rows are not company expenses.

## Open decisions (owner: Oscar unless noted)

1. Lane C default for Amex business spend.
2. Card rules: Platinum ·3003 (-13003) personal-only or swept for KINTO; whether -61006 and -62004 are the same card; Spotify, Audible, Kindle business or personal; the 17 needs-tag rows in `out/amex-queue-2026-05-06_2026-08-02.csv`.
3. Whether to stay on Pleo long term (Efter Pleo research says exit; Advanced was bought to clear the backlog and run the card meanwhile).
4. Trimero: cut-off day, pack versus Pocket, who runs the Pleo export, Kivra handling, which entity reimburses the Oderland invoices issued to 5555 Media AB.
5. Gmail: may the agent forward to Fetch and download attachments; allowlist versus per-batch approval.

## Next actions

1. Oscar completes the Pleo MCP OAuth (paste the callback URL, see State). Then: enumerate tools, read both the missing list and the receipt inbox, confirm the two Firecrawl partial matches, and attach the 9 files in `out/receipts/MATCHING.csv` (status `ready`) after Oscar approves the batch. The Vercel and 2026-07-12 Google One rows are past the 40-day forwarding window, so MCP attach is the only route for them.
2. Get the two missing Pleo downloads: out-of-pocket expenses for Viseo AB, and 5555 Media AB (both types).
3. Build `scripts/classify.py` (card rules plus Oscar's tags) and the queue store. `scripts/gmail_match.py` is done; its open points for Oscar: which address is linked to the Viseo AB Pleo for forwarding (the script uses `oscar.sandstrom@viseo.se`, the sender of every forward on record), whether Firecrawl and Oderland belong to 5555 Media AB (the alias table only adds a note today), and whether MCP attach should replace forwarding for all Viseo rows regardless of age.
4. Wire an OpenClaw cron for Lane A once one manual run is clean.
