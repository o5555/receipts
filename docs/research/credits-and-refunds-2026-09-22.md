# Refunds, credit notes, partial payments and split VAT in the current data

Research note for [issue 9](https://github.com/o5555/receipts/issues/9), inventoried 2026-09-22 from the local data folders. Counts and types only; the underlying rows live in gitignored `out/`, `data/` and `archive/` and are not on GitHub.

## Sources and how each source expresses the types

- Amex raw ledger `out/amex-raw-2026-05-06_2026-09-01.csv` (205 rows). `scripts/amex_import.py` assigns `kind` per row: `charge` (190), `refund` (11), `payment` (4). Refunds and payments both carry a negative `amount`; the portal export writes credits with a Unicode minus, the normaliser turns it into a plain negative number.
- Amex classified ledger `out/amex-2026-05-06_2026-09-01.classified2.csv` (same 205 rows, no `kind` column). `scripts/classify.py` tags every refund and payment row `skip` (refunds with `why=refund`); 15 negative rows in total. No refund is carried into a Fortnox plan and none gets a business or personal tag of its own.
- Pleo export `data/pleo/expenses_2026-08-21/export_2026-01-01_2026-12-31.csv` (181 rows). Card purchases are negative amounts on `Expense Type = Card Purchase` (Unicode minus). A refund is a positive amount on a `Card Purchase` row with its own receipt number, own `Expense ID` and a positive `Tax Amount`. Payout rows are `Expense Type = Reimbursement to Oscar Sandström`, negative, with the reimbursed receipt numbers in `Reconciled Entries`. One `Tax Rate` per row.
- Kvittoarkiv `archive/` (182 pages plus 12 in `pending-replacements/`). Frontmatter `kind` is `receipt` or `credit-note`; `amount_sek` is always positive and the sign lives in `kind`. `scripts/archive.py` sets `credit-note` from a positive Pleo amount (line 1848) or a negative Amex amount (line 2063), and `check` exempts credit notes from the shared-invoice-number warning (line 869). VAT sits in `vat_rate` and `vat_sek` for Pleo pages and in `vat_regime` for Amex pages.

## Counts per type

### 1. Refunds on Amex: 11 rows, 12 030,95 SEK

- 4 on card 1022 (3 016,88 SEK), 7 on card 2004 (9 014,07 SEK). 10 domestic merchants, 1 in Ireland.
- 3 reverse a prior charge in full (same merchant, same card, same amount inside the window). 7 are smaller than the prior charge of the same merchant (partial refunds, typically a returned item). 1 has no charge in the window (a 3,57 SEK adjustment from an ad platform).
- Counterpart tags: 10 of the 11 refunds sit against charges tagged personal. 1 reverses in full a charge tagged business (Clas Ohlson, 2 499,00 SEK, `in_pleo=yes`, `pack_status=pending-payout`, kontering 5410). The refund row itself is `skip`, so the business charge stands without its credit in every downstream artifact. This is a worklist item for Oscar.
- Archive counterpart: 0 of 11. No Amex refund has a page, and none of the refunded charges has a page either (`amex_ref` lookup).

### 2. Payments received on Amex: 4 rows, 281 690,37 SEK (not refunds)

- Merchant text `BETALNING MOTTAGEN, TACK`, all on card 2004, one per statement month (May, June, July, August). `amex_import.py` marks them `kind=payment`, the classifier tags them `skip`.
- They are Oscar's payments of the card balance and must never be read as refunds or booked as costs. They have no archive counterpart by design.

### 3. Refunds in Pleo: 2 rows, 976,68 SEK

- Both are positive `Card Purchase` rows dated April 2026, both with `Tax Rate 0,25000` and a positive `Tax Amount`.
- One is a subscription refund in USD (Fireflies.ai, 271,68 SEK) against a 282,39 SEK charge a week earlier; the Stripe document carries the same invoice number as the original receipt and states the full amount refunded, the 10,71 SEK gap is FX. In SEK it therefore looks partial while it is a full refund in the vendor's currency.
- One is a partial refund from a Swedish web shop (Proffsmagasin, 705,00 SEK against 877,75 SEK).
- Archive counterpart: 2 of 2, both as `kind: credit-note` (see 5).

### 4. Payout rows in Pleo: 4 rows, 209 200,45 SEK (not refunds)

- `Reimbursement to Oscar Sandström` rows dated 2026-01-23, 2026-04-23 and two on 2026-07-09. Each lists 5 to 7 receipt numbers in `Reconciled Entries`; these are the only places the out-of-pocket expenses appear in a card export.
- 5 card rows are `Marked as personal`; they are ordinary negative purchases, not refunds.

### 5. Credit notes in the archive: 2 pages

- The two Pleo refunds above, ingested from the Pleo export as `kind: credit-note`, `vat_rate 25`, `vat_sek` 67,92 and 141,00.
- The Fireflies page holds a real refund document (Stripe "Refund" receipt with "Total refunded without credit note"). The Proffsmagasin credit-note page holds the original order receipt, not a credit document, so the archive has 1 genuine credit document and 1 credit-note page without one.
- 23 pages mention "refund" in their Kvittotext; 22 of these are refund-policy boilerplate from Apple, Google and Dropbox receipts, only the Fireflies page is an actual refund.
- No page has a negative `amount_sek` (the sign convention is `kind`), and no Amex-lane page is a credit note.

### 6. Partial payments: 0 confirmed

- Amex: 14 groups of several charges on the same merchant, card and day (31 rows). 3 groups are business, all car sharing (Kinto): each row has its own archive page with its own receipt number, so they are separate rentals, not one invoice split in two. The 11 other groups are personal.
- Pleo: 6 same-merchant same-day pairs (12 rows). The pair with archive pages (OpenAI) carries two different invoice numbers; the others (Bolagsverket 2 x 1 000,00, DigitalOcean twice, Fireflies, Zettle) are separate purchases by amount pattern and have no pages. None is one invoice paid in two rows.
- Row larger or smaller than its receipt: `scripts/archive.py check` flags 2 pages where the charge amount is not printed in the receipt text (one FX charge in CRC, one Proffsmagasin order). Plus the Fireflies FX gap in 3. These are currency and rounding cases, not partial payments.
- 9 invoice numbers sit on two pages each. 1 pair is receipt plus its credit note (Fireflies). 8 pairs are consecutive-month subscription receipts (Anthropic 3, Brave, Fathom, Loom, Grok, Letaido/Firecrawl) where the same document is filed on two charges; those are wrong-period receipts, already parked in `archive/pending-replacements/` (12 pages), not partial payments.

### 7. Mixed or split VAT on a single receipt: 0 confirmed

- Kvittotext with more than one VAT rate: 1 page (Inet Askim, Amex lane, 8 990,00 SEK) prints "Moms 6 %", "Moms 12 %" and "Moms 25 %" as template rows; only the 25 % row carries an amount (1 798,00), the others are 0,00. Every other page with a rate in its text has exactly one: 25 % on 44 pages, 6 % on 8, 0 % on 9.
- Archive frontmatter: `vat_rate` 25 on 132 pages, 6 on 9, none on 41 (25 Amex-lane pages carry `vat_regime` instead, 16 Pleo pages have an empty or 0 % rate). `vat_sek` is present on 149 pages.
- Pleo `Tax Rate`: 25 % on 159 rows, 6 % on 12, 0 % on 5, empty on 5. The export has one rate per row, so a mixed receipt cannot be represented there; it would appear as one rate with a computed `Tax Amount`. The 6 % rows include a bakery and a grocery store next to taxi, gym and books, which suggests Pleo's rate is a guess rather than read from the receipt.
- Pleo `Net Amount` semantics: on 132 of 175 card rows (all foreign merchants: US 83, IE 24, NL 14, SG 7, CA 2, PT 2) `Net Amount` equals `Amount` and `Tax Amount` is 25 % on top, i.e. Pleo computes reverse-charge VAT. On 40 rows (35 Swedish) VAT is included in the amount. 5 rows have no tax fields.
- Amex lane: `vat_regime` on the 40 business rows is `domestic25_half` 14, `domestic25` 12, `noneu_rc` 4, `oss_pending` 3, `eu_rc` 2, empty 5. `domestic25_half` is the passenger-car rule in `scripts/fortnox_lane.py` (half of the 25 % VAT deductible); it splits one rate on the voucher, not on the receipt.

## What lacks a counterpart in the archive

- All 11 Amex refunds and their 11 refunded charges (10 personal, 1 business).
- 4 of the 6 Pleo same-day pairs (no pages, so no invoice numbers to confirm they are separate invoices).
- The Proffsmagasin credit-note page lacks a credit document.
- The 4 Amex payment rows and the 4 Pleo payout rows have no counterpart by design.

## Method

Stdlib Python over the three sources: `csv` for the ledgers (Unicode minus and sv-SE decimals normalised), frontmatter parsed by hand from the markdown pages, Kvittotext scanned with a regex for `25|12|6 %` and a `0 %` next to a VAT word. Refund counterparts matched on card, first eight letters of the merchant text and amount. Archive warnings taken from `scripts/archive.py check --root <archive>` (182 pages, 0 errors, 37 warnings).
