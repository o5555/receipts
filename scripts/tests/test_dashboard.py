#!/usr/bin/env python3
"""Offline checks for scripts/dashboard.py (collectors, CLI) and scripts/dashboard_html.py (renderer).

Every fixture is invented and written into a temp root that mirrors the repo layout
(data/pleo, data/amex, data/tags, out/, archive/). The only real inputs are
scripts/vendor_aliases.json and scripts/merchant_rules.json, read through archive.py
for vendor canonicalisation and portal hints; the expected vendor names, the portal hint
and the alias-dependent archive page id are derived from those files at run time
(archive.resolve_vendor, the first alias routed "portal"), never spelled out, so an
operational edit to the alias table does not break the suite. The assertions follow DASH-SPEC and the
model sample: the model shape and JSON round trip, Pleo month counts from the newest
export, payout rows kept apart from card purchases without receipt, same-file detection
that ignores an a/b pair, flagged statuses open / refiled / gone against two exports (all
open with the baseline alone, still open when a row merely lost its file), both missing-list
formats, route derivation (kivra, pocket, portal via aliases, gmail, route column),
needs-tag counts that honour data/tags/overrides.csv, the Amex stage per month, the
todo rules and their ordering, source freshness with a controlled --today, archive
warning kinds and status classes, the rendered page (no dashes, section ids, escaping,
Swedish amounts), and the CLI through subprocess (--json, --out, --archive, empty root).
The archive is built through archive.Archive.add with archive.EXTRACTOR stubbed, so the
Swift helper is never needed. The temp root is removed on success and kept on failure.
out/, data/ and the real archive root are never read or written. No network, no Gmail,
no Pleo, no Fortnox.

Run: python3 scripts/tests/test_dashboard.py   (exit 0 = every check passed)
"""
import csv
import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
REPO = os.path.dirname(SCRIPTS)
SCRIPT = os.path.join(SCRIPTS, "dashboard.py")
sys.path.insert(0, SCRIPTS)
try:
    import archive
except Exception as exc:  # the fixtures cannot be built without the archive library
    print(f"FAIL import scripts/archive.py: {exc!r}")
    sys.exit(1)

dashboard = None
dashboard_html = None

EM_DASH = "\u2014"
EN_DASH = "\u2013"
BAR_DASH = "\u2015"
NBSP = "\u00a0"
MINUS = "\u2212"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TODAY = "2026-09-12"
LATER = "2026-09-26"

MODEL_KEYS = ["generated", "today", "entity", "entity_name", "root", "sources", "summary", "todo",
              "pleo", "amex", "archive"]
SOURCE_KEYS = ["pleo_export", "pleo_missing", "worklist", "amex_csv", "amex_ledger", "fortnox_runs", "archive"]
SOURCE_FIELDS = ["label", "path", "date", "age_days", "detail", "state"]
SUMMARY_KEYS = ["pleo_rows", "pleo_with_receipt", "pleo_missing_export", "pleo_missing_live",
                "pleo_missing_live_sek", "pleo_missing_live_attached", "flagged_total", "flagged_open", "flagged_refiled",
                "amex_business_rows", "amex_business_sek", "amex_receipts_found", "amex_needs_tag_open",
                "amex_vouchers_planned", "amex_vouchers_booked", "archive_pages", "archive_sek",
                "archive_errors", "archive_warnings", "next_amex_month", "next_amex_csv_present"]
TODO_KEYS = ["key", "severity", "owner", "title", "detail", "count", "amount_sek", "command", "link"]
PLEO_KEYS = ["export_dir", "export_date", "months", "totals", "missing_live", "flagged", "same_file_groups_now"]
PLEO_MONTH_KEYS = ["month", "rows", "with_receipt", "missing", "amount_sek", "missing_sek", "payouts",
                   "payouts_sek", "exported", "queued", "not_exported"]
LIVE_ROW_KEYS = ["receipt_no", "expense_id", "date", "merchant", "vendor", "amount_sek", "type",
                 "export_status", "route", "route_label", "hint"]
BY_ROUTE_KEYS = ["route", "route_label", "count", "amount_sek", "owner"]
FLAG_GROUP_KEYS = ["key", "label", "count", "open", "refiled", "gone", "items"]
FLAG_ITEM_KEYS = ["receipt_no", "date", "merchant", "vendor", "amount_sek", "export_status", "expense_id",
                  "problem", "status", "status_label", "check_still_warns"]
AMEX_KEYS = ["ledger_file", "ledger_date", "coverage_end", "months", "needs_tag", "business_rows",
             "next_month", "next_month_csv_present"]
AMEX_MONTH_KEYS = ["month", "rows", "business", "business_sek", "business_other_entity", "personal",
                   "needs_tag", "needs_tag_open", "skip", "receipts_found", "receipts_missing", "plan",
                   "stage", "stage_label"]
PLAN_KEYS = ["dir", "built", "vouchers", "receipts", "uploaded", "booked", "total_sek", "warnings"]
NEEDS_TAG_KEYS = ["ref", "date", "merchant", "amount_sek", "card", "why", "answered"]
BUSINESS_ROW_KEYS = ["ref", "date", "month", "merchant", "vendor", "amount_sek", "card", "entity",
                     "bas_account", "vat_regime", "receipt", "receipt_file", "archive_id", "voucher",
                     "status_label"]
ARCHIVE_KEYS = ["root", "pages", "total_sek", "updated", "previews", "errors", "warnings_total",
                "warning_kinds", "warnings", "months", "status", "text_methods"]
ARCHIVE_MONTH_KEYS = ["month", "pages", "amount_sek", "pleo", "amex", "attached", "in_pleo", "warnings"]
STAGE_LABELS = {"no-csv": "väntar på Amex-CSV", "untagged": "otaggade rader kvar", "no-plan": "plan saknas",
                "planned": "plan klar, väntar på godkännande", "partial": "delvis bokförd", "booked": "bokförd",
                "nothing-to-book": "inget att bokföra"}
ROUTE_LABELS = {"kivra": "Kivra-appen", "portal": "leverantörsportal", "pocket": "eget utlägg", "gmail": "Gmail"}
KIVRA_HINT = "Kivra-appen, BankID, Oscar laddar ner"
GROUP_LABELS = {"same-file": "Samma fil på två utgifter", "previous-period": "Föregående periods kvitto",
                "other-company": "Ställt till annat bolag", "unclear": "Fel leverantör eller oklart underlag"}
STATUS_LABELS = {"open": "öppen", "refiled": "ny fil bifogad", "gone": "saknas i exporten"}
WARNING_LABELS = {"duplicate-invoice": "samma fakturanummer på två köp", "earlier-period": "kvitto från en tidigare period",
                  "later-period": "kvitto från en senare period", "other-company": "ställt till annat bolag",
                  "amount-missing": "beloppet finns inte i texten", "vendor-missing": "leverantören nämns inte i texten",
                  "orphan-file": "fil utan sida", "index-stale": "index behöver köras om", "other": "övrigt"}
SECTION_IDS = ["lage", "att-gora", "pleo", "amex", "arkiv", "underlag"]

PLEO_COLUMNS = ["Date", "Receipt", "Expense Type", "Amount", "Net Amount", "Total FX Fee", "Currency",
                "Orig. amount", "Orig. currency", "Source description", "Category", "Account number",
                "Owner", "Note", "Team", "Team code", "Receipt urls", "Employee Code", "Review Status",
                "Reviewer", "Review Note", "AI Review Status", "Tax Code", "Tax Rate", "Tax Amount",
                "CIF", "Document Number", "Attendees", "Merchant Country Code", "Reconciled Entries",
                "Expense ID", "Personal Expense", "Export Status", "Linked Invoice Numbers",
                "Kostnadsstalle - Tag", "Kostnadsstalle - Id"]
LEDGER_COLUMNS = ["date", "card", "account", "amount", "merchant", "tag", "why", "in_pleo", "confirm",
                  "dash_id", "pack_status", "ext", "ref", "period", "entity", "bas_account",
                  "vat_regime", "konto_name"]
ACTIVITY_COLUMNS = ["Datum", "Beskrivning", "Kortmedlem", "Konto #", "Belopp", "Utökade specifikationer",
                    "Visas på ditt kontoutdrag som", "Adress", "Ort", "Postnummer", "Land", "Referens"]
MANIFEST_COLUMNS = ["ref", "date", "merchant", "amount", "bas_account", "vat_regime", "receipt_file",
                    "voucher_payload", "status"]

EXP2 = "22222222-2222-2222-2222-222222222222"
EXP3 = "33333333-3333-3333-3333-333333333333"
EXP4 = "44444444-4444-4444-4444-444444444444"
EXP5 = "55555555-5555-5555-5555-555555555555"
EXP6 = "66666666-6666-6666-6666-666666666666"
EXP7 = "77777777-7777-7777-7777-777777777777"
EXP8 = "88888888-8888-8888-8888-888888888888"
EXP9 = "99999999-9999-9999-9999-999999999999"
EXPK = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
EXPN = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
EXPB = "cccccccc-cccc-cccc-cccc-cccccccccccc"
EXPT = "dddddddd-dddd-dddd-dddd-dddddddddddd"

BASELINE = "2026-08-21"
NEWER = "2026-09-10"
ESCAPE_MERCHANT = "<TEST> & CO"
ESCAPE_MERCHANT_AMEX = "ROR & FLAK <TEST> AB"
WINCHER_MERCHANT = "WINCHERINTERNATIONALCOM STOCKHOLM"
# (alias key, vendor dict, merchant text) of a real alias routed "portal", picked in main();
# None when the table has no such vendor, which skips the portal assertions
PORTAL = None
# archive page ids by fixture label, recorded when the archive fixture is built
PAGE_IDS = {}
FLAGGED = {"2600102", "2600103", "2600104", "2600105", "2600107"}

checks = []


def ok(name, cond, detail=""):
    checks.append((name, cond, detail))
    if not cond:
        print(f"FAIL {name} {detail}")


def call(name, fn, *args, **kw):
    """Run a call; an exception becomes a failed check instead of aborting the suite."""
    try:
        return fn(*args, **kw)
    except Exception as exc:
        ok(name, False, f"{type(exc).__name__}: {exc}")
        return None


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def near(a, b, tol=0.005):
    try:
        return abs(float(a) - float(b)) < tol
    except (TypeError, ValueError):
        return False


def portal_alias(aliases):
    """(key, vendor dict, merchant text) for the first alias routed "portal" with a hint whose
    merchant text is its own (no other alias lists it) and resolves back to it through
    archive.vendor_for; None when the table has no such vendor."""
    vendors = aliases.get("vendors") or {}
    for key, v in vendors.items():
        if v.get("route_if_none") != "portal" or v.get("merchant_exact") or not v.get("merchants") or not v.get("portal"):
            continue
        pat = re.sub(r"\s+", " ", str(v["merchants"][0])).strip().upper()
        if not pat or "KIVRA" in pat:
            continue
        shared = any(k2 != key and any(re.sub(r"\s+", " ", str(x)).strip().upper() == pat for x in v2.get("merchants") or [])
                     for k2, v2 in vendors.items())
        if shared:
            continue
        merchant = pat + " 1234"
        if archive.vendor_for(merchant)[0] == v.get("name"):
            return key, v, merchant
    return None


def content_snapshot(path):
    out = {}
    for base, _, files in os.walk(path):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, path)] = sha(p)
    return out


def set_mtime(path, y, m, d, hour=12):
    ts = time.mktime((y, m, d, hour, 0, 0, 0, 0, -1))
    os.utime(path, (ts, ts))


def write_text(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def write_csv(path, header, rows, bom=False, quote_all=False):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8-sig" if bom else "utf-8", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL if quote_all else csv.QUOTE_MINIMAL)
        w.writerow(header)
        w.writerows(rows)
    return path


def receipt_file(path, text):
    """A fake receipt whose text the stub extractor returns: a PDF-looking file (header
    line, then the text) or an html file when the suffix says so."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if path.lower().endswith((".html", ".htm")):
        data = ("<html><body>\n" + text + "\n</body></html>\n").encode("utf-8")
    else:
        data = b"%PDF-1.4\n" + text.encode("utf-8") + b"\n"
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def stub_extract(path, preview_path=None, max_pages=3, **kw):
    """Stands in for the Swift helper: the text is the fake file's own content, PDFs get a
    tiny preview file, html files report method html."""
    with open(path, "rb") as fh:
        text = fh.read().decode("utf-8", "replace")
    if text.startswith("%PDF"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
    method = "html" if path.lower().endswith((".html", ".htm")) else "pdfkit"
    if preview_path:
        with open(preview_path, "wb") as fh:
            fh.write(b"\x89PNG\r\n\x1a\n fake preview\n")
    return {"text": text, "method": method, "pages": 1, "preview": bool(preview_path), "error": None}


# ----------------------------------------------------------------------------- fixtures

def pleo_row(**kw):
    r = {c: "" for c in PLEO_COLUMNS}
    r.update(kw)
    return [r[c] for c in PLEO_COLUMNS]


def write_pleo_export(root, folder_date, newer=False, lost_file=False):
    """data/pleo/expenses_<date>: six card rows across July and August (a/b pair under one
    number, a byte-identical pair under two numbers, a Receipt-urls row without a file,
    two rows without any receipt, one of them a reimbursement payout). The newer variant
    refiles 2600103 with a different file and drops 2600104; the lost_file variant keeps
    every row but ships no file for 2600103."""
    d = os.path.join(root, "data", "pleo", "expenses_" + folder_date)
    rdir = os.path.join(d, "receipts")
    os.makedirs(rdir)
    rows = [
        pleo_row(**{"Date": "17-07-2026", "Receipt": "2600102", "Expense Type": "Card Purchase",
                    "Amount": MINUS + "981,09", "Currency": "SEK", "Orig. amount": "99,00", "Orig. currency": "USD",
                    "Source description": "TESTCLOUD INC", "Category": "Hosting", "Account number": "6540",
                    "Owner": "Test Person", "Expense ID": EXP2, "Export Status": "EXPORTED"}),
        pleo_row(**{"Date": "20-07-2026", "Receipt": "2600103", "Expense Type": "Card Purchase",
                    "Amount": MINUS + "45,00", "Currency": "SEK", "Orig. amount": "45,00", "Orig. currency": "SEK",
                    "Source description": "TESTDUBBELKVITTO AB", "Category": "Programvaror", "Account number": "5420",
                    "Owner": "Test Person", "Expense ID": EXP3, "Export Status": "QUEUED"}),
        pleo_row(**{"Date": "05-08-2026", "Receipt": "2600104", "Expense Type": "Card Purchase",
                    "Amount": MINUS + "45,00", "Currency": "SEK", "Orig. amount": "45,00", "Orig. currency": "SEK",
                    "Source description": "TESTDUBBELKVITTO AB", "Category": "Programvaror", "Account number": "5420",
                    "Owner": "Test Person", "Expense ID": EXP4, "Export Status": "QUEUED"}),
        pleo_row(**{"Date": "12-08-2026", "Receipt": "2600105", "Expense Type": "Card Purchase",
                    "Amount": MINUS + "129,00", "Currency": "SEK", "Orig. amount": "129,00", "Orig. currency": "SEK",
                    "Source description": "TESTURL AB", "Category": "Programvaror", "Account number": "5420",
                    "Owner": "Test Person", "Expense ID": EXP5, "Export Status": "NOT_EXPORTED",
                    "Receipt urls": "https://app.pleo.io:443/accounting-entries/aaaa/receipts/bbbb/file"}),
        pleo_row(**{"Date": "20-08-2026", "Receipt": "2600106", "Expense Type": "Card Purchase",
                    "Amount": MINUS + "123,75", "Currency": "SEK", "Orig. amount": "123,75", "Orig. currency": "SEK",
                    "Source description": "KIVRA+", "Category": "Administration", "Account number": "6570",
                    "Owner": "Test Person", "Expense ID": EXP6, "Export Status": "NOT_EXPORTED"}),
        pleo_row(**{"Date": "21-08-2026", "Receipt": "2600107", "Expense Type": "Reimbursement to Test Person",
                    "Amount": MINUS + "700,00", "Currency": "SEK", "Orig. amount": "700,00", "Orig. currency": "SEK",
                    "Owner": "Test Person", "Expense ID": EXP7, "Export Status": "NOT_EXPORTED"}),
    ]
    if newer:
        rows = [r for r in rows if r[1] != "2600104"]
    write_csv(os.path.join(d, "export_" + folder_date + ".csv"), PLEO_COLUMNS, rows, bom=True, quote_all=True)
    receipt_file(os.path.join(rdir, "2600102a.pdf"), "TESTCLOUD INC receipt ab pair")
    receipt_file(os.path.join(rdir, "2600102b.pdf"), "TESTCLOUD INC receipt ab pair")
    if not lost_file:
        receipt_file(os.path.join(rdir, "2600103.pdf"), "TESTDUBBELKVITTO refiled 2600103" if newer
                     else "TESTDUBBELKVITTO shared bytes")
    if not newer:
        receipt_file(os.path.join(rdir, "2600104.pdf"), "TESTDUBBELKVITTO shared bytes")
    return d


def write_worklist(root):
    """out/pleo-kvittofel-2026-09-03.txt: two groups, three items, baseline export 2026-08-21."""
    text = (
        "Kvittofel i Pleo (Viseo AB), hittade vid arkivbygget 2026-09-03\n"
        "Underlag: Pleo-exporten 2026-08-21 jamford mot kvittotexten i varje fil (scripts/archive.py check).\n"
        "Kvittonummer ar Pleos \"Receipt\"-nummer. EXPORTED = raden ar redan exporterad till Trimero.\n"
        "\n"
        "1. Samma fil pa tva utgifter (kvittot hor bara till den ena)\n"
        "- 2600103  2026-07-20  Testdubbelkvitto 45,00 SEK   samma fil som 2600104 (2026-08-05, 45,00 SEK). Julikvittot saknas.\n"
        "- 2600104  2026-08-05  Testdubbelkvitto 45,00 SEK   samma fil som 2600103 (2026-07-20, 45,00 SEK). Augustikvittot saknas.\n"
        "\n"
        "2. Foregaende periods kvitto bifogat (kvittodatum en manad fore kopet)\n"
        "- 2600102  2026-07-17  Testcloud 981,09 SEK         kvitto daterat 2026-06-15              (EXPORTED)\n"
        "\n"
        "3. Kvitto stallt till annat bolag\n"
        "- 2600105  2026-08-12  Testurl 129,00 SEK           stallt till 5555 Media AB\n"
        "\n"
        "4. Fel leverantor eller oklart underlag\n"
        "- 2600107  2026-08-21  Utbetalning 700,00 SEK       underlaget saknas helt\n"
    )
    return write_text(os.path.join(root, "out", "pleo-kvittofel-2026-09-03.txt"), text)


def write_missing_a(root):
    """out/pleo-missing-receipts-2026-08-31.csv (format a): two Kivra rows, a row for the
    portal alias picked from the real table (an unknown merchant when there is none), a
    Bazoom out-of-pocket row and an unknown merchant with < and & (gmail)."""
    portal_merchant = PORTAL[2] if PORTAL else "TESTPORTAL AB"
    header = ["pleo_receipt_no", "expense_id", "date", "merchant", "type", "amount", "orig_amount",
              "orig_currency", "export_status"]
    rows = [
        ["2600106", EXP6, "2026-08-20", "KIVRA+", "Card Purchase", "-123.75", "", "SEK", "NOT_EXPORTED"],
        ["aug31-kivra", EXPK, "2026-08-31", "KIVRA+", "Card Purchase", "-123.75", "", "SEK", "NOT_EXPORTED"],
        ["aug28-portal", EXPN, "2026-08-28", portal_merchant, "Card Purchase", "-210.00", "", "SEK", "NOT_EXPORTED"],
        ["apr01-bazoom", EXPB, "2026-04-01", "BAZOOM", "Out of Pocket", "-59426.85", "5000.00", "EUR", "NOT_EXPORTED"],
        ["aug15-test", EXPT, "2026-08-15", ESCAPE_MERCHANT, "Card Purchase", "-99.00", "", "SEK", "NOT_EXPORTED"],
        # the archive page P4 carries this expense id with pleo_status "attached 2026-08-31"
        ["aug12-testurl", EXP5, "2026-08-12", "TESTURL AB", "Card Purchase", "-129.00", "", "SEK", "NOT_EXPORTED"],
    ]
    return write_csv(os.path.join(root, "out", "pleo-missing-receipts-2026-08-31.csv"), header, rows)


def write_missing_b(root):
    """out/pleo-missing-receipts-2026-08-21.csv (format b, older): a Firecrawl row routed
    'forward or attach' and a fee row routed 'ask Nicolina'."""
    header = ["date", "pleo_receipt_no", "merchant", "amount_sek", "route", "evidence", "action"]
    rows = [
        ["2026-08-17", "2600500", "FIRECRAWL.DEV", "981,09", "forward or attach",
         "Gmail 1a00b4197fake001, Receipt-0001.pdf", "Attach via MCP."],
        ["2026-08-20", "2600501", "TESTFEE AB", "123,75", "ask Nicolina", "", "Ask how fees are evidenced."],
    ]
    return write_csv(os.path.join(root, "out", "pleo-missing-receipts-2026-08-21.csv"), header, rows)


LEDGER_ROWS = [
    # date, card, account, amount, merchant, tag, ref, entity, bas_account, vat_regime
    ["2026-04-18", "2004", "-62004", "75.00", "TESTKAFE", "personal", "ATFIX401", "viseo", "", ""],
    ["2026-05-15", "1022", "-61022", "1000.00", "TESTMAY AB", "business", "ATFIX501", "viseo", "6540", "domestic25"],
    ["2026-06-05", "1022", "-61022", "1599.00", "TESTSAAS LTD", "business", "ATFIX601", "viseo", "6540", "noneu_rc"],
    ["2026-06-12", "2004", "-62004", "59.00", "TESTKAFE", "personal", "ATFIX602", "viseo", "", ""],
    ["2026-06-20", "1022", "-61022", "250.00", "TESTVENDOR AB", "business", "ATFIX603", "5555media", "6540", "domestic25"],
    ["2026-06-15", "1022", "-61022", "3663.08", "TESTHYRBIL AB", "business", "ATFIX605", "viseo", "", ""],
    ["2026-06-28", "1022", "-61022", "80.00", "TESTKAFE", "needs-tag", "ATFIX604", "viseo", "", ""],
    ["2026-07-10", "1022", "-61022", "3570.54", "WINCHERINTERNATIONALCOM STOCKHOLM", "business", "ATFIX701", "viseo", "6540", "eu_rc"],
    ["2026-07-05", "1022", "-61022", "224.82", "TESTKJELL AB", "business", "ATFIX702", "viseo", "5410", "domestic25"],
    ["2026-07-15", "1022", "-61022", "189.00", ESCAPE_MERCHANT_AMEX, "needs-tag", "ATFIX703", "viseo", "", ""],
    ["2026-07-20", "3003", "-13003", "1000.00", "TESTSKIP", "skip", "ATFIX704", "viseo", "", ""],
    ["2026-08-02", "2004", "-62004", "45.00", "TESTKAFE", "personal", "ATFIX801", "viseo", "", ""],
]


def write_amex_ledger(root):
    """out/amex-2026-05-06_2026-08-02.classified2.csv, mtime 2026-08-24 (the ledger date)."""
    rows = []
    for date, card, account, amount, merchant, tag, ref, entity, bas, vat in LEDGER_ROWS:
        rows.append([date, card, account, amount, merchant, tag, "fixture", "", "", "", "", "", ref,
                     "activity.csv", entity, bas, vat, "IT-tjanster" if bas else ""])
    path = write_csv(os.path.join(root, "out", "amex-2026-05-06_2026-08-02.classified2.csv"), LEDGER_COLUMNS, rows)
    set_mtime(path, 2026, 8, 24)
    return path


def write_needs_tag(root):
    header = ["ref", "date", "merchant", "amount", "card", "why"]
    rows = [["ATFIX604", "2026-06-28", "TESTKAFE", "80,00 SEK", "1022", "card default conflict"],
            ["ATFIX703", "2026-07-15", ESCAPE_MERCHANT_AMEX, "189,00 SEK", "1022", "tagged both ways"]]
    return write_csv(os.path.join(root, "out", "amex-needs-tag-2026-08-24.csv"), header, rows)


def write_overrides(root, refs):
    rows = [[ref, "personal", "fixture answer"] for ref in refs]
    return write_csv(os.path.join(root, "data", "tags", "overrides.csv"), ["ref", "tag", "note"], rows)


def write_activity(root, max_date_mdy):
    """data/amex/activity.csv with a Datum column (MM/DD/YYYY); the last row carries max_date.
    The file date is irrelevant: only the purchases inside decide the coverage."""
    rows = []
    for i, mdy in enumerate(["05/06/2026", "06/12/2026", "07/20/2026", max_date_mdy]):
        rows.append([mdy, "FIXTURE " + str(i), "OSCAR", "-61022", "100,00", "", "FIXTURE", "", "", "", "SE",
                     "ATFIX9" + str(i)])
    return write_csv(os.path.join(root, "data", "amex", "activity.csv"), ACTIVITY_COLUMNS, rows)


def write_receipt_map(root):
    """out/receipts-amex/map-2026-07.csv: ATFIX701 points at an existing file, ATFIX702 at a
    file that does not exist."""
    rdir = os.path.join(root, "out", "receipts-amex")
    have = receipt_file(os.path.join(rdir, "ATFIX701_receipt.pdf"), "Wincher International AB Total EUR 320.00")
    rows = [["ATFIX701", have], ["ATFIX702", os.path.join(rdir, "ATFIX702_missing.pdf")]]
    write_csv(os.path.join(rdir, "map-2026-07.csv"), ["ref", "file"], rows)
    return have


def write_plan(root, month, rows, warnings, execution=None, uploads=None):
    """out/fortnox-run-<month>/: manifest.csv (rows: ref, date, merchant, amount, bas, vat,
    receipt_file, status; a status starting with 'excluded' has no voucher payload),
    plan.md with a Varningar section, optional execution.json and uploads.json. plan.md
    mtime is 2026-08-24 (the built date)."""
    d = os.path.join(root, "out", "fortnox-run-" + month)
    os.makedirs(os.path.join(d, "vouchers"), exist_ok=True)
    manifest = []
    for i, (ref, date, merchant, amount, bas, vat, receipt, status) in enumerate(rows, 1):
        payload = "" if status.startswith("excluded") else f"out/fortnox-run-{month}/vouchers/{i:03d}_{ref}.json"
        manifest.append([ref, date, merchant, amount, bas, vat, receipt, payload, status])
    write_csv(os.path.join(d, "manifest.csv"), MANIFEST_COLUMNS, manifest)
    plan = [f"# Fortnox-korning: Amex-utlagg {month}", "", "Detta ar en plan, inga API-anrop har gjorts.", "",
            "## Varningar", ""]
    plan += [f"- {w}" for w in warnings]
    plan += ["", "## Rader", "", "| Nr | Datum | Handlare | Belopp |", "|---|---|---|---|"]
    plan += [f"| {i} | {r[1]} | {r[2]} | {r[3]} SEK |" for i, r in enumerate(rows, 1)]
    ppath = write_text(os.path.join(d, "plan.md"), "\n".join(plan) + "\n")
    set_mtime(ppath, 2026, 8, 24)
    if execution is not None:
        with open(os.path.join(d, "execution.json"), "w", encoding="utf-8") as fh:
            json.dump(execution, fh, indent=1)
    if uploads is not None:
        with open(os.path.join(d, "uploads.json"), "w", encoding="utf-8") as fh:
            json.dump({"_comment": "fixture", "uploads": uploads}, fh, indent=1)
    return d


def write_fortnox_plans(root, receipt_701, june_plan=False):
    """May: one voucher, booked. July: two vouchers plus an excluded row, one booked, one
    upload, two warnings. June (optional): one voucher plus a row excluded for a missing
    BAS account, nothing executed."""
    write_plan(root, "2026-05",
               [("ATFIX501", "2026-05-15", "TESTMAY AB", "1 000,00", "6540", "domestic25", "", "kvitto saknas")],
               [],
               execution={"batch_code": "fixture-may", "salary_done": True,
                          "rows": {"ATFIX501": {"voucher_number": 11, "voucher_series": "A", "voucher_year": 2026}},
                          "log": ["2026-08-30 09:00:00 verifikat skapat: ATFIX501 -> A11 (TESTMAY AB, 1 000,00 SEK)",
                                  "2026-08-30 09:00:01 lonetransaktion bokford: utlagg 1 000,00 SEK"]})
    july = write_plan(root, "2026-07",
                      [("ATFIX701", "2026-07-10", "WINCHERINTERNATIONALCOM STOCKHOLM", "3 570,54", "6540", "eu_rc",
                        receipt_701, "ok"),
                       ("ATFIX702", "2026-07-05", "TESTKJELL AB", "224,82", "5410", "domestic25", "", "kvitto saknas"),
                       ("ATFIX703", "2026-07-15", ESCAPE_MERCHANT_AMEX, "189,00", "", "", "", "excluded needs-tag")],
                      ["Kvitto saknas: ATFIX702 2026-07-05 TESTKJELL AB (224,82 SEK), verifikatet planeras anda",
                       "Exkluderad (tag needs-tag): ATFIX703 2026-07-15 " + ESCAPE_MERCHANT_AMEX + " (189,00 SEK)"],
                      execution={"batch_code": "fixture-july", "salary_done": False,
                                 "rows": {"ATFIX701": {"file_id": "f-701", "voucher_number": 12, "voucher_series": "A",
                                                       "voucher_year": 2026, "connected": True},
                                          "ATFIX702": {}},
                                 "log": ["2026-09-05 10:00:00 kvitto uppladdat: ATFIX701 File.Id f-701",
                                         "2026-09-05 10:00:01 verifikat skapat: ATFIX701 -> A12 (WINCHERINTERNATIONALCOM, 3 570,54 SEK)",
                                         "2026-09-05 10:00:02 kvitto kopplat: ATFIX701 -> verifikat A12"]},
                      uploads=[{"ref": "ATFIX701", "merchant": "WINCHERINTERNATIONALCOM STOCKHOLM", "file": receipt_701,
                                "inbox_file_id": "f-701", "archive_file_id": "a-701", "uploaded_at": "2026-09-05",
                                "voucher_payload": "out/fortnox-run-2026-07/vouchers/001_ATFIX701.json"}])
    if june_plan:
        write_plan(root, "2026-06",
                   [("ATFIX601", "2026-06-05", "TESTSAAS LTD", "1 599,00", "6540", "noneu_rc", "", "kvitto saknas"),
                    ("ATFIX605", "2026-06-15", "TESTHYRBIL AB", "3 663,08", "", "", "", "excluded bas_account saknas")],
                   ["Kvitto saknas: ATFIX601 2026-06-05 TESTSAAS LTD (1 599,00 SEK), verifikatet planeras anda"])
    return july


ARCHIVE_PAGES = [
    # (label, file name, text, meta) -> one page each; the texts decide the check warnings
    ("P1 earlier period", "p1.pdf", "Receipt from TESTCLOUD INC\nTotal 981,09 SEK",
     {"date": "2026-07-17", "merchant": "TESTCLOUD INC", "amount_sek": 981.09, "card": "pleo", "lane": "pleo",
      "pleo_expense_id": EXP2, "pleo_receipt_number": "2600102", "pleo_status": "i Pleo (EXPORTED)",
      "receipt_date": "2026-06-15", "source_kind": "pleo-export"}),
    ("P2 duplicate invoice", "p2.pdf", "TESTDUBBELKVITTO AB\nTotal 45,00 SEK",
     {"date": "2026-07-20", "merchant": "TESTDUBBELKVITTO AB", "amount_sek": 45.0, "card": "pleo", "lane": "pleo",
      "pleo_expense_id": EXP3, "pleo_receipt_number": "2600103", "pleo_status": "attached 2026-08-24 (QUEUED)",
      "invoice_number": "INV-7", "source_kind": "gmail", "source_ref": "1a00b4197fake103"}),
    ("P3 clean html", "p3.html", "TESTDUBBELKVITTO AB Total 45,00 SEK",
     {"date": "2026-08-05", "merchant": "TESTDUBBELKVITTO AB", "amount_sek": 45.0, "card": "pleo", "lane": "pleo",
      "pleo_expense_id": EXP4, "pleo_receipt_number": "2600104", "pleo_status": "i Pleo (QUEUED)",
      "source_kind": "pleo-export"}),
    ("P4 other company", "p4.pdf", "Receipt from TESTURL AB\nBill to 5555 Media AB\nTotal 129,00 SEK",
     {"date": "2026-08-12", "merchant": "TESTURL AB", "amount_sek": 129.0, "card": "pleo", "lane": "pleo",
      "pleo_expense_id": EXP5, "pleo_receipt_number": "2600105", "pleo_status": "attached 2026-08-31",
      "source_kind": "gmail", "source_ref": "1a00b4197fake105"}),
    ("P5 amex wincher", "p5.pdf", "Wincher International AB\nTotal EUR 320.00",
     {"date": "2026-07-10", "merchant": "WINCHERINTERNATIONALCOM STOCKHOLM", "amount_sek": 3570.54, "currency": "EUR",
      "amount": 320.0, "card": "amex-1022", "lane": "amex-fortnox", "amex_ref": "ATFIX701", "bas_account": "6540",
      "vat_regime": "eu_rc", "source_kind": "gmail", "source_ref": "1a00b4197fake701"}),
    ("P6 amex testsaas", "p6.pdf", "TESTSAAS LTD\nTotal 1599.00 SEK (1 599,00 SEK)",
     {"date": "2026-06-05", "merchant": "TESTSAAS LTD", "amount_sek": 1599.0, "card": "amex-1022",
      "lane": "amex-fortnox", "amex_ref": "ATFIX601", "bas_account": "6540", "vat_regime": "noneu_rc",
      "source_kind": "gmail", "source_ref": "1a00b4197fake601"}),
    ("P7 duplicate invoice, not flagged", "p7.pdf", "TESTDUBBELKVITTO AB\nKvitto juni\nTotal 45,00 SEK",
     {"date": "2026-06-30", "merchant": "TESTDUBBELKVITTO AB", "amount_sek": 45.0, "card": "pleo", "lane": "pleo",
      "pleo_expense_id": EXP8, "pleo_receipt_number": "2600201", "pleo_status": "i Pleo (EXPORTED)",
      "invoice_number": "INV-7", "source_kind": "pleo-export"}),
    ("P8 vendor missing, not flagged", "p8.pdf", "Receipt\nTotal 250,00 SEK",
     {"date": "2026-06-10", "merchant": "TESTPOST AB", "amount_sek": 250.0, "card": "pleo", "lane": "pleo",
      "pleo_expense_id": EXP9, "pleo_receipt_number": "2600202", "pleo_status": "i Pleo (EXPORTED)",
      "source_kind": "pleo-export"}),
]
P1 = "2026-07-17-testcloud-981-09"
P2 = "2026-07-20-testdubbelkvitto-45-00"
P3 = "2026-08-05-testdubbelkvitto-45-00"
P4 = "2026-08-12-testurl-129-00"
P6 = "2026-06-05-testsaas-1599-00"
P7 = "2026-06-30-testdubbelkvitto-45-00"
P8 = "2026-06-10-testpost-250-00"
ARCHIVE_PAGES_TOTAL = 8
ARCHIVE_SEK = 981.09 + 45.0 + 45.0 + 129.0 + 3570.54 + 1599.0 + 45.0 + 250.0
EXPECTED_WARNINGS = {"duplicate-invoice": 2, "earlier-period": 1, "other-company": 1, "vendor-missing": 1}


def build_archive(root, july_plan, src_dir):
    """Eight pages through the library with the stub extractor, the July verifikat synced
    onto the Wincher page, then index.md. Returns the Archive."""
    old = archive.EXTRACTOR
    archive.EXTRACTOR = stub_extract
    try:
        arc = archive.Archive(os.path.join(root, "archive"))
        for label, name, text, meta in ARCHIVE_PAGES:
            src = receipt_file(os.path.join(src_dir, name), text)
            m = dict(meta, entity="viseo")
            rec = call("archive fixture add " + label, arc.add, [src], m)
            if rec is not None:
                PAGE_IDS[label.split()[0]] = rec.get("id")
            if rec is not None and label.startswith("P1"):
                ok("archive fixture P1 id", rec.get("id") == P1, rec.get("id"))
        call("archive fixture sync-fortnox", archive.sync_fortnox, arc, july_plan)
        call("archive fixture index", arc.write_index)
        return arc
    finally:
        archive.EXTRACTOR = old


def make_root(work, name, second_export=False, missing="a", overrides=("ATFIX604",), amex_max="08/02/2026",
              june_plan=False, with_archive=True, with_worklist=True, lost_file=False):
    """A repo-shaped root under work/<name> with the fixture set selected by the options."""
    root = os.path.join(work, name)
    os.makedirs(os.path.join(root, "out"))
    write_pleo_export(root, BASELINE)
    if second_export:
        write_pleo_export(root, NEWER, newer=not lost_file, lost_file=lost_file)
    if with_worklist:
        write_worklist(root)
    if missing in ("a", "both"):
        write_missing_a(root)
    if missing in ("b", "both"):
        write_missing_b(root)
    write_amex_ledger(root)
    write_needs_tag(root)
    if overrides is not None:
        write_overrides(root, overrides)
    write_activity(root, amex_max)
    receipt_701 = write_receipt_map(root)
    july = write_fortnox_plans(root, receipt_701, june_plan=june_plan)
    if with_archive:
        build_archive(root, july, os.path.join(work, name + "-src"))
    return root


# ----------------------------------------------------------------------------- model access

def build(root, today, archive_root=None):
    """build_model with today as a date; falls back to the ISO string when the builder
    wants text. Exceptions become a failed check."""
    if archive_root is None:
        archive_root = os.path.join(root, "archive")
    day = datetime.date.fromisoformat(today)
    try:
        return dashboard.build_model(root, day, archive_root)
    except (TypeError, AttributeError):
        pass
    except Exception as exc:
        ok(f"build_model {os.path.basename(root)} raises", False, f"{type(exc).__name__}: {exc}")
        return None
    try:
        return dashboard.build_model(root, today, archive_root)
    except Exception as exc:
        ok(f"build_model {os.path.basename(root)} raises", False, f"{type(exc).__name__}: {exc}")
        return None


def month(m, section, key):
    for row in (m.get(section) or {}).get("months") or []:
        if row.get("month") == key:
            return row
    return None


def todo_by_key(m, key):
    return [t for t in m.get("todo") or [] if t.get("key") == key]


def todo_starting(m, prefix):
    return [t for t in m.get("todo") or [] if str(t.get("key") or "").startswith(prefix)]


def flagged_items(m):
    out = {}
    for g in ((m.get("pleo") or {}).get("flagged") or {}).get("groups") or []:
        for it in g.get("items") or []:
            out[it.get("receipt_no")] = it
    return out


def flagged_group(m, key):
    for g in ((m.get("pleo") or {}).get("flagged") or {}).get("groups") or []:
        if g.get("key") == key:
            return g
    return None


def live_rows(m):
    return {r.get("receipt_no"): r for r in ((m.get("pleo") or {}).get("missing_live") or {}).get("rows") or []}


def by_route(m):
    return {r.get("route"): r for r in ((m.get("pleo") or {}).get("missing_live") or {}).get("by_route") or []}


def business_rows(m):
    return {r.get("ref"): r for r in (m.get("amex") or {}).get("business_rows") or []}


def needs_tag_rows(m):
    return {r.get("ref"): r for r in (m.get("amex") or {}).get("needs_tag") or []}


def warning_kinds(m):
    return {k.get("key"): k for k in (m.get("archive") or {}).get("warning_kinds") or []}


def has_keys(d, keys):
    return isinstance(d, dict) and all(k in d for k in keys)


def no_dashes(s):
    return EM_DASH not in s and EN_DASH not in s and BAR_DASH not in s


def sev_rank(s):
    return {"crit": 0, "warn": 1, "info": 2}.get(s, 9)


# ----------------------------------------------------------------------------- check groups

def check_shape(m, label):
    """Every key of the contract present, JSON round trip, scalar formats."""
    ok(f"{label} model is a dict", isinstance(m, dict))
    ok(f"{label} model keys", has_keys(m, MODEL_KEYS), sorted(m) if isinstance(m, dict) else m)
    dumped = call(f"{label} json.dumps raises", json.dumps, m, ensure_ascii=False)
    ok(f"{label} model is JSON-serialisable", dumped is not None and json.loads(dumped) == m)
    ok(f"{label} generated is ISO UTC", TS_RE.match(str(m.get("generated"))) is not None, m.get("generated"))
    ok(f"{label} today is ISO", DATE_RE.match(str(m.get("today"))) is not None, m.get("today"))
    ok(f"{label} entity", m.get("entity") == "viseo" and m.get("entity_name") == "Viseo AB",
       (m.get("entity"), m.get("entity_name")))
    ok(f"{label} root is absolute", isinstance(m.get("root"), str) and os.path.isabs(m["root"]), m.get("root"))
    ok(f"{label} sources keys", has_keys(m.get("sources"), SOURCE_KEYS), sorted(m.get("sources") or {}))
    for k in SOURCE_KEYS:
        s = (m.get("sources") or {}).get(k)
        ok(f"{label} source {k} fields", has_keys(s, SOURCE_FIELDS) and s.get("state") in ("ok", "stale", "missing")
           and isinstance(s.get("label"), str) and isinstance(s.get("detail"), str)
           and (isinstance(s.get("path"), str) or (s.get("path") is None and s.get("state") == "missing")), s)
    ok(f"{label} summary keys", has_keys(m.get("summary"), SUMMARY_KEYS), sorted(m.get("summary") or {}))
    ok(f"{label} todo is a list of full entries", isinstance(m.get("todo"), list)
       and all(has_keys(t, TODO_KEYS) for t in m["todo"]), m.get("todo"))
    for t in m.get("todo") or []:
        ok(f"{label} todo {t.get('key')} vocab", t.get("severity") in ("crit", "warn", "info")
           and t.get("owner") in ("Oscar", "systemet", "Nicolina") and isinstance(t.get("title"), str)
           and t["title"] and isinstance(t.get("detail"), str) and (t.get("count") is None or isinstance(t["count"], int))
           and (t.get("amount_sek") is None or isinstance(t["amount_sek"], (int, float)))
           and (t.get("command") is None or isinstance(t["command"], str)) and isinstance(t.get("link"), str), t)
    ok(f"{label} pleo keys", has_keys(m.get("pleo"), PLEO_KEYS), sorted(m.get("pleo") or {}))
    pleo = m.get("pleo") or {}
    ok(f"{label} pleo months rows", all(has_keys(r, PLEO_MONTH_KEYS) for r in pleo.get("months") or []), pleo.get("months"))
    ok(f"{label} pleo totals keys", has_keys(pleo.get("totals"), ["rows", "with_receipt", "missing", "amount_sek", "missing_sek"]),
       pleo.get("totals"))
    ok(f"{label} pleo missing_live keys", has_keys(pleo.get("missing_live"), ["file", "date", "rows", "by_route"]),
       pleo.get("missing_live"))
    ok(f"{label} pleo live rows keys", all(has_keys(r, LIVE_ROW_KEYS) for r in (pleo.get("missing_live") or {}).get("rows") or []))
    ok(f"{label} pleo by_route keys", all(has_keys(r, BY_ROUTE_KEYS) for r in (pleo.get("missing_live") or {}).get("by_route") or []))
    ok(f"{label} pleo flagged keys", has_keys(pleo.get("flagged"), ["file", "date", "baseline_export", "compared_export", "groups"]),
       pleo.get("flagged"))
    groups = (pleo.get("flagged") or {}).get("groups") or []
    ok(f"{label} pleo flagged groups keys", all(has_keys(g, FLAG_GROUP_KEYS) for g in groups))
    ok(f"{label} pleo flagged item keys", all(has_keys(i, FLAG_ITEM_KEYS) for g in groups for i in g.get("items") or []))
    ok(f"{label} pleo same_file_groups_now is a list of lists", isinstance(pleo.get("same_file_groups_now"), list)
       and all(isinstance(g, list) for g in pleo["same_file_groups_now"]), pleo.get("same_file_groups_now"))
    ok(f"{label} amex keys", has_keys(m.get("amex"), AMEX_KEYS), sorted(m.get("amex") or {}))
    amex = m.get("amex") or {}
    ok(f"{label} amex months keys", all(has_keys(r, AMEX_MONTH_KEYS) for r in amex.get("months") or []), amex.get("months"))
    ok(f"{label} amex plan keys", all(r.get("plan") is None or has_keys(r["plan"], PLAN_KEYS) for r in amex.get("months") or []))
    ok(f"{label} amex stage vocab", all(r.get("stage") in STAGE_LABELS and r.get("stage_label") == STAGE_LABELS[r["stage"]]
                                       for r in amex.get("months") or []), [(r.get("stage"), r.get("stage_label")) for r in amex.get("months") or []])
    ok(f"{label} amex needs_tag keys", all(has_keys(r, NEEDS_TAG_KEYS) for r in amex.get("needs_tag") or []))
    ok(f"{label} amex business_rows keys", all(has_keys(r, BUSINESS_ROW_KEYS) for r in amex.get("business_rows") or []))
    ok(f"{label} archive keys", has_keys(m.get("archive"), ARCHIVE_KEYS), sorted(m.get("archive") or {}))
    arc = m.get("archive") or {}
    ok(f"{label} archive months keys", all(has_keys(r, ARCHIVE_MONTH_KEYS) for r in arc.get("months") or []))
    ok(f"{label} archive status keys", has_keys(arc.get("status"), ["attached", "in_pleo", "no_status", "fortnox_booked", "fortnox_open"]),
       arc.get("status"))
    ok(f"{label} archive warning entries", all(has_keys(w, ["page_id", "month", "kind", "text"]) for w in arc.get("warnings") or [])
       and all(has_keys(k, ["key", "label", "count"]) for k in arc.get("warning_kinds") or []))
    ok(f"{label} archive errors are strings", isinstance(arc.get("errors"), list) and all(isinstance(e, str) for e in arc["errors"]))
    ok(f"{label} model text has no dashes", no_dashes(dumped or ""))


def check_pleo_export_counts(m, label, newest):
    """Month counts, totals, payouts and same-file detection for the newest export (baseline or newer)."""
    pleo = m["pleo"]
    ok(f"{label} export_dir", pleo.get("export_dir") == "data/pleo/expenses_" + newest, pleo.get("export_dir"))
    ok(f"{label} export_date", pleo.get("export_date") == newest, pleo.get("export_date"))
    ok(f"{label} months newest first", [r["month"] for r in pleo["months"]] == ["2026-08", "2026-07"],
       [r["month"] for r in pleo["months"]])
    jul = month(m, "pleo", "2026-07")
    ok(f"{label} july rows", jul is not None and jul["rows"] == 2 and jul["with_receipt"] == 2 and jul["missing"] == 0
       and jul["payouts"] == 0, jul)
    ok(f"{label} july amounts", jul is not None and near(jul["amount_sek"], 1026.09) and near(jul["missing_sek"], 0)
       and near(jul["payouts_sek"], 0), jul)
    ok(f"{label} july export status", jul is not None and jul["exported"] == 1 and jul["queued"] == 1 and jul["not_exported"] == 0, jul)
    aug = month(m, "pleo", "2026-08")
    t = pleo["totals"]
    s = m["summary"]
    if newest == BASELINE:
        ok(f"{label} august rows", aug is not None and aug["rows"] == 4 and aug["with_receipt"] == 2 and aug["missing"] == 1
           and aug["payouts"] == 1, aug)
        ok(f"{label} august amounts", aug is not None and near(aug["amount_sek"], 997.75) and near(aug["missing_sek"], 123.75)
           and near(aug["payouts_sek"], 700.0), aug)
        ok(f"{label} august export status", aug is not None and aug["exported"] == 0 and aug["queued"] == 1 and aug["not_exported"] == 3, aug)
        ok(f"{label} totals", t["rows"] == 6 and t["with_receipt"] == 4 and t["missing"] == 1 and t["payouts"] == 1
           and near(t["amount_sek"], 2023.84) and near(t["missing_sek"], 123.75) and near(t["payouts_sek"], 700.0), t)
        groups = [sorted(g) for g in pleo["same_file_groups_now"]]
        ok(f"{label} same-file group is the two-number pair", groups == [["2600103", "2600104"]], groups)
        ok(f"{label} same-file ignores the a/b pair", all("2600102" not in g for g in groups), groups)
        ok(f"{label} summary pleo counts", s["pleo_rows"] == 6 and s["pleo_with_receipt"] == 4 and s["pleo_missing_export"] == 2,
           (s["pleo_rows"], s["pleo_with_receipt"], s["pleo_missing_export"]))
    else:
        ok(f"{label} august rows", aug is not None and aug["rows"] == 3 and aug["with_receipt"] == 1 and aug["missing"] == 1
           and aug["payouts"] == 1, aug)
        ok(f"{label} august amounts", aug is not None and near(aug["amount_sek"], 952.75) and near(aug["missing_sek"], 123.75)
           and near(aug["payouts_sek"], 700.0), aug)
        ok(f"{label} august export status", aug is not None and aug["exported"] == 0 and aug["queued"] == 0 and aug["not_exported"] == 3, aug)
        ok(f"{label} totals", t["rows"] == 5 and t["with_receipt"] == 3 and t["missing"] == 1 and t["payouts"] == 1
           and near(t["amount_sek"], 1978.84) and near(t["missing_sek"], 123.75) and near(t["payouts_sek"], 700.0), t)
        ok(f"{label} no same-file group after the refile", pleo["same_file_groups_now"] == [], pleo["same_file_groups_now"])
        ok(f"{label} summary pleo counts", s["pleo_rows"] == 5 and s["pleo_with_receipt"] == 3 and s["pleo_missing_export"] == 2,
           (s["pleo_rows"], s["pleo_with_receipt"], s["pleo_missing_export"]))
    ok(f"{label} rows split into receipt, missing and payout", all(r["rows"] == r["with_receipt"] + r["missing"] + r["payouts"]
                                                                    for r in pleo["months"]), pleo["months"])


def check_flagged(m, label, compared):
    """The worklist parse, enrichment from the export and the status per item."""
    fl = m["pleo"]["flagged"]
    ok(f"{label} flagged file", fl.get("file") == "out/pleo-kvittofel-2026-09-03.txt" and fl.get("date") == "2026-09-03",
       (fl.get("file"), fl.get("date")))
    ok(f"{label} flagged baseline", fl.get("baseline_export") == "data/pleo/expenses_" + BASELINE, fl.get("baseline_export"))
    ok(f"{label} flagged compared export", fl.get("compared_export") == compared, fl.get("compared_export"))
    keys = [g["key"] for g in fl["groups"]]
    ok(f"{label} flagged group keys in heading order", keys == ["same-file", "previous-period", "other-company", "unclear"], keys)
    ok(f"{label} flagged group labels", all(g["label"] == GROUP_LABELS.get(g["key"]) for g in fl["groups"]),
       [(g["key"], g["label"]) for g in fl["groups"]])
    g1, g2 = flagged_group(m, "same-file"), flagged_group(m, "previous-period")
    g3, g4 = flagged_group(m, "other-company"), flagged_group(m, "unclear")
    ok(f"{label} flagged group counts", g1 is not None and g2 is not None and g3 is not None and g4 is not None
       and g1["count"] == 2 and g2["count"] == 1 and g3["count"] == 1 and g4["count"] == 1,
       [(g["key"], g["count"]) for g in fl["groups"]])
    items = flagged_items(m)
    ok(f"{label} flagged item numbers", set(items) == FLAGGED, sorted(items))
    i102, i103, i104 = items.get("2600102"), items.get("2600103"), items.get("2600104")
    i105, i107 = items.get("2600105"), items.get("2600107")
    ok(f"{label} item without a file enriched", i107 is not None and near(i107["amount_sek"], 700.0) and i107["expense_id"] == EXP7
       and i107["export_status"] == "NOT_EXPORTED" and i107["problem"] == "underlaget saknas helt", i107)
    ok(f"{label} other-company item", i105 is not None and i105["merchant"] == "TESTURL AB" and near(i105["amount_sek"], 129.0)
       and i105["problem"] == "stallt till 5555 Media AB" and i105["check_still_warns"] is True, i105)
    ok(f"{label} no page means no archive warning", i107 is not None and i107["check_still_warns"] is False, i107 and i107["check_still_warns"])
    ok(f"{label} problem strips the export status token", i102 is not None and i102["problem"] == "kvitto daterat 2026-06-15",
       i102 and i102["problem"])
    ok(f"{label} problem keeps the text after the vendor chunk", i103 is not None
       and i103["problem"] == "samma fil som 2600104 (2026-08-05, 45,00 SEK). Julikvittot saknas.", i103 and i103["problem"])
    ok(f"{label} item date from the txt", i102 is not None and i102["date"] == "2026-07-17" and i103 is not None
       and i103["date"] == "2026-07-20", (i102 and i102["date"], i103 and i103["date"]))
    ok(f"{label} item enriched from the export", i102 is not None and i102["merchant"] == "TESTCLOUD INC"
       and near(i102["amount_sek"], 981.09) and i102["export_status"] == "EXPORTED" and i102["expense_id"] == EXP2, i102)
    ok(f"{label} item 2600103 enriched", i103 is not None and i103["merchant"] == "TESTDUBBELKVITTO AB"
       and near(i103["amount_sek"], 45.0) and i103["export_status"] == "QUEUED" and i103["expense_id"] == EXP3, i103)
    ok(f"{label} status labels match the status", all(it["status_label"] == STATUS_LABELS.get(it["status"]) for it in items.values()),
       [(it["status"], it["status_label"]) for it in items.values()])
    ok(f"{label} check_still_warns is a bool", all(isinstance(it["check_still_warns"], bool) for it in items.values()))
    ok(f"{label} check_still_warns true for the pages the archive warns about", i102 is not None and i103 is not None
       and i102["check_still_warns"] is True and i103["check_still_warns"] is True,
       (i102 and i102["check_still_warns"], i103 and i103["check_still_warns"]))
    ok(f"{label} check_still_warns false for the clean page", i104 is not None and i104["check_still_warns"] is False,
       i104 and i104["check_still_warns"])
    s = m["summary"]
    if compared is None:
        ok(f"{label} all open with the baseline alone", all(it["status"] == "open" for it in items.values()),
           [(k, it["status"]) for k, it in items.items()])
        ok(f"{label} group open counts", g1 is not None and g1["open"] == 2 and g1["refiled"] == 0 and g1["gone"] == 0
           and g2 is not None and g2["open"] == 1 and g3 is not None and g3["open"] == 1 and g4 is not None and g4["open"] == 1,
           [(g["key"], g["open"], g["refiled"], g["gone"]) for g in fl["groups"]])
        ok(f"{label} summary flagged", s["flagged_total"] == 5 and s["flagged_open"] == 5 and s["flagged_refiled"] == 0,
           (s["flagged_total"], s["flagged_open"], s["flagged_refiled"]))
    else:
        ok(f"{label} refiled when the file changed", i103 is not None and i103["status"] == "refiled", i103 and i103["status"])
        ok(f"{label} gone when the number left the export", i104 is not None and i104["status"] == "gone", i104 and i104["status"])
        ok(f"{label} open when the file is the same", i102 is not None and i102["status"] == "open", i102 and i102["status"])
        ok(f"{label} open when neither export has a file", i107 is not None and i107["status"] == "open" and i105 is not None
           and i105["status"] == "open", (i105 and i105["status"], i107 and i107["status"]))
        ok(f"{label} group counts after the refile", g1 is not None and g1["open"] == 0 and g1["refiled"] == 1 and g1["gone"] == 1
           and g2 is not None and g2["open"] == 1 and g2["refiled"] == 0 and g2["gone"] == 0,
           [(g["key"], g["open"], g["refiled"], g["gone"]) for g in fl["groups"]])
        ok(f"{label} summary flagged", s["flagged_total"] == 5 and s["flagged_open"] == 3 and s["flagged_refiled"] == 1,
           (s["flagged_total"], s["flagged_open"], s["flagged_refiled"]))


def check_lostfile(m, label):
    """A newer export in which a flagged row kept its line but lost its file: still open."""
    fl = m["pleo"]["flagged"]
    ok(f"{label} compared against the newer export", fl.get("compared_export") == "data/pleo/expenses_" + NEWER, fl.get("compared_export"))
    items = flagged_items(m)
    i103, i104 = items.get("2600103"), items.get("2600104")
    ok(f"{label} row that lost its file stays open", i103 is not None and i103["status"] == "open", i103 and i103["status"])
    ok(f"{label} row with the same file stays open", i104 is not None and i104["status"] == "open", i104 and i104["status"])
    ok(f"{label} nothing refiled or gone", all(it["status"] == "open" for it in items.values()),
       [(k, it["status"]) for k, it in items.items()])
    s = m["summary"]
    ok(f"{label} summary flagged", s["flagged_total"] == 5 and s["flagged_open"] == 5 and s["flagged_refiled"] == 0,
       (s["flagged_total"], s["flagged_open"], s["flagged_refiled"]))
    jul = month(m, "pleo", "2026-07")
    ok(f"{label} the lost file counts as missing", jul is not None and jul["with_receipt"] == 1 and jul["missing"] == 1, jul)
    r = todo_by_key(m, "pleo-reattach")
    ok(f"{label} pleo-reattach keeps every item", len(r) == 1 and r[0]["count"] == 5, r)


def check_missing_a(m, label):
    """Format a: the newest list wins, routes derived, hints and vendor names from the alias
    table through archive.py."""
    live = m["pleo"]["missing_live"]
    ok(f"{label} newest missing list picked", live.get("file") == "out/pleo-missing-receipts-2026-08-31.csv"
       and live.get("date") == "2026-08-31", (live.get("file"), live.get("date")))
    rows = live_rows(m)
    ok(f"{label} six live rows", len(rows) == 6, sorted(rows))
    a = rows.get("aug12-testurl")
    ok(f"{label} archive-attached row keeps its place under route attached", a is not None and a["route"] == "attached"
       and a["route_label"] == "bifogat sedan listan gjordes" and a["expense_id"] == EXP5, a)
    ok(f"{label} archive-attached hint names the archive status", a is not None and "attached 2026-08-31" in a["hint"]
       and "enligt arkivet" in a["hint"], a and a["hint"])
    k1 = rows.get("2600106")
    ok(f"{label} kivra route", k1 is not None and k1["route"] == "kivra" and k1["route_label"] == ROUTE_LABELS["kivra"]
       and k1["hint"] == KIVRA_HINT, k1)
    kivra_vendor = archive.resolve_vendor({"merchant": "KIVRA+", "amount": 123.75, "currency": "SEK"})[0]
    ok(f"{label} kivra row fields", k1 is not None and near(k1["amount_sek"], 123.75) and k1["date"] == "2026-08-20"
       and k1["expense_id"] == EXP6 and k1["type"] == "Card Purchase" and k1["export_status"] == "NOT_EXPORTED"
       and k1["merchant"] == "KIVRA+" and k1["vendor"] == kivra_vendor, k1)
    n = rows.get("aug28-portal")
    if PORTAL:
        _key, v, _merchant = PORTAL
        ok(f"{label} portal route via aliases", n is not None and n["route"] == "portal" and n["route_label"] == ROUTE_LABELS["portal"], n)
        ok(f"{label} portal hint from the alias table", n is not None and n["hint"] == v["portal"], n and n["hint"])
        ok(f"{label} portal vendor", n is not None and n["vendor"] == v.get("name") and near(n["amount_sek"], 210.0), n)
    else:
        ok(f"{label} unknown merchant routes gmail when no alias is routed portal", n is not None and n["route"] == "gmail", n)
    b = rows.get("apr01-bazoom")
    ok(f"{label} pocket route beats the alias", b is not None and b["route"] == "pocket" and b["route_label"] == ROUTE_LABELS["pocket"]
       and near(b["amount_sek"], 59426.85) and b["type"] == "Out of Pocket", b)
    t = rows.get("aug15-test")
    ok(f"{label} gmail route for an unknown merchant", t is not None and t["route"] == "gmail" and t["route_label"] == ROUTE_LABELS["gmail"]
       and t["merchant"] == ESCAPE_MERCHANT and near(t["amount_sek"], 99.0), t)
    ok(f"{label} amounts are positive", all(r["amount_sek"] > 0 for r in rows.values()), [r["amount_sek"] for r in rows.values()])
    br = by_route(m)
    expected_routes = {"kivra", "pocket", "gmail"} | ({"portal"} if PORTAL else set())
    ok(f"{label} by_route routes (attached rows are no work)", set(br) == expected_routes, sorted(br))
    ok(f"{label} by_route kivra", br.get("kivra") is not None and br["kivra"]["count"] == 2 and near(br["kivra"]["amount_sek"], 247.5)
       and br["kivra"]["owner"] == "Oscar" and br["kivra"]["route_label"] == ROUTE_LABELS["kivra"], br.get("kivra"))
    ok(f"{label} by_route pocket", br.get("pocket") is not None and br["pocket"]["count"] == 1
       and near(br["pocket"]["amount_sek"], 59426.85) and br["pocket"]["owner"] == "Oscar", br.get("pocket"))
    ok(f"{label} by_route gmail belongs to the system", br.get("gmail") is not None and br["gmail"]["owner"] == "systemet"
       and br["gmail"]["count"] == (1 if PORTAL else 2), br.get("gmail"))
    if PORTAL:
        ok(f"{label} by_route portal belongs to the system", br.get("portal") is not None and br["portal"]["owner"] == "systemet"
           and br["portal"]["count"] == 1, br.get("portal"))
    s = m["summary"]
    ok(f"{label} summary live missing", s["pleo_missing_live"] == 5 and near(s["pleo_missing_live_sek"], 59983.35),
       (s["pleo_missing_live"], s["pleo_missing_live_sek"]))
    ok(f"{label} summary counts the archive-attached row apart", s["pleo_missing_live_attached"] == 1, s["pleo_missing_live_attached"])
    src = m["sources"]["pleo_missing"]
    ok(f"{label} source detail mentions the attached row", "6 rader" in src["detail"] and "1 bifogad sedan listan gjordes" in src["detail"], src["detail"])
    ok(f"{label} no todo for the archive-attached row", not any(t["key"] == "pleo-missing:attached" for t in m["todo"]),
       [t["key"] for t in m["todo"]])


def check_missing_b(m, label):
    """Format b: the route column is used, the MCP spellings share one route, amounts parse from sv format."""
    live = m["pleo"]["missing_live"]
    ok(f"{label} format b file", live.get("file") == "out/pleo-missing-receipts-2026-08-21.csv" and live.get("date") == "2026-08-21",
       (live.get("file"), live.get("date")))
    rows = live_rows(m)
    ok(f"{label} two rows", len(rows) == 2, sorted(rows))
    f = rows.get("2600500")
    ok(f"{label} forward or attach is the mcp route labelled bifoga via MCP", f is not None and f["route"] == "mcp"
       and f["route_label"] == "bifoga via MCP", f)
    firecrawl = archive.resolve_vendor({"merchant": "FIRECRAWL.DEV", "amount": 981.09, "currency": "SEK"})[0]
    ok(f"{label} sv amount parsed", f is not None and near(f["amount_sek"], 981.09) and f["date"] == "2026-08-17"
       and f["merchant"] == "FIRECRAWL.DEV" and f["vendor"] == firecrawl, f)
    a = rows.get("2600501")
    ok(f"{label} ask Nicolina labelled", a is not None and a["route_label"] == "fråga Nicolina" and near(a["amount_sek"], 123.75), a)
    ok(f"{label} summary live missing", m["summary"]["pleo_missing_live"] == 2 and near(m["summary"]["pleo_missing_live_sek"], 1104.84),
       (m["summary"]["pleo_missing_live"], m["summary"]["pleo_missing_live_sek"]))
    ok(f"{label} pleo-missing todos per route", len(todo_starting(m, "pleo-missing")) == 2, [t["key"] for t in todo_starting(m, "pleo-missing")])
    mcp = todo_by_key(m, "pleo-missing:mcp")
    ok(f"{label} mcp todo in the singular", len(mcp) == 1 and mcp[0]["title"] == "Bifoga 1 kvitto via Pleo MCP"
       and mcp[0]["owner"] == "systemet" and mcp[0]["count"] == 1, mcp)
    ask = todo_by_key(m, "pleo-missing:ask Nicolina")
    ok(f"{label} ask Nicolina todo", len(ask) == 1 and ask[0]["owner"] == "Nicolina" and ask[0]["title"] == "Fråga Nicolina om 1 Pleo-rad", ask)


def check_amex(m, label, open_refs, june_plan=False):
    """Ledger months, tags, receipts, plans, stages and status labels. open_refs: needs-tag refs not answered."""
    amex = m["amex"]
    ok(f"{label} ledger file", amex.get("ledger_file") == "out/amex-2026-05-06_2026-08-02.classified2.csv", amex.get("ledger_file"))
    ok(f"{label} ledger date from mtime", amex.get("ledger_date") == "2026-08-24", amex.get("ledger_date"))
    ok(f"{label} coverage end", amex.get("coverage_end") == "2026-08-02", amex.get("coverage_end"))
    ok(f"{label} months newest first", [r["month"] for r in amex["months"]] == ["2026-08", "2026-07", "2026-06", "2026-05", "2026-04"],
       [r["month"] for r in amex["months"]])
    aug, jul, jun, may, apr = (month(m, "amex", k) for k in ("2026-08", "2026-07", "2026-06", "2026-05", "2026-04"))
    ok(f"{label} august counts", aug is not None and aug["rows"] == 1 and aug["business"] == 0 and aug["personal"] == 1
       and aug["needs_tag"] == 0 and aug["skip"] == 0 and near(aug["business_sek"], 0), aug)
    ok(f"{label} august waits for the csv", aug is not None and aug["plan"] is None and aug["stage"] == "no-csv", aug)
    ok(f"{label} july counts", jul is not None and jul["rows"] == 4 and jul["business"] == 2 and jul["business_other_entity"] == 0
       and jul["personal"] == 0 and jul["needs_tag"] == 1 and jul["skip"] == 1 and near(jul["business_sek"], 3795.36), jul)
    ok(f"{label} july receipts", jul is not None and jul["receipts_found"] == 1 and jul["receipts_missing"] == 1, jul)
    ok(f"{label} july needs_tag_open", jul is not None and jul["needs_tag_open"] == (1 if "ATFIX703" in open_refs else 0), jul)
    ok(f"{label} july plan", jul is not None and jul["plan"] is not None and jul["plan"]["dir"] == "out/fortnox-run-2026-07"
       and jul["plan"]["built"] == "2026-08-24" and jul["plan"]["vouchers"] == 2 and jul["plan"]["receipts"] == 1
       and jul["plan"]["uploaded"] == 1 and jul["plan"]["booked"] == 1 and near(jul["plan"]["total_sek"], 3795.36)
       and jul["plan"]["warnings"] == 2, jul and jul["plan"])
    ok(f"{label} july stage", jul is not None and jul["stage"] == ("untagged" if "ATFIX703" in open_refs else "partial"), jul and jul["stage"])
    ok(f"{label} june counts", jun is not None and jun["rows"] == 5 and jun["business"] == 3 and jun["business_other_entity"] == 1
       and jun["personal"] == 1 and jun["needs_tag"] == 1 and jun["skip"] == 0 and near(jun["business_sek"], 5512.08), jun)
    ok(f"{label} june receipts via the archive page", jun is not None and jun["receipts_found"] == 1 and jun["receipts_missing"] == 2, jun)
    ok(f"{label} june needs_tag_open", jun is not None and jun["needs_tag_open"] == (1 if "ATFIX604" in open_refs else 0), jun)
    if june_plan:
        ok(f"{label} june plan", jun is not None and jun["plan"] is not None and jun["plan"]["vouchers"] == 1 and jun["plan"]["booked"] == 0
           and jun["plan"]["uploaded"] == 0 and jun["plan"]["receipts"] == 0 and jun["plan"]["warnings"] == 1, jun and jun["plan"])
        ok(f"{label} june stage planned", jun is not None and jun["stage"] == ("untagged" if "ATFIX604" in open_refs else "planned"),
           jun and jun["stage"])
    else:
        ok(f"{label} june has no plan", jun is not None and jun["plan"] is None, jun and jun["plan"])
        ok(f"{label} june stage", jun is not None and jun["stage"] == ("untagged" if "ATFIX604" in open_refs else "no-plan"), jun and jun["stage"])
    ok(f"{label} may counts", may is not None and may["rows"] == 1 and may["business"] == 1 and near(may["business_sek"], 1000.0)
       and may["needs_tag"] == 0 and may["receipts_found"] == 0 and may["receipts_missing"] == 1, may)
    ok(f"{label} may plan booked", may is not None and may["plan"] is not None and may["plan"]["vouchers"] == 1 and may["plan"]["booked"] == 1
       and may["plan"]["receipts"] == 0 and may["plan"]["uploaded"] == 0 and near(may["plan"]["total_sek"], 1000.0)
       and may["plan"]["warnings"] == 0, may and may["plan"])
    ok(f"{label} may stage booked", may is not None and may["stage"] == "booked", may and may["stage"])
    ok(f"{label} april holds only personal spend", apr is not None and apr["rows"] == 1 and apr["business"] == 0 and apr["personal"] == 1
       and apr["plan"] is None and apr["needs_tag_open"] == 0, apr)
    ok(f"{label} april stage nothing to book", apr is not None and apr["stage"] == "nothing-to-book"
       and apr["stage_label"] == STAGE_LABELS["nothing-to-book"], apr and (apr["stage"], apr["stage_label"]))
    if apr is not None:
        steps = call(f"{label} april stepper raises", dashboard_html.amex_steps, apr) or []
        ok(f"{label} april stepper is all done", len(steps) == 5 and all(state == "done" for _, state in steps), steps)
    nt = needs_tag_rows(m)
    ok(f"{label} needs_tag rows", set(nt) == {"ATFIX604", "ATFIX703"}, sorted(nt))
    ok(f"{label} needs_tag answered flags", nt.get("ATFIX604") is not None and nt.get("ATFIX703") is not None
       and nt["ATFIX604"]["answered"] is ("ATFIX604" not in open_refs) and nt["ATFIX703"]["answered"] is ("ATFIX703" not in open_refs),
       [(k, v["answered"]) for k, v in nt.items()])
    ok(f"{label} needs_tag row fields", nt.get("ATFIX703") is not None and near(nt["ATFIX703"]["amount_sek"], 189.0)
       and nt["ATFIX703"]["card"] == "Amex 1022" and nt["ATFIX703"]["date"] == "2026-07-15"
       and nt["ATFIX703"]["merchant"] == ESCAPE_MERCHANT_AMEX and nt["ATFIX703"]["why"] == "tagged both ways", nt.get("ATFIX703"))
    br = business_rows(m)
    ok(f"{label} business rows include both entities", set(br) == {"ATFIX501", "ATFIX601", "ATFIX603", "ATFIX605", "ATFIX701", "ATFIX702"}, sorted(br))
    w = br.get("ATFIX701")
    wincher = archive.resolve_vendor({"merchant": WINCHER_MERCHANT, "amount": 3570.54, "currency": "SEK"})[0]
    ok(f"{label} wincher row", w is not None and w["receipt"] is True and w["receipt_file"] and w["receipt_file"].endswith("ATFIX701_receipt.pdf")
       and w["archive_id"] == PAGE_IDS.get("P5") and w["archive_id"] and w["vendor"] == wincher and w["card"] == "Amex 1022"
       and w["month"] == "2026-07" and w["entity"] == "viseo" and w["bas_account"] == "6540" and w["vat_regime"] == "eu_rc"
       and near(w["amount_sek"], 3570.54), w)
    ok(f"{label} wincher voucher", w is not None and re.sub(r"\s", "", str(w["voucher"] or "")) == "A12" and w["status_label"] == "bokförd",
       w and (w["voucher"], w["status_label"]))
    k = br.get("ATFIX702")
    ok(f"{label} kjell row without receipt", k is not None and k["receipt"] is False and not k["receipt_file"] and not k["archive_id"]
       and not k["voucher"] and k["bas_account"] == "5410" and k["status_label"] == "kvitto saknas", k)
    s6 = br.get("ATFIX601")
    ok(f"{label} archive-only receipt counts", s6 is not None and s6["receipt"] is True and s6["archive_id"] == P6 and not s6["voucher"], s6)
    ok(f"{label} planned row label", s6 is not None and s6["status_label"] == ("plan klar" if june_plan else "kvitto funnet, plan saknas"),
       s6 and s6["status_label"])
    m5 = br.get("ATFIX501")
    ok(f"{label} booked row voucher", m5 is not None and re.sub(r"\s", "", str(m5["voucher"] or "")) == "A11" and m5["receipt"] is False, m5)
    ok(f"{label} other entity row", br.get("ATFIX603") is not None and br["ATFIX603"]["entity"] == "5555media"
       and br["ATFIX603"]["status_label"] == "5555 Media AB, utanför Viseo-planen", br.get("ATFIX603"))
    h = br.get("ATFIX605")
    ok(f"{label} row without konto is labelled", h is not None and h["receipt"] is False and h["bas_account"] == ""
       and h["status_label"] == ("utanför planen: bas_account saknas" if june_plan else "kvitto och konto saknas"),
       h and h["status_label"])
    ok(f"{label} status labels are text", all(isinstance(r["status_label"], str) and r["status_label"] for r in br.values()),
       [r["status_label"] for r in br.values()])
    ok(f"{label} next month", amex.get("next_month") == "2026-08" and amex.get("next_month_csv_present") is False,
       (amex.get("next_month"), amex.get("next_month_csv_present")))
    s = m["summary"]
    ok(f"{label} summary amex rows and sek", s["amex_business_rows"] == 6 and near(s["amex_business_sek"], 10307.44),
       (s["amex_business_rows"], s["amex_business_sek"]))
    ok(f"{label} summary receipts and tags", s["amex_receipts_found"] == 2 and s["amex_needs_tag_open"] == len(open_refs),
       (s["amex_receipts_found"], s["amex_needs_tag_open"]))
    ok(f"{label} summary vouchers", s["amex_vouchers_planned"] == (4 if june_plan else 3) and s["amex_vouchers_booked"] == 2,
       (s["amex_vouchers_planned"], s["amex_vouchers_booked"]))
    ok(f"{label} summary next month", s["next_amex_month"] == "2026-08" and s["next_amex_csv_present"] is False,
       (s["next_amex_month"], s["next_amex_csv_present"]))


def check_todo_order(m, label):
    todo = m["todo"]
    ranks = [sev_rank(t["severity"]) for t in todo]
    ok(f"{label} todo severity order", ranks == sorted(ranks), [(t["key"], t["severity"]) for t in todo])
    good = True
    for sev in ("crit", "warn", "info"):
        pairs = [(t["count"], float(t["amount_sek"] or 0)) for t in todo if t["severity"] == sev and isinstance(t["count"], int)]
        good = good and pairs == sorted(pairs, key=lambda p: (-p[0], -p[1]))
    ok(f"{label} todo count then amount order within severity", good, [(t["key"], t["severity"], t["count"], t["amount_sek"]) for t in todo])
    keys = [t["key"] for t in todo]
    ok(f"{label} todo keys unique", len(keys) == len(set(keys)), keys)


def check_todo_main(m, label):
    """The rule set on the full root (two exports, one open needs-tag, archive warnings)."""
    check_todo_order(m, label)
    r = todo_by_key(m, "pleo-reattach")
    ok(f"{label} pleo-reattach", len(r) == 1 and r[0]["severity"] == "crit" and r[0]["owner"] == "Oscar" and r[0]["count"] == 3, r)
    ok(f"{label} pleo-reattach command", r and r[0]["command"] and "archive.py ingest pleo-export" in r[0]["command"]
       and "archive.py check" in r[0]["command"], r and r[0]["command"])
    ok(f"{label} pleo-reattach is first", m["todo"] and m["todo"][0]["key"] == "pleo-reattach", m["todo"] and m["todo"][0]["key"])
    pm = {t["key"]: t for t in todo_starting(m, "pleo-missing")}
    expected = {"pleo-missing:kivra", "pleo-missing:pocket", "pleo-missing:gmail"} | ({"pleo-missing:portal"} if PORTAL else set())
    ok(f"{label} pleo-missing per route", set(pm) == expected, sorted(pm))
    ok(f"{label} pleo-missing kivra entry", pm.get("pleo-missing:kivra") is not None and pm["pleo-missing:kivra"]["severity"] == "warn"
       and pm["pleo-missing:kivra"]["owner"] == "Oscar" and pm["pleo-missing:kivra"]["count"] == 2
       and near(pm["pleo-missing:kivra"]["amount_sek"], 247.5)
       and pm["pleo-missing:kivra"]["title"] == "Hämta 2 Kivra-kvitton i Kivra-appen", pm.get("pleo-missing:kivra"))
    ok(f"{label} pleo-missing pocket entry", pm.get("pleo-missing:pocket") is not None and pm["pleo-missing:pocket"]["owner"] == "Oscar"
       and pm["pleo-missing:pocket"]["count"] == 1 and near(pm["pleo-missing:pocket"]["amount_sek"], 59426.85)
       and pm["pleo-missing:pocket"]["title"] == "Lägg kvitto på 1 eget utlägg i Pleo", pm.get("pleo-missing:pocket"))
    ok(f"{label} pleo-missing gmail entry", pm.get("pleo-missing:gmail") is not None and pm["pleo-missing:gmail"]["owner"] == "systemet"
       and (not PORTAL or pm["pleo-missing:gmail"]["title"] == "Sök 1 kvitto i Gmail och bifoga via MCP"), pm.get("pleo-missing:gmail"))
    keys = [t["key"] for t in m["todo"]]
    ok(f"{label} equal counts fall back to the larger amount", "pleo-missing:pocket" in keys and "pleo-missing:gmail" in keys
       and keys.index("pleo-missing:pocket") < keys.index("pleo-missing:gmail"), keys)
    if PORTAL:
        p = pm.get("pleo-missing:portal")
        ok(f"{label} pleo-missing portal entry", p is not None and p["owner"] == "systemet" and p["count"] == 1
           and p["title"] == "Hämta 1 kvitto från leverantörsportalen", p)
        ok(f"{label} portal detail is a Swedish vendor summary", p is not None and "tabellen" in p["detail"]
           and f"{PORTAL[1].get('name')} 1" in p["detail"] and PORTAL[1]["portal"] not in p["detail"], p and p["detail"])
    c = todo_by_key(m, "amex-csv")
    ok(f"{label} amex-csv", len(c) == 1 and c[0]["severity"] == "warn" and c[0]["owner"] == "Oscar", c)
    ok(f"{label} amex-csv command", c and c[0]["command"] and "amex_import.py data/amex/*.csv" in c[0]["command"]
       and "month_end.py --month 2026-08" in c[0]["command"], c and c[0]["command"])
    ok(f"{label} amex-csv names august", c and ("augusti 2026" in c[0]["title"] or "2026-08" in c[0]["title"]), c and c[0]["title"])
    t = todo_by_key(m, "amex-tags")
    ok(f"{label} amex-tags", len(t) == 1 and t[0]["severity"] == "warn" and t[0]["owner"] == "Oscar" and t[0]["count"] == 1
       and t[0]["title"] == "Tagga 1 Amex-rad", t)
    ok(f"{label} amex-tags detail names the files", t and "amex-needs-tag-2026-08-24.csv" in t[0]["detail"]
       and "data/tags/overrides.csv" in t[0]["detail"], t and t[0]["detail"])
    f = todo_by_key(m, "fortnox-execute:2026-07")
    ok(f"{label} fortnox-execute july", len(f) == 1 and f[0]["severity"] == "info" and f[0]["owner"] == "Oscar" and f[0]["count"] == 2
       and near(f[0]["amount_sek"], 3795.36), f)
    ok(f"{label} fortnox-execute command", f and f[0]["command"] and "fortnox_execute.py out/fortnox-run-2026-07" in f[0]["command"]
       and "sync-fortnox out/fortnox-run-2026-07" in f[0]["command"], f and f[0]["command"])
    ok(f"{label} no fortnox-execute for the booked month", not todo_by_key(m, "fortnox-execute:2026-05"), [t["key"] for t in m["todo"]])
    p = todo_starting(m, "fortnox-plan-missing")
    ok(f"{label} fortnox-plan-missing june", len(p) == 1 and p[0]["severity"] == "info" and p[0]["owner"] == "systemet"
       and ("2026-06" in p[0]["key"] + p[0]["title"] + p[0]["detail"] or "juni" in (p[0]["title"] + p[0]["detail"]).lower()), p)
    ok(f"{label} fortnox-plan-missing counts the Viseo rows only", p and p[0]["count"] == 2 and near(p[0]["amount_sek"], 5262.08)
       and "2 företagsrader för Viseo AB, 5 262,08 SEK" in p[0]["detail"], p and (p[0]["count"], p[0]["amount_sek"], p[0]["detail"]))
    ok(f"{label} no pleo-export-stale for a 2-day export", not todo_by_key(m, "pleo-export-stale"), [t["key"] for t in m["todo"]])
    a = todo_by_key(m, "archive-review")
    ok(f"{label} archive-review counts warnings outside the worklist", len(a) == 1 and a[0]["severity"] == "info"
       and a[0]["owner"] == "Oscar" and a[0]["count"] == 1 and a[0]["title"] == "Titta på 1 arkivvarning utanför kvittofel-listan", a)
    ok(f"{label} archive-review skips the duplicate whose partner page is on the worklist", a and a[0]["detail"] == "1 " + WARNING_LABELS["vendor-missing"] + ".",
       a and a[0]["detail"])
    ok(f"{label} no archive-errors", not todo_by_key(m, "archive-errors"), [t["key"] for t in m["todo"]])
    ok(f"{label} todo links are section anchors", all(isinstance(t["link"], str) and t["link"] for t in m["todo"]),
       [t["link"] for t in m["todo"]])
    ok(f"{label} todo text has no dashes", no_dashes(json.dumps(m["todo"], ensure_ascii=False)))


def check_sources_main(m, label):
    src = m["sources"]
    ok(f"{label} pleo_export ok", src["pleo_export"]["state"] == "ok" and src["pleo_export"]["date"] == NEWER
       and src["pleo_export"]["age_days"] == 2 and src["pleo_export"]["path"] == "data/pleo/expenses_" + NEWER, src["pleo_export"])
    ok(f"{label} pleo_missing ok", src["pleo_missing"]["state"] == "ok" and src["pleo_missing"]["date"] == "2026-08-31"
       and src["pleo_missing"]["age_days"] == 12 and src["pleo_missing"]["path"] == "out/pleo-missing-receipts-2026-08-31.csv",
       src["pleo_missing"])
    ok(f"{label} worklist ok", src["worklist"]["state"] == "ok" and src["worklist"]["date"] == "2026-09-03"
       and src["worklist"]["age_days"] == 9 and src["worklist"]["path"] == "out/pleo-kvittofel-2026-09-03.txt", src["worklist"])
    ok(f"{label} amex_csv stale", src["amex_csv"]["state"] == "stale" and src["amex_csv"]["date"] == "2026-08-02"
       and src["amex_csv"]["age_days"] == 41 and src["amex_csv"]["path"] == "data/amex", src["amex_csv"])
    ok(f"{label} amex_ledger ok", src["amex_ledger"]["state"] == "ok" and src["amex_ledger"]["date"] == "2026-08-24"
       and src["amex_ledger"]["path"] == "out/amex-2026-05-06_2026-08-02.classified2.csv", src["amex_ledger"])
    ok(f"{label} fortnox_runs ok", src["fortnox_runs"]["state"] == "ok" and src["fortnox_runs"]["date"] == "2026-08-24"
       and "fortnox-run" in src["fortnox_runs"]["path"], src["fortnox_runs"])
    ok(f"{label} archive ok", src["archive"]["state"] == "ok" and src["archive"]["path"] == "archive"
       and src["archive"]["date"] == str(m["archive"]["updated"])[:10], src["archive"])
    ok(f"{label} source ages are ints", all(isinstance(s["age_days"], int) for s in src.values()),
       [(k, s["age_days"]) for k, s in src.items()])
    ok(f"{label} pleo_export detail", src["pleo_export"]["detail"] == "5 rader, 3 kvittofiler", src["pleo_export"]["detail"])
    ok(f"{label} amex_csv detail in the singular", src["amex_csv"]["detail"] == "1 fil, sista köp 2026-08-02, augusti saknas",
       src["amex_csv"]["detail"])
    ok(f"{label} fortnox_runs detail names the months in Swedish", src["fortnox_runs"]["detail"].startswith("maj 2026, juli 2026, "),
       src["fortnox_runs"]["detail"])
    ok(f"{label} worklist detail", src["worklist"]["detail"].startswith("5 poster i 4 grupper, "), src["worklist"]["detail"])
    ok(f"{label} source details are non-empty Swedish text", all(s["detail"].strip() for s in src.values())
       and no_dashes(json.dumps(src, ensure_ascii=False)), src)


def check_archive(m, label):
    arc = m["archive"]
    ok(f"{label} archive root", os.path.isabs(str(arc["root"])) and arc["root"].endswith("archive"), arc["root"])
    ok(f"{label} pages and sum", arc["pages"] == ARCHIVE_PAGES_TOTAL and near(arc["total_sek"], ARCHIVE_SEK), (arc["pages"], arc["total_sek"]))
    ok(f"{label} updated from index.md", TS_RE.match(str(arc["updated"])) is not None, arc["updated"])
    ok(f"{label} previews", arc["previews"] == 7, arc["previews"])
    ok(f"{label} no errors", arc["errors"] == [], arc["errors"])
    ok(f"{label} warnings_total", arc["warnings_total"] == 5 and len(arc["warnings"]) == 5, (arc["warnings_total"], len(arc["warnings"])))
    kinds = warning_kinds(m)
    ok(f"{label} warning kinds", {k: v["count"] for k, v in kinds.items()} == EXPECTED_WARNINGS, {k: v["count"] for k, v in kinds.items()})
    ok(f"{label} warning kind labels", all(v["label"] == WARNING_LABELS.get(k) for k, v in kinds.items()), [(k, v["label"]) for k, v in kinds.items()])
    ok(f"{label} warning kinds sorted by count", [v["count"] for v in arc["warning_kinds"]] == sorted((v["count"] for v in arc["warning_kinds"]), reverse=True),
       [(v["key"], v["count"]) for v in arc["warning_kinds"]])
    by_page = {}
    for w in arc["warnings"]:
        by_page.setdefault(w["page_id"], []).append(w)
    ok(f"{label} warning page ids", set(by_page) == {P1, P2, P4, P7, P8}, sorted(by_page))
    ok(f"{label} warning kinds per page", by_page.get(P1, [{}])[0].get("kind") == "earlier-period"
       and by_page.get(P2, [{}])[0].get("kind") == "duplicate-invoice" and by_page.get(P4, [{}])[0].get("kind") == "other-company"
       and by_page.get(P7, [{}])[0].get("kind") == "duplicate-invoice" and by_page.get(P8, [{}])[0].get("kind") == "vendor-missing",
       [(w["page_id"], w["kind"]) for w in arc["warnings"]])
    ok(f"{label} warning months from the stem", all(w["month"] == w["page_id"][:7] for w in arc["warnings"]),
       [(w["page_id"], w["month"]) for w in arc["warnings"]])
    ok(f"{label} warning text carries the check message", any("32 days before" in w["text"] for w in arc["warnings"])
       and any("INV-7" in w["text"] for w in arc["warnings"]), [w["text"] for w in arc["warnings"]])
    ok(f"{label} months newest first", [r["month"] for r in arc["months"]] == ["2026-08", "2026-07", "2026-06"], [r["month"] for r in arc["months"]])
    a8, a7, a6 = (month(m, "archive", k) for k in ("2026-08", "2026-07", "2026-06"))
    ok(f"{label} august month", a8 is not None and a8["pages"] == 2 and near(a8["amount_sek"], 174.0) and a8["pleo"] == 2 and a8["amex"] == 0
       and a8["attached"] == 1 and a8["in_pleo"] == 1 and a8["warnings"] == 1, a8)
    ok(f"{label} july month", a7 is not None and a7["pages"] == 3 and near(a7["amount_sek"], 4596.63) and a7["pleo"] == 2 and a7["amex"] == 1
       and a7["attached"] == 1 and a7["in_pleo"] == 1 and a7["warnings"] == 2, a7)
    ok(f"{label} june month", a6 is not None and a6["pages"] == 3 and near(a6["amount_sek"], 1894.0) and a6["pleo"] == 2 and a6["amex"] == 1
       and a6["attached"] == 0 and a6["in_pleo"] == 2 and a6["warnings"] == 2, a6)
    st = arc["status"]
    ok(f"{label} status classes", st["attached"] == 2 and st["in_pleo"] == 4 and st["no_status"] == 2, st)
    ok(f"{label} fortnox status classes", st["fortnox_booked"] == 1 and st["fortnox_open"] == 1, st)
    ok(f"{label} text methods", arc["text_methods"] == {"pdfkit": 7, "html": 1}, arc["text_methods"])
    s = m["summary"]
    ok(f"{label} summary archive", s["archive_pages"] == ARCHIVE_PAGES_TOTAL and near(s["archive_sek"], ARCHIVE_SEK)
       and s["archive_errors"] == 0 and s["archive_warnings"] == 5, (s["archive_pages"], s["archive_sek"], s["archive_errors"], s["archive_warnings"]))


def check_render(html, label, model):
    ok(f"{label} render returns text", isinstance(html, str) and len(html) > 2000, type(html))
    if not isinstance(html, str):
        return
    ok(f"{label} no dashes in the page", no_dashes(html), [i for i, ch in enumerate(html) if ch in (EM_DASH, EN_DASH, BAR_DASH)][:5])
    ok(f"{label} starts with the charset meta", html.lstrip().startswith('<meta charset="utf-8">'), html[:80])
    ok(f"{label} viewport and title", '<meta name="viewport" content="width=device-width, initial-scale=1">' in html
       and "<title>Kvittotavlan</title>" in html)
    ok(f"{label} font link and one style block", "fonts.googleapis.com" in html and html.count("<style") == 1, html.count("<style"))
    low = html.lower()
    ok(f"{label} no document skeleton tags", "<!doctype" not in low and "<html" not in low and "<head>" not in low and "<body" not in low)
    ok(f"{label} no script tags", "<script" not in low)
    for sid in SECTION_IDS:
        ok(f"{label} section id {sid}", f'id="{sid}"' in html)
    ok(f"{label} semantic landmarks", "<header" in low and "<main" in low and "<section" in low and "<footer" in low)
    ok(f"{label} theme tokens", re.search(r"prefers-color-scheme:\s*dark", html) is not None and 'data-theme="dark"' in html
       and "--ground" in html and re.search(r"background:\s*var\(--ground\)", html) is not None)
    ok(f"{label} tabular numbers and table wrap", "tabular-nums" in html and re.search(r"overflow-x:\s*auto", html) is not None)
    ok(f"{label} masthead", "Kvittotavlan" in html and "Viseo AB" in html and "läget " + model["today"] in html)
    ok(f"{label} swedish headings", "Att göra" in html and "Pleo-kortet" in html and "Amex till Fortnox" in html
       and "Kvittoarkivet" in html and "Underlag" in html)
    ok(f"{label} footer", "Genererad" in html and "scripts/dashboard.py" in html)


def check_render_main(html, label, model):
    check_render(html, label, model)
    if not isinstance(html, str):
        return
    ok(f"{label} escapes < and & in a merchant", "&lt;TEST&gt; &amp; CO" in html and "<TEST>" not in html and "ROR &amp; FLAK &lt;TEST&gt; AB" in html)
    ok(f"{label} thousands separator and comma decimals", re.search(r"3[  ]570,54", html) is not None
       and re.search(r"59[  ]426,85", html) is not None and "123,75" in html)
    ok(f"{label} no dot-decimal amounts leak", re.search(r"\b3570\.54\b", html) is None and re.search(r"\b59426\.85\b", html) is None)
    ok(f"{label} status pills", "ny fil bifogad" in html and "saknas i exporten" in html and "öppen" in html)
    ok(f"{label} stage labels", all(lbl in html for lbl in (STAGE_LABELS["no-csv"], STAGE_LABELS["untagged"], STAGE_LABELS["no-plan"], STAGE_LABELS["booked"])))
    ok(f"{label} route labels", all(lbl in html for lbl in ROUTE_LABELS.values()) and KIVRA_HINT in html)
    ok(f"{label} group labels", all(lbl in html for lbl in GROUP_LABELS.values()))
    ok(f"{label} owner chips", "Oscar" in html and "systemet" in html)
    ok(f"{label} commands in code blocks", "<code" in html and "fortnox_execute.py out/fortnox-run-2026-07" in html
       and "archive.py ingest pleo-export" in html)
    ok(f"{label} todo titles on the page", all(t["title"] in html for t in model["todo"]), [t["title"] for t in model["todo"] if t["title"] not in html])
    masthead = html.split("</style>", 1)[-1][:3000]
    ok(f"{label} lede names the top todo", model["todo"] and model["todo"][0]["title"] in masthead, masthead[:400])
    ok(f"{label} month names in Swedish", "augusti 2026" in html and "juli 2026" in html and "juni 2026" in html and "maj 2026" in html)
    ok(f"{label} compared exports note", BASELINE in html and NEWER in html)
    ok(f"{label} archive warning list", "<details" in html and P1 in html and "32 days before" in html)
    ok(f"{label} warning kind chips", WARNING_LABELS["duplicate-invoice"] in html and WARNING_LABELS["other-company"] in html)
    ok(f"{label} source paths", "out/pleo-kvittofel-2026-09-03.txt" in html and "data/pleo/expenses_" + NEWER in html)
    ok(f"{label} text methods footer", "pdfkit" in html)
    ok(f"{label} receipt numbers", "2600102" in html and "2600103" in html and "2600104" in html)
    ok(f"{label} needs-tag ref in mono", "ATFIX703" in html)
    ok(f"{label} other entity named from archive", "5555 Media AB" in html)
    ok(f"{label} payouts drawn apart", "utbetalning" in html and 'class="seg pay"' in html)
    ok(f"{label} nothing-to-book label", STAGE_LABELS["nothing-to-book"] in html)
    ok(f"{label} live ids get the expense id", "<th>Id</th>" in html and 'title="' + EXPK + '"' in html)
    ok(f"{label} singular row count", not PORTAL or "1 rad, 99,00 SEK" in html)
    ok(f"{label} plan note under the card", 'class="plan-note"' in html and 'byggd <span class="num">2026-08-24</span>' in html)
    ok(f"{label} hatched amex segment", "repeating-linear-gradient" in html)


def check_render_empty(html, label, model):
    check_render(html, label, model)
    if not isinstance(html, str):
        return
    ok(f"{label} underlag saknas", "underlag saknas" in html)
    ok(f"{label} empty todo state", "Inget att göra just nu" in html)
    ok(f"{label} lede counts the missing sources", "Inget att göra just nu, men 7 underlag saknas." in html)
    ok(f"{label} archive tile is dead", 'Kvittoarkivet</span><span class="chip dead">saknas</span>' in html)


def check_empty_model(m, label):
    src = m["sources"]
    ok(f"{label} every source missing", all(s["state"] == "missing" and s["date"] is None and s["age_days"] is None for s in src.values()),
       [(k, s["state"], s["date"], s["age_days"]) for k, s in src.items()])
    ok(f"{label} pleo empty", m["pleo"]["export_dir"] is None and m["pleo"]["months"] == [] and m["pleo"]["missing_live"]["rows"] == []
       and m["pleo"]["flagged"]["groups"] == [] and m["pleo"]["same_file_groups_now"] == [], m["pleo"])
    ok(f"{label} amex empty", m["amex"]["ledger_file"] is None and m["amex"]["months"] == [] and m["amex"]["needs_tag"] == []
       and m["amex"]["business_rows"] == [], m["amex"])
    ok(f"{label} archive empty", m["archive"]["pages"] == 0 and m["archive"]["warnings"] == [] and m["archive"]["months"] == [], m["archive"])
    s = m["summary"]
    ok(f"{label} summary zeros and nulls", s["pleo_rows"] == 0 and s["pleo_missing_live"] is None and s["flagged_total"] == 0
       and s["amex_business_rows"] == 0 and s["archive_pages"] == 0 and s["next_amex_csv_present"] is False,
       s)
    ok(f"{label} no todo without inputs", m["todo"] == [], [t["key"] for t in m["todo"]])


def run_cli(*args):
    return subprocess.run([sys.executable, SCRIPT] + list(args), capture_output=True, text=True)


def check_cli(work, full_root, empty_root, lib_model):
    before = content_snapshot(full_root)
    p = run_cli("--root", full_root, "--today", TODAY, "--json")
    ok("cli --json exit 0", p.returncode == 0, (p.returncode, p.stderr[-800:]))
    js = None
    try:
        js = json.loads(p.stdout)
    except ValueError as exc:
        ok("cli --json prints valid JSON", False, f"{exc}: {p.stdout[:200]!r}")
    if js is not None:
        ok("cli --json prints valid JSON", True)
        ok("cli --json keys", has_keys(js, MODEL_KEYS), sorted(js))
        ok("cli --json today", js.get("today") == TODAY, js.get("today"))
        for k in ("sources", "summary", "todo", "pleo", "amex"):
            ok(f"cli --json {k} equals the library model", js.get(k) == lib_model.get(k), k)
        cli_arc = {k: v for k, v in (js.get("archive") or {}).items() if k != "root"}
        lib_arc = {k: v for k, v in (lib_model.get("archive") or {}).items() if k != "root"}
        ok("cli --json archive equals the library model", cli_arc == lib_arc,
           [k for k in set(cli_arc) | set(lib_arc) if cli_arc.get(k) != lib_arc.get(k)])
        ok("cli --json roots name the same directory", os.path.samefile(js.get("root"), lib_model.get("root"))
           and os.path.samefile((js.get("archive") or {}).get("root"), (lib_model.get("archive") or {}).get("root")),
           (js.get("root"), lib_model.get("root")))
    ok("cli --json writes nothing", content_snapshot(full_root) == before)
    out_file = os.path.join(work, "cli-out", "tavla.html")
    p = run_cli("--root", full_root, "--today", TODAY, "--out", out_file)
    ok("cli --out exit 0", p.returncode == 0, (p.returncode, p.stderr[-800:]))
    ok("cli --out writes the file", os.path.isfile(out_file))
    ok("cli --out writes nothing else", content_snapshot(full_root) == before)
    if os.path.isfile(out_file):
        with open(out_file, encoding="utf-8") as fh:
            html = fh.read()
        ok("cli --out file is the page", html.lstrip().startswith('<meta charset="utf-8">') and all(f'id="{s}"' in html for s in SECTION_IDS)
           and no_dashes(html) and "&lt;TEST&gt; &amp; CO" in html)
    p = run_cli("--root", empty_root, "--today", TODAY, "--json")
    ok("cli empty root --json exit 0", p.returncode == 0, (p.returncode, p.stderr[-800:]))
    try:
        js = json.loads(p.stdout)
    except ValueError as exc:
        js = None
        ok("cli empty root JSON", False, f"{exc}: {p.stdout[:200]!r}")
    if js is not None:
        ok("cli empty root keys", has_keys(js, MODEL_KEYS) and has_keys(js.get("summary"), SUMMARY_KEYS)
           and has_keys(js.get("sources"), SOURCE_KEYS), sorted(js))
        ok("cli empty root sources missing", all(s["state"] == "missing" for s in js["sources"].values()),
           [(k, s["state"]) for k, s in js["sources"].items()])
    empty_out = os.path.join(work, "cli-out", "tom.html")
    p = run_cli("--root", empty_root, "--today", TODAY, "--out", empty_out)
    ok("cli empty root --out exit 0", p.returncode == 0, (p.returncode, p.stderr[-800:]))
    if os.path.isfile(empty_out):
        with open(empty_out, encoding="utf-8") as fh:
            html = fh.read()
        ok("cli empty root page says underlag saknas", "underlag saknas" in html and "Inget att göra just nu" in html)
        ok("cli empty root page has no dashes", no_dashes(html))
    else:
        ok("cli empty root writes the page", False, empty_out)
    ok("cli empty root left it empty", os.listdir(empty_root) == [], os.listdir(empty_root))
    p = run_cli("--root", empty_root, "--today", TODAY, "--archive", os.path.join(full_root, "archive"), "--json")
    ok("cli --archive exit 0", p.returncode == 0, (p.returncode, p.stderr[-800:]))
    try:
        js = json.loads(p.stdout)
        ok("cli --archive overrides the root", js["archive"]["pages"] == ARCHIVE_PAGES_TOTAL and js["sources"]["archive"]["state"] == "ok"
           and js["pleo"]["export_dir"] is None, (js["archive"]["pages"], js["sources"]["archive"]["state"]))
    except (ValueError, KeyError, TypeError) as exc:
        ok("cli --archive JSON", False, f"{exc}: {p.stdout[:200]!r}")
    p = run_cli("--root", empty_root, "--json")
    ok("cli default today", p.returncode == 0 and datetime.date.today().isoformat() in p.stdout[:400], (p.returncode, p.stdout[:120]))
    ok("cli empty root still empty", os.listdir(empty_root) == [], os.listdir(empty_root))


# ----------------------------------------------------------------------------- main

def main():
    global dashboard, dashboard_html
    try:
        import dashboard as _dashboard
        import dashboard_html as _dashboard_html
    except Exception as exc:  # the suite cannot run without the modules under test
        print(f"FAIL import scripts/dashboard.py and scripts/dashboard_html.py: {exc!r}")
        sys.exit(1)
    dashboard, dashboard_html = _dashboard, _dashboard_html
    ok("dashboard exposes build_model and main", callable(getattr(dashboard, "build_model", None))
       and callable(getattr(dashboard, "main", None)))
    ok("dashboard_html exposes render", callable(getattr(dashboard_html, "render", None)))
    for mod, name in ((dashboard, "dashboard.py"), (dashboard_html, "dashboard_html.py")):
        with open(mod.__file__, encoding="utf-8") as fh:
            src = fh.read()
        ok(f"{name} has no dashes", no_dashes(src))
    global PORTAL
    with open(os.path.join(SCRIPTS, "vendor_aliases.json"), encoding="utf-8") as fh:
        aliases = json.load(fh)
    PORTAL = portal_alias(aliases)
    if PORTAL is None:
        print("note: no alias routed portal in vendor_aliases.json, portal assertions skipped")
    work = tempfile.mkdtemp(prefix="test_dashboard_")

    # --- baseline export only: every flagged item is open, the export is stale ---
    base = make_root(work, "base", second_export=False, missing="both")
    mb = build(base, TODAY)
    if mb is not None:
        check_shape(mb, "base")
        check_pleo_export_counts(mb, "base", BASELINE)
        check_flagged(mb, "base", None)
        check_missing_a(mb, "base")
        html_b = dashboard_html.render(mb)
        ok("base render lists the archive-attached row as done", "bifogat sedan listan gjordes" in html_b and "klart" in html_b
           and "aug12-testurl" in html_b)
        check_todo_order(mb, "base")
        r = todo_by_key(mb, "pleo-reattach")
        ok("base pleo-reattach counts every open item", len(r) == 1 and r[0]["count"] == 5, r)
        st = todo_by_key(mb, "pleo-export-stale")
        ok("base pleo-export-stale", len(st) == 1 and st[0]["severity"] == "info" and st[0]["owner"] == "Oscar", st)
        ok("base pleo_export source stale", mb["sources"]["pleo_export"]["state"] == "stale"
           and mb["sources"]["pleo_export"]["age_days"] == 22 and mb["sources"]["pleo_export"]["date"] == BASELINE,
           mb["sources"]["pleo_export"])
        ok("base pleo_missing still ok", mb["sources"]["pleo_missing"]["state"] == "ok", mb["sources"]["pleo_missing"])

    # --- two exports: refiled and gone, the main reference model ---
    full = make_root(work, "full", second_export=True, missing="both")
    mf = build(full, TODAY)
    html = None
    if mf is not None:
        check_shape(mf, "full")
        check_pleo_export_counts(mf, "full", NEWER)
        check_flagged(mf, "full", "data/pleo/expenses_" + NEWER)
        check_missing_a(mf, "full")
        check_amex(mf, "full", open_refs={"ATFIX703"})
        check_todo_main(mf, "full")
        check_sources_main(mf, "full")
        check_archive(mf, "full")
        html = call("render full raises", dashboard_html.render, mf)
        check_render_main(html, "render full", mf)
        # a later today: both Pleo inputs stale, the todo says so
        ml = build(full, LATER)
        if ml is not None:
            ok("later pleo_export stale", ml["sources"]["pleo_export"]["state"] == "stale" and ml["sources"]["pleo_export"]["age_days"] == 16,
               ml["sources"]["pleo_export"])
            ok("later pleo_missing stale", ml["sources"]["pleo_missing"]["state"] == "stale" and ml["sources"]["pleo_missing"]["age_days"] == 26,
               ml["sources"]["pleo_missing"])
            ok("later pleo-export-stale todo", len(todo_by_key(ml, "pleo-export-stale")) == 1, [t["key"] for t in ml["todo"]])
            ok("later today in the model", ml["today"] == LATER, ml["today"])
        # mid-month: the next Amex month is still running, so neither the source nor a todo complains
        mid = build(full, "2026-08-20")
        if mid is not None:
            ok("mid-month no amex-csv todo", not todo_by_key(mid, "amex-csv"), [t["key"] for t in mid["todo"]])
            ok("mid-month amex_csv source ok", mid["sources"]["amex_csv"]["state"] == "ok"
               and "saknas" not in mid["sources"]["amex_csv"]["detail"], mid["sources"]["amex_csv"])

    # --- newer export where a flagged row lost its file: still open, never "refiled" ---
    lf = make_root(work, "lostfile", second_export=True, lost_file=True, with_archive=False)
    mlf = build(lf, TODAY)
    if mlf is not None:
        check_lostfile(mlf, "lostfile")

    # --- format b only ---
    fb = make_root(work, "fmtb", missing="b")
    mfb = build(fb, TODAY)
    if mfb is not None:
        check_missing_b(mfb, "fmtb")
        ok("fmtb pleo_missing source", mfb["sources"]["pleo_missing"]["path"] == "out/pleo-missing-receipts-2026-08-21.csv"
           and mfb["sources"]["pleo_missing"]["date"] == "2026-08-21" and mfb["sources"]["pleo_missing"]["state"] == "stale",
           mfb["sources"]["pleo_missing"])

    # --- no missing list at all ---
    nl = make_root(work, "nolist", missing=None, with_archive=False)
    mnl = build(nl, TODAY)
    if mnl is not None:
        ok("nolist missing_live empty", mnl["pleo"]["missing_live"]["file"] is None and mnl["pleo"]["missing_live"]["rows"] == []
           and mnl["pleo"]["missing_live"]["by_route"] == [], mnl["pleo"]["missing_live"])
        ok("nolist summary null", mnl["summary"]["pleo_missing_live"] is None, mnl["summary"]["pleo_missing_live"])
        ok("nolist source missing", mnl["sources"]["pleo_missing"]["state"] == "missing", mnl["sources"]["pleo_missing"])
        ok("nolist no pleo-missing todo", not todo_starting(mnl, "pleo-missing"), [t["key"] for t in mnl["todo"]])
        ok("nolist archive missing", mnl["sources"]["archive"]["state"] == "missing" and mnl["archive"]["pages"] == 0
           and not todo_by_key(mnl, "archive-errors"), (mnl["sources"]["archive"], [t["key"] for t in mnl["todo"]]))
        ok("nolist check_still_warns false without an archive", all(it["check_still_warns"] is False for it in flagged_items(mnl).values()),
           [(k, it["check_still_warns"]) for k, it in flagged_items(mnl).items()])
        r601 = business_rows(mnl).get("ATFIX601") or {}
        ok("nolist amex receipts only from the map", mnl["summary"]["amex_receipts_found"] == 1
           and r601.get("receipt") is False and r601.get("archive_id") is None, (mnl["summary"]["amex_receipts_found"], r601))
        ok("nolist archive-review absent", not todo_by_key(mnl, "archive-review"), [t["key"] for t in mnl["todo"]])

    # --- overrides absent, then every ref answered plus a June plan ---
    no = make_root(work, "nooverrides", second_export=True, overrides=None, with_archive=True)
    mno = build(no, TODAY)
    if mno is not None:
        check_amex(mno, "nooverrides", open_refs={"ATFIX604", "ATFIX703"})
        t = todo_by_key(mno, "amex-tags")
        ok("nooverrides amex-tags count", len(t) == 1 and t[0]["count"] == 2, t)
    al = make_root(work, "answered", second_export=True, overrides=("ATFIX604", "ATFIX703"), june_plan=True)
    mal = build(al, TODAY)
    if mal is not None:
        check_amex(mal, "answered", open_refs=set(), june_plan=True)
        ok("answered no amex-tags todo", not todo_by_key(mal, "amex-tags"), [t["key"] for t in mal["todo"]])
        ok("answered no fortnox-plan-missing", not todo_starting(mal, "fortnox-plan-missing"), [t["key"] for t in mal["todo"]])
        f6 = todo_by_key(mal, "fortnox-execute:2026-06")
        ok("answered fortnox-execute june", len(f6) == 1 and f6[0]["count"] == 1 and near(f6[0]["amount_sek"], 1599.0)
           and "fortnox-run-2026-06" in (f6[0]["command"] or ""), f6)
        ok("answered fortnox-execute july still there", len(todo_by_key(mal, "fortnox-execute:2026-07")) == 1, [t["key"] for t in mal["todo"]])
        ok("answered fortnox_runs detail names both months in Swedish", "juni 2026" in mal["sources"]["fortnox_runs"]["detail"]
           and "juli 2026" in mal["sources"]["fortnox_runs"]["detail"], mal["sources"]["fortnox_runs"])
        h2 = call("render answered raises", dashboard_html.render, mal)
        if isinstance(h2, str):
            ok("render answered stage labels", STAGE_LABELS["partial"] in h2 and STAGE_LABELS["planned"] in h2 and no_dashes(h2))
            ok("render answered shows the excluded row as critical",
               '<span class="chip crit">utanför planen: bas_account saknas</span>' in h2 and "utanför planen" in h2)

    # --- the August CSV has arrived but the ledger has not been rebuilt ---
    cov = make_root(work, "covered", second_export=True, amex_max="08/31/2026", with_archive=False)
    mcov = build(cov, TODAY)
    if mcov is not None:
        ok("covered coverage end", mcov["amex"]["coverage_end"] == "2026-08-31", mcov["amex"]["coverage_end"])
        ok("covered next month from the ledger", mcov["amex"]["next_month"] == "2026-08" and mcov["amex"]["next_month_csv_present"] is True,
           (mcov["amex"]["next_month"], mcov["amex"]["next_month_csv_present"]))
        ok("covered summary", mcov["summary"]["next_amex_month"] == "2026-08" and mcov["summary"]["next_amex_csv_present"] is True,
           (mcov["summary"]["next_amex_month"], mcov["summary"]["next_amex_csv_present"]))
        ok("covered no amex-csv todo", not todo_by_key(mcov, "amex-csv"), [t["key"] for t in mcov["todo"]])
        ok("covered amex_csv source ok", mcov["sources"]["amex_csv"]["state"] == "ok" and mcov["sources"]["amex_csv"]["date"] == "2026-08-31",
           mcov["sources"]["amex_csv"])

    # --- no activity csv: coverage falls back to the ledger ---
    if mf is not None:
        shutil.rmtree(os.path.join(full, "data", "amex"))
        mnc = build(full, TODAY)
        if mnc is not None:
            ok("no csv coverage from the ledger", mnc["amex"]["coverage_end"] == "2026-08-02", mnc["amex"]["coverage_end"])
            ok("no csv source missing", mnc["sources"]["amex_csv"]["state"] == "missing", mnc["sources"]["amex_csv"])
            ok("no csv amex-csv todo still raised", len(todo_by_key(mnc, "amex-csv")) == 1, [t["key"] for t in mnc["todo"]])
        write_activity(full, "08/02/2026")

    # --- tamper with the archive: an error, an orphan file and a stale index ---
    if mf is not None:
        primary = os.path.join(full, "archive", "viseo", "2026", P1 + ".pdf")
        with open(primary, "wb") as fh:
            fh.write(b"%PDF-1.4\ntampered\n")
        receipt_file(os.path.join(full, "archive", "viseo", "2026", "orphan-file.pdf"), "orphan")
        page = os.path.join(full, "archive", "viseo", "2026", P2 + ".md")
        os.utime(page, None)
        idx = os.path.join(full, "archive", "index.md")
        set_mtime(idx, 2026, 9, 1)
        mt = build(full, TODAY)
        if mt is not None:
            ok("tamper archive error reported", len(mt["archive"]["errors"]) == 1 and "sha256" in mt["archive"]["errors"][0]
               and P1 in mt["archive"]["errors"][0], mt["archive"]["errors"])
            ok("tamper summary errors", mt["summary"]["archive_errors"] == 1, mt["summary"]["archive_errors"])
            ok("tamper archive source stale", mt["sources"]["archive"]["state"] == "stale", mt["sources"]["archive"])
            e = todo_by_key(mt, "archive-errors")
            ok("tamper archive-errors todo", len(e) == 1 and e[0]["severity"] == "crit" and e[0]["owner"] == "systemet" and e[0]["count"] == 1, e)
            ok("tamper archive-errors first", mt["todo"] and mt["todo"][0]["severity"] == "crit", mt["todo"] and mt["todo"][0])
            kinds = warning_kinds(mt)
            ok("tamper orphan-file kind", kinds.get("orphan-file") is not None and kinds["orphan-file"]["count"] == 1
               and kinds["orphan-file"]["label"] == WARNING_LABELS["orphan-file"], kinds.get("orphan-file"))
            ok("tamper index-stale kind", kinds.get("index-stale") is not None and kinds["index-stale"]["count"] == 1
               and kinds["index-stale"]["label"] == WARNING_LABELS["index-stale"], kinds.get("index-stale"))
            ok("tamper warnings_total", mt["archive"]["warnings_total"] == 7 and mt["summary"]["archive_warnings"] == 7,
               (mt["archive"]["warnings_total"], mt["summary"]["archive_warnings"]))
            ok("tamper index updated stamp still read", TS_RE.match(str(mt["archive"]["updated"])) is not None, mt["archive"]["updated"])
            h3 = call("render tampered raises", dashboard_html.render, mt)
            if isinstance(h3, str):
                ok("render tampered shows the error", "sha256" in h3 and WARNING_LABELS["orphan-file"] in h3 and no_dashes(h3))
        # restore for the CLI runs
        receipt_file(primary, ARCHIVE_PAGES[0][2])
        os.remove(os.path.join(full, "archive", "viseo", "2026", "orphan-file.pdf"))
        os.utime(idx, None)

    # --- empty root through the library and the CLI ---
    empty = os.path.join(work, "empty")
    os.makedirs(empty)
    me = build(empty, TODAY)
    if me is not None:
        check_shape(me, "empty")
        check_empty_model(me, "empty")
        he = call("render empty raises", dashboard_html.render, me)
        check_render_empty(he, "render empty", me)
    ok("library build leaves the empty root empty", os.listdir(empty) == [], os.listdir(empty))
    if mf is not None:
        check_cli(work, full, empty, mf)

    n_fail = sum(not c for _, c, _ in checks)
    if n_fail:
        print(f"\ntest_dashboard: {len(checks)} checks, {n_fail} failed (work dir kept: {work})")
        sys.exit(1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"test_dashboard: {len(checks)} checks, 0 failed")


if __name__ == "__main__":
    main()
