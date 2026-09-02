#!/usr/bin/env python3
"""Offline checks for scripts/archive.py (the receipt archive CLI and library).

Every fixture is invented for this suite: vendors like TESTVENDOR AB, receipt
files that are a few fake bytes, a synthetic Pleo export folder, a synthetic
MATCHING.csv with an html sibling, a synthetic Amex ledger with an
amex_receipts matches file. The only real inputs are scripts/vendor_aliases.json
and scripts/merchant_rules.json, read for vendor canonicalisation (Wincher and
Spotify must resolve). Covers the frontmatter emitter and parser round trip
with tricky strings, parse_amount and sv_amount, stem slugging and collision
suffixes, merchant cleaning (processor prefixes, statement codes, domain
suffixes) and the amount tie-break between alias vendors, the three ingest
subcommands (each re-run must be a no-op, the Pleo export and the matching
ingest must agree on pleo_status whichever runs last, a second expense on the
same file and charge is refused, business Amex rows without a file and the
ledger's foreign amount are reported), add and set through both the CLI and
the library, index, find, check with a tampered primary file, --dry-run writing
nothing, the VAT-number label forms, receipt-date labels (forwarded-mail
headers and shipping estimates skipped), the buyer note for receipts made out
to another own company, the plausibility warnings check and the ingest report
print, the text-fit rule that gives a receipt file shared by two rows to the
charge the text shows (real run and dry run), merge re-extraction only on new
files or --retext with the preview recorded, the entity guard on the library
API, clean CLI errors for bad set keys and a non-numeric find amount, and the
receipt_text helper output parser with U+2028 in the text. Everything goes to a
fresh temp archive root that is removed on success and kept on failure; out/,
data/ and the real archive root are never touched. No network, no Gmail, no
Pleo, no Fortnox. The CLI runs with --no-text except the matching ingest, whose
html sibling is parsed in pure python; the library add uses a stub extractor.

Run: python3 scripts/tests/test_archive.py   (exit 0 = every check passed)
"""
import csv
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
SCRIPT = os.path.join(SCRIPTS, "archive.py")
REAL_ROOT = os.path.join(REPO, "archive")
sys.path.insert(0, SCRIPTS)
try:
    import archive
except Exception as exc:  # the suite cannot run without the module under test
    print(f"FAIL import scripts/archive.py: {exc!r}")
    sys.exit(1)

EM_DASH = "\u2014"
EN_DASH = "\u2013"
NBSP = "\u00a0"
TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
REQUIRED = ["type", "title", "id", "entity", "vendor", "merchant", "date", "amount_sek",
            "currency", "amount", "kind", "card", "lane", "source_kind", "files", "sha256",
            "text_method", "tags", "created", "updated"]
KEY_ORDER = ["type", "title", "id", "entity", "vendor", "merchant", "date", "receipt_date",
             "amount_sek", "currency", "amount", "vat_sek", "vat_rate", "kind", "card", "lane",
             "invoice_number", "vendor_vat_number", "vendor_country", "pleo_expense_id",
             "pleo_receipt_number", "pleo_status", "pleo_category", "amex_ref", "fortnox_voucher",
             "fortnox_status", "bas_account", "vat_regime", "source_kind", "source_ref",
             "source_from", "source_subject", "source_url", "files", "preview", "sha256",
             "text_method", "text_truncated", "note", "tags", "created", "updated"]
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
MINUS = "\u2212"
EXP1 = "11111111-1111-1111-1111-111111111111"
EXP2 = "22222222-2222-2222-2222-222222222222"
EXP3 = "33333333-3333-3333-3333-333333333333"
EXP4 = "44444444-4444-4444-4444-444444444444"
EXP5 = "55555555-5555-5555-5555-555555555555"
EXP6 = "66666666-6666-6666-6666-666666666666"
EXP7 = "77777777-7777-7777-7777-777777777777"
EXP8 = "88888888-8888-8888-8888-888888888888"
EXP9 = "99999999-9999-9999-9999-999999999999"

TRICKY = {
    "type": "receipt",
    "title": 'Kolon: "citat" \\ bakstreck',
    "id": "007",
    "entity": "viseo",
    "vendor": "Café Åäö & Co",
    "merchant": "",
    "date": "2026-07-10",
    "amount_sek": 3570.54,
    "currency": "SEK",
    "amount": 3570.54,
    "vat_rate": 25,
    "kind": "receipt",
    "card": "amex-1022",
    "lane": "amex-fortnox",
    "pleo_receipt_number": "2600491",
    "pleo_status": "yes",
    "amex_ref": "null",
    "source_kind": "gmail",
    "source_subject": "Your receipt #42 [paid] {ok} 'single' @home 100% a|b > c ! *",
    "files": ["2026-07-10-x-3570-54.pdf", "a, b.pdf"],
    "sha256": "0" * 64,
    "text_method": "none",
    "text_truncated": True,
    "note": " leading and trailing ",
    "tags": ["receipt", "viseo", "a, b", "2026-07", "true", "12"],
    "created": "2026-09-01T10:00:00Z",
    "updated": "2026-09-01T10:00:00Z",
    "custom_key": "kept",
}
BLOCK_PAGE = ("---\ntype: receipt\ntitle: 'single quoted'\ntags:\n  - receipt\n  - viseo\n"
              "vendor: ~\nnote: null\ntext_truncated: true\nflag: false\namount_sek: 129.0\n"
              "extra_key: kept\n---\n\n# Rubrik\n\ntext\n")
HTML_RECEIPT = ("<!DOCTYPE html>\n<html><head><title>Receipt</title>"
                "<style>body { color: #000; }</style></head>\n<body>\n"
                "<h1>Receipt from Testvendor</h1>\n<p>Date: August 17, 2026</p>\n"
                "<p>Invoice number INV-2026-0042</p>\n<p>Total: 981.09 SEK</p>\n"
                "<script>var hidden = 1;</script>\n</body></html>\n")
STUB_TEXT = ("Wincher" + NBSP + "International AB   \n"
             "Date: July 10, 2026\n"
             "Invoice number WI-2026-0710\n"
             "Customer VAT number SE556840887501\n"
             "VAT number EU826012345\n"
             "Total EUR 320.00\n"
             "The VAT amount converted to SEK is: 699.33\n"
             "\n\n\n\n"
             "Slut")
checks = []


def ok(name, cond, detail=""):
    checks.append((name, cond, detail))
    if not cond:
        print(f"FAIL {name} {detail}")


def sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def fake(path, kind, tag, size=None):
    """Write a small fake receipt file with unique bytes; pad to size when given."""
    if kind == "pdf":
        data = b"%PDF-1.4\n% fake receipt " + tag.encode() + b"\n"
    elif kind == "png":
        data = b"\x89PNG\r\n\x1a\n fake " + tag.encode() + b"\n"
    else:
        data = b"\xff\xd8\xff fake " + tag.encode()
    if size and len(data) < size:
        data += b"\0" * (size - len(data))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


def snapshot(path):
    """rel path -> (size, mtime_ns) for every file under path; None when path is absent."""
    if not os.path.isdir(path):
        return None
    out = {}
    for base, _, files in os.walk(path):
        for f in files:
            p = os.path.join(base, f)
            st = os.stat(p)
            out[os.path.relpath(p, path)] = (st.st_size, st.st_mtime_ns)
    return out


def content_snapshot(path):
    out = {}
    for base, _, files in os.walk(path):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, path)] = sha(p)
    return out


def run(root, *args, text=False):
    cmd = [sys.executable, SCRIPT, "--root", root]
    if not text:
        cmd.append("--no-text")
    cmd.extend(args)
    env = dict(os.environ, RECEIPTS_ARCHIVE=root)
    return subprocess.run(cmd, capture_output=True, text=True, env=env)


def read_page(root, rel):
    """(frontmatter, body, raw text) of a page, or (None, '', '') when missing."""
    path = os.path.join(root, rel)
    if not os.path.exists(path):
        return None, "", ""
    with open(path, encoding="utf-8") as fh:
        txt = fh.read()
    fm, body = archive.fm_load(txt)
    return fm, body, txt


def kvittotext(body):
    return body.split("## Kvittotext", 1)[1] if "## Kvittotext" in body else ""


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def near(a, b, tol=0.005):
    return num(a) is not None and abs(num(a) - b) < tol


def lines(s):
    return [ln for ln in s.splitlines() if ln.strip()]


def pages(root):
    out = []
    for base, _, files in os.walk(root):
        for f in files:
            if f.endswith(".md") and f not in ("index.md", "README.md"):
                out.append(os.path.join(base, f))
    return sorted(out)


def stub_extract(path, preview_path=None, max_pages=3, **kw):
    return {"text": STUB_TEXT, "method": "pdfkit", "pages": 1, "preview": False, "error": None}


def call(name, fn, *args, **kw):
    """Run a library call; an exception becomes a failed check instead of aborting the suite."""
    try:
        return fn(*args, **kw)
    except Exception as exc:
        ok(name, False, f"{type(exc).__name__}: {exc}")
        return None


def write_pleo_fixture(base):
    """expenses_2026-08-21 with 6 rows: single file, a/b files, no file (merchant with an em
    dash), reimbursement, and two expenses whose receipt files are byte-identical."""
    d = os.path.join(base, "expenses_2026-08-21")
    rdir = os.path.join(d, "receipts")
    os.makedirs(rdir)

    def row(**kw):
        r = {c: "" for c in PLEO_COLUMNS}
        r.update(kw)
        return [r[c] for c in PLEO_COLUMNS]

    rows = [
        row(**{"Date": "10-07-2026", "Receipt": "2600101", "Expense Type": "Card Purchase",
               "Amount": MINUS + "250,00", "Net Amount": MINUS + "200,00", "Currency": "SEK",
               "Orig. amount": "250,00", "Orig. currency": "SEK", "Source description": "TESTVENDOR AB",
               "Category": "Programvaror", "Account number": "5420", "Owner": "Test Person",
               "Receipt urls": "https://app.pleo.io:443/accounting-entries/aaaa/receipts/bbbb/file",
               "Review Status": "No review required", "Tax Code": "-", "Tax Rate": "0,25000",
               "Tax Amount": "50,00", "Merchant Country Code": "SE", "Expense ID": EXP1,
               "Export Status": "NOT_EXPORTED"}),
        row(**{"Date": "17-07-2026", "Receipt": "2600102", "Expense Type": "Card Purchase",
               "Amount": MINUS + "981,09", "Net Amount": MINUS + "981,09", "Total FX Fee": "19,15",
               "Currency": "SEK", "Orig. amount": "99,00", "Orig. currency": "USD",
               "Source description": "TESTCLOUD INC", "Category": "Hosting", "Account number": "6540",
               "Owner": "Test Person",
               "Receipt urls": "https://app.pleo.io:443/accounting-entries/cccc/receipts/dddd/file",
               "Tax Code": "-", "Tax Rate": "0,00000", "Tax Amount": "0,00", "Merchant Country Code": "US",
               "Expense ID": EXP2, "Export Status": "EXPORTED"}),
        row(**{"Date": "20-08-2026", "Receipt": "2600103", "Expense Type": "Card Purchase",
               "Amount": MINUS + "123,75", "Net Amount": MINUS + "99,00", "Currency": "SEK",
               "Orig. amount": "123,75", "Orig. currency": "SEK", "Source description": "TESTPOST " + EM_DASH + " AB",
               "Category": "Administration", "Account number": "6570", "Owner": "Test Person",
               "Tax Code": "-", "Tax Rate": "0,25000", "Tax Amount": "24,75", "Merchant Country Code": "SE",
               "Expense ID": EXP3, "Export Status": "NOT_EXPORTED"}),
        row(**{"Date": "21-08-2026", "Receipt": "2600104", "Expense Type": "Reimbursement to Test Person",
               "Amount": MINUS + "700,00", "Net Amount": MINUS + "700,00", "Currency": "SEK",
               "Orig. amount": "700,00", "Orig. currency": "SEK", "Owner": "Test Person",
               "Tax Amount": "0,00", "Expense ID": EXP4, "Export Status": "EXPORTED"}),
        row(**{"Date": "20-07-2026", "Receipt": "2600105", "Expense Type": "Card Purchase",
               "Amount": MINUS + "45,00", "Net Amount": MINUS + "36,00", "Currency": "SEK",
               "Orig. amount": "45,00", "Orig. currency": "SEK", "Source description": "TESTDUBBELKVITTO AB",
               "Category": "Programvaror", "Account number": "5420", "Owner": "Test Person",
               "Tax Rate": "0,25000", "Tax Amount": "9,00", "Merchant Country Code": "SE", "Expense ID": EXP8,
               "Export Status": "QUEUED"}),
        row(**{"Date": "20-07-2026", "Receipt": "2600106", "Expense Type": "Card Purchase",
               "Amount": MINUS + "45,00", "Net Amount": MINUS + "36,00", "Currency": "SEK",
               "Orig. amount": "45,00", "Orig. currency": "SEK", "Source description": "TESTDUBBELKVITTO AB",
               "Category": "Programvaror", "Account number": "5420", "Owner": "Test Person",
               "Tax Rate": "0,25000", "Tax Amount": "9,00", "Merchant Country Code": "SE", "Expense ID": EXP9,
               "Export Status": "QUEUED"}),
    ]
    with open(os.path.join(d, "export_test.csv"), "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(PLEO_COLUMNS)
        w.writerows(rows)
    fake(os.path.join(rdir, "2600101.pdf"), "pdf", "pleo-2600101")
    fake(os.path.join(rdir, "2600102a.pdf"), "pdf", "pleo-2600102a")
    fake(os.path.join(rdir, "2600102b.pdf"), "pdf", "pleo-2600102b")
    fake(os.path.join(rdir, "2600104.pdf"), "pdf", "pleo-2600104-reimbursement")
    fake(os.path.join(rdir, "2600105.pdf"), "pdf", "pleo-2600105-shared")
    fake(os.path.join(rdir, "2600106.pdf"), "pdf", "pleo-2600105-shared")
    return d


def write_matching_fixture(base):
    """MATCHING.csv: html-sibling row, new1 row, file-less row, second-source row for pleo 2600101."""
    fdir = os.path.join(base, "files")
    os.makedirs(fdir)
    fake(os.path.join(fdir, "TESTMATCH1_testvendor.pdf"), "pdf", "match-1-invoice")
    fake(os.path.join(fdir, "TESTMATCH1_testvendor_receipt.pdf"), "pdf", "match-1-receipt")
    with open(os.path.join(fdir, "TESTMATCH1_testvendor.html"), "w", encoding="utf-8") as fh:
        fh.write(HTML_RECEIPT)
    fake(os.path.join(fdir, "TESTMATCH2_testkafe.pdf"), "pdf", "match-2")
    fake(os.path.join(fdir, "TESTMATCH4_testvendor.pdf"), "pdf", "match-4-second-source")
    path = os.path.join(base, "MATCHING.csv")
    with open(path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["pleo_receipt_no", "expense_id", "date", "merchant", "pleo_amount", "file_to_attach",
                    "extra_file", "gmail_message_id", "status"])
        w.writerow(["2600201", EXP5, "2026-08-17", "TESTVENDOR AB", "981,09 SEK (99 USD)",
                    "TESTMATCH1_testvendor.pdf", "TESTMATCH1_testvendor_receipt.pdf", "1a00b4197fake001",
                    "attached 2026-08-24"])
        w.writerow(["new1", EXP6, "2026-08-18", "TESTKAFE", "129,00 SEK", "TESTMATCH2_testkafe.pdf", "",
                    "1a00b4197fake002", "attached 2026-08-24"])
        w.writerow(["2600203", EXP7, "2026-08-19", "TESTPOST AB", "123,75 SEK", "", "", "", "portal"])
        w.writerow(["2600101", EXP1, "2026-07-10", "TESTVENDOR AB", "250,00 SEK", "TESTMATCH4_testvendor.pdf",
                    "", "1a00b4197fake004", "attached 2026-08-24"])
    return path, fdir


def write_amex_fixture(base):
    """Ledger in classify.py CSV shape, receipts dir with a junk jpg, one matches JSON."""
    rdir = os.path.join(base, "receipts")
    os.makedirs(rdir)
    rows = [
        ["2026-06-05", "1022", "-61022", "1599.00", "TESTSAAS LTD", "business", "merchant rule", "", "", "", "",
         "Foreign Spend Amount: 149.00 UNITED STATES DOLLAR Commission Amount: 30,00 Currency Exchange Rate: 0.1",
         "ATFIX101", "activity_fixture.csv", "viseo", "6540", "noneu_rc", "IT-tjanster"],
        ["2026-06-12", "2004", "-62004", "59.00", "TESTKAFE", "personal", "card default", "", "", "", "",
         "", "ATFIX102", "activity_fixture.csv", "viseo", "", "", ""],
        ["2026-06-20", "1022", "-61022", "250,00", "TESTVENDOR AB", "business", "merchant rule", "", "", "", "",
         "", "ATFIX103", "activity_fixture.csv", "5555media", "6540", "domestic25", "IT-tjanster"],
        ["2026-06-25", "1022", "-61022", "100.00", "TESTVENDOR AB", "business", "merchant rule", "", "", "", "",
         "", "ATFIX104", "activity_fixture.csv", "viseo", "6540", "domestic25", "IT-tjanster"],
        ["2026-06-28", "1022", "-61022", "80.00", "TESTKAFE", "needs-tag", "queued", "", "", "", "",
         "", "ATFIX105", "activity_fixture.csv", "viseo", "", "", ""],
    ]
    ledger = os.path.join(base, "ledger.csv")
    with open(ledger, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(LEDGER_COLUMNS)
        w.writerows(rows)
    fake(os.path.join(rdir, "ATFIX101_invoice.pdf"), "pdf", "amex-101-invoice")
    fake(os.path.join(rdir, "ATFIX101_1.jpg"), "jpg", "amex-101-junk", size=100)
    fake(os.path.join(rdir, "ATFIX101_2.png"), "png", "amex-101-screenshot", size=6000)
    fake(os.path.join(rdir, "ATFIX102_receipt.pdf"), "pdf", "amex-102-personal")
    fake(os.path.join(rdir, "ATFIX103_receipt.png"), "png", "amex-103-photo", size=6000)
    fake(os.path.join(rdir, "ATFIX105_receipt.pdf"), "pdf", "amex-105-needs-tag")
    fake(os.path.join(rdir, "ATFIX999_orphan.pdf"), "pdf", "amex-999-orphan")
    mid = "19e1207bfake0101"
    matches = {"run": {"month": "2026-06", "fixture": True},
               "rows": [{"row_key": "ATFIX101", "ledger": "amex", "date": "2026-06-05",
                         "merchant": "TESTSAAS LTD", "amount_sek": 1599.0, "tag": "business",
                         "entity": "viseo", "known_message_id": mid, "best": mid,
                         "subject": "Your receipt from Testsaas", "from": "Testsaas <billing@testsaas.example>",
                         "candidates": [{"message_id": mid, "subject": "Your receipt from Testsaas",
                                         "from": "Testsaas <billing@testsaas.example>"}],
                         "route": "attach", "confidence": "strong"},
                        {"row_key": "ATFIX103", "ledger": "amex", "date": "2026-06-20",
                         "merchant": "TESTVENDOR AB", "amount_sek": 250.0, "tag": "business",
                         "entity": "5555media", "known_message_id": "", "best": None, "candidates": [],
                         "route": "none", "confidence": "none"}]}
    mpath = os.path.join(base, "matches-2026-06.json")
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(matches, fh, ensure_ascii=False, indent=1)
    return ledger, rdir, mpath, mid


def check_frontmatter_roundtrip():
    dump = archive.fm_dump(TRICKY)
    ok("fm_dump opens and closes with ---", dump.startswith("---\n") and "\n---" in dump[4:], dump[:60])
    ok("fm_dump quotes number-like id", 'id: "007"' in dump, dump)
    ok("fm_dump quotes bool-like string", 'pleo_status: "yes"' in dump, dump)
    ok("fm_dump quotes null-like string", 'amex_ref: "null"' in dump, dump)
    ok("fm_dump quotes digit string", 'pleo_receipt_number: "2600491"' in dump, dump)
    ok("fm_dump plain float", "amount_sek: 3570.54\n" in dump, dump)
    ok("fm_dump plain int", "vat_rate: 25\n" in dump, dump)
    ok("fm_dump plain date", "date: 2026-07-10\n" in dump, dump)
    ok("fm_dump lowercase bool", "text_truncated: true\n" in dump, dump)
    ok("fm_dump inline lists", "tags: [" in dump and "files: [" in dump, dump)
    ok("fm_dump quotes list items with commas", '"a, b"' in dump, dump)
    ok("fm_dump quotes bool-like and number-like list items", '"true"' in dump and '"12"' in dump, dump)
    ok("fm_dump escapes backslash and quote in title",
       'title: "Kolon: \\"citat\\" \\\\ bakstreck"' in dump, dump)
    ok("fm_dump quotes leading and trailing space", 'note: " leading and trailing "' in dump, dump)
    ok("fm_dump keeps unicode", "Café Åäö" in dump, dump)
    positions = [dump.find("\n" + k + ":") for k in KEY_ORDER if k in TRICKY]
    ok("fm_dump key order", all(p >= 0 for p in positions) and positions == sorted(positions),
       [(k, dump.find("\n" + k + ":")) for k in KEY_ORDER if k in TRICKY])
    ok("fm_dump has no dash characters", EM_DASH not in dump and EN_DASH not in dump)

    loaded, body = archive.fm_load(dump)
    ok("fm round trip equal", loaded == TRICKY,
       {k: (TRICKY.get(k), loaded.get(k)) for k in set(TRICKY) | set(loaded) if TRICKY.get(k) != loaded.get(k)})
    ok("fm round trip types", all(type(loaded.get(k)) is type(v) for k, v in TRICKY.items()),
       {k: (type(v).__name__, type(loaded.get(k)).__name__) for k, v in TRICKY.items()
        if type(loaded.get(k)) is not type(v)})
    ok("fm round trip keeps unknown key", loaded.get("custom_key") == "kept", loaded.get("custom_key"))
    ok("fm round trip empty body", body.strip() == "", repr(body))

    fm, body = archive.fm_load(BLOCK_PAGE)
    ok("fm_load single quoted", fm.get("title") == "single quoted", fm.get("title"))
    ok("fm_load block list", fm.get("tags") == ["receipt", "viseo"], fm.get("tags"))
    ok("fm_load tilde is None", "vendor" in fm and fm["vendor"] is None, fm.get("vendor"))
    ok("fm_load null is None", "note" in fm and fm["note"] is None, fm.get("note"))
    ok("fm_load true/false", fm.get("text_truncated") is True and fm.get("flag") is False,
       (fm.get("text_truncated"), fm.get("flag")))
    ok("fm_load float", fm.get("amount_sek") == 129.0, fm.get("amount_sek"))
    ok("fm_load unknown key kept", fm.get("extra_key") == "kept", fm.get("extra_key"))
    ok("fm_load body", body.strip() == "# Rubrik\n\ntext", repr(body))


def check_amounts_and_vendors():
    for raw, want in [(MINUS + "1 234,56", -1234.56), ("1" + NBSP + "234,56", 1234.56), ("1 234,56", 1234.56),
                      ("-1234.56", -1234.56), ("123,75 SEK", 123.75), ("3570.54", 3570.54),
                      (MINUS + "981,09", -981.09), ("99", 99.0)]:
        got = archive.parse_amount(raw)
        ok(f"parse_amount {raw!r}", got is not None and abs(got - want) < 1e-9, got)
    for args, want in [((3570.54,), "3 570,54 SEK"), ((129,), "129,00 SEK"), ((99, "USD"), "99,00 USD"),
                       ((1234567.5,), "1 234 567,50 SEK"), ((0.5,), "0,50 SEK")]:
        got = archive.sv_amount(*args)
        ok(f"sv_amount {args}", got == want, got)
    ok("sv_amount cur keyword", archive.sv_amount(981.09, cur="USD") == "981,09 USD",
       archive.sv_amount(981.09, cur="USD"))
    for raw, want in [(".5", 0.5), ("-.5", -0.5), ("\u20111", -1.0), (",5", 0.5)]:
        got = archive.parse_amount(raw)
        ok(f"parse_amount leading point {raw!r}", got is not None and abs(got - want) < 1e-9, got)
    ok("parse_amount rejects scientific notation", archive.parse_amount("1e5") is None, archive.parse_amount("1e5"))
    ok("plain_dashes nbsp to space", archive.plain_dashes("Foo" + NBSP + "Bar") == "Foo Bar",
       repr(archive.plain_dashes("Foo" + NBSP + "Bar")))
    ok("plain_dashes em dash", archive.plain_dashes("FOO " + EM_DASH + " BAR") == "FOO - BAR",
       archive.plain_dashes("FOO " + EM_DASH + " BAR"))
    for merchant, want in [("1PASSWORD* TRIAL OVER", "1password"), ("PADDLE.NET* BARTENDER LISBOA", "Bartender"),
                           ("FS *typora.io", "Typora"), ("Zettle_*Rocoto Import", "Rocoto Import"),
                           ("SVEA*PROFFSMAGASIN", "Proffsmagasin"), ("DISCORD* NITROYEARLY", "Discord"),
                           ("Dropbox*8SNF1BW4CHHL", "Dropbox"), ("INCOGNI* INCOGNI.CO", "Incogni"),
                           ("AMAZONRETAIL*N61QQ6964 WWW.AMAZON.SE", "Amazonretail")]:
        got = archive.resolve_vendor({"merchant": merchant})[0]
        ok(f"vendor name for {merchant!r}", got == want, got)
    ok("statement code rule", archive.is_statement_code("N61QQ6964") and archive.is_statement_code("8SNF1BW4CHHL")
       and archive.is_statement_code("RXVD9D") and not archive.is_statement_code("1PASSWORD")
       and not archive.is_statement_code("TESTVENDOR"))
    res = archive.vendor_for("ANTHROPIC* CLAUDE SUB", 22.5, "EUR")
    ok("vendor_for amount tie-break Pro", res == ("Anthropic Claude Pro", "anthropic-pro"), res)
    res = archive.vendor_for("ANTHROPIC* CLAUDE SUB", 180, "EUR")
    ok("vendor_for amount tie-break Max", res == ("Anthropic Claude Max", "anthropic-max"), res)
    res = archive.vendor_for("ANTHROPIC* CLAUDE SUB", 999, "EUR")
    ok("vendor_for unknown amount keeps file order", res[0] == "Anthropic Claude Max", res)
    res = archive.foreign_amount("Foreign Spend Amount: 6.926,00 EUROPEAN UNION EURO Commission Amount: 1537,84 Currency Exchange Rate: 0.09")
    ok("foreign_amount euro", res == (6926.0, "EUR"), res)
    res = archive.foreign_amount("Foreign Spend Amount: 240.00 UNITED STATES DOLLAR Commission Amount: 46,11")
    ok("foreign_amount usd", res == (240.0, "USD"), res)
    ok("foreign_amount absent", archive.foreign_amount("Sixt9523521991 60610XVC4KKWCKNC8L") is None)

    res = archive.vendor_for("WINCHERINTERNATIONALCOM STOCKHOLM")
    ok("vendor_for returns a pair", isinstance(res, tuple) and len(res) == 2, res)
    ok("vendor_for Wincher alias", res == ("Wincher", "wincher"), res)
    ok("vendor_for case and whitespace insensitive",
       archive.vendor_for("  wincher   international  ")[0] == "Wincher",
       archive.vendor_for("  wincher   international  "))
    res = archive.vendor_for("TESTVENDOR AB STOCKHOLM")
    ok("vendor_for unknown merchant", res[0] is None and res[1] == "testvendor", res)
    res = archive.vendor_for("CAFÉ ÅÄÖ AB STOCKHOLM")
    ok("vendor_for folds swedish letters", res[1] == "cafe-aao", res)
    res = archive.vendor_for("TESTVENDOR LONGNAME PRODUCTIONS UNLIMITED")
    ok("vendor_for caps slug at 24", len(res[1]) <= 24 and res[1].startswith("testvendor-longname")
       and not res[1].startswith("-") and not res[1].endswith("-"), res)
    ok("vendor_for all-stopword fallback", archive.vendor_for("AB STOCKHOLM")[1] == "okand",
       archive.vendor_for("AB STOCKHOLM"))
    ok("vendor_for star-only fallback", archive.vendor_for("***")[1] == "okand", archive.vendor_for("***"))
    res = archive.vendor_for("SPOTIFY TESTKOP")
    ok("vendor_for merchant_rules fallback", res == ("Spotify", "spotify"), res)
    res = archive.vendor_for("ANTHROPIC")
    ok("vendor_for merchant_exact", res[0] == "Anthropic API", res)
    res = archive.vendor_for("ANTHROPIC* CLAUDE SUB")
    ok("vendor_for substring beats exact on longer text", res[0] == "Anthropic Claude Max", res)
    res = archive.vendor_for("GOOGLE *Google One")
    ok("vendor_for merchant_regex", res[0] == "Google One (Google Play)", res)


def check_harvest_and_fit():
    """VAT-number label forms, slash dates, amount spotting, and a batch whose two rows share
    one receipt file: the row the text fits gets the page, the other is refused with a reason,
    the dry run predicts the same, and without text the batch order stands."""
    hv = archive.harvest_vendor_vat_number
    for name, text, want in [
        ("VAT Reg # :", "VAT Reg # : EU528377759  Payment Terms Due Upon Receipt", "EU528377759"),
        ("VAT-/momsnummer:", "Ireland\nVAT-/momsnummer: IE3668997OH\nFaktureringsadress", "IE3668997OH"),
        ("Momsreg.nummer next line", "172 27 Sundbyberg  Momsreg.nummer\nSE556563252701\nInnehar F-skattebevis", "SE556563252701"),
        ("VAT ID is", "For your records, Discord's VAT ID is EU528003307.", "EU528003307"),
        ("Momsregistreringsnummer:", "Momsregistreringsnummer: SE502052130701", "SE502052130701"),
        ("VAT number:", "VAT number: SE556908528401\nVAT number: SE556840887501", "SE556908528401"),
        ("own number skipped then vendor found", "SE VAT SE556840887501\nOpenAI VAT EU372041333", "EU372041333"),
        ("5555 Media line skipped", "5555 Media AB VAT number SE559912345601\nVAT number: DE123456789", "DE123456789"),
        ("needs a digit", "VAT EXEMPTION NOTE\nVAT REGISTRATION NUMBER", None),
        ("no label", "Org nr 556563-2527 SE556563252701", None),
    ]:
        ok("vat label " + name, hv(text) == want, hv(text))
    hd = archive.harvest_receipt_date
    ok("receipt_date slash iso", hd("Fakturanummer: 6391405923436639151 Datum: 2026/05/11", "2026-05-11") == "2026-05-11",
       hd("Fakturanummer: 6391405923436639151 Datum: 2026/05/11", "2026-05-11"))
    ok("receipt_date dotted iso", hd("Datum 2026.05.09", "2026-05-11") == "2026-05-09", hd("Datum 2026.05.09", "2026-05-11"))
    ok("receipt_date outside window", hd("Date paid March 14, 2026", "2026-05-14") is None)
    fwd = ("Med vanlig halsning\n---------- Forwarded message ---------\nFrom: Test <t@example.com>\n"
           "Date: Sat, Jan 24, 2026 at 5:02 PM\nSubject: Your receipt\nTo: x@example.com\n\n"
           "Order date: Jan 5, 2026\nTotal 129.99")
    ok("receipt_date skips forwarded header and prefers the label", hd(fwd, "2026-01-06") == "2026-01-05", hd(fwd, "2026-01-06"))
    ship = "Ordernummer: #123\nDin order beraknas att skickas 2026-03-30.\n"
    ok("receipt_date skips shipping estimate", hd(ship, "2026-04-14") is None, hd(ship, "2026-04-14"))
    ok("receipt_date label on next line", hd("Kvittosammanst\u00e4llningsdatum\n2026-01-31\nPeriod 2026-01-31 - 2026-02-28",
                                             "2026-02-01") == "2026-01-31")
    ok("receipt_date paid on", hd("$3.25 paid on June 1, 2026\nDate 2026-06-30", "2026-07-01") == "2026-06-01")
    ok("receipt_date plain first date", hd("Vercel Inc.\nJuly 10, 2026\nInvoice 1A2B", "2026-07-10") == "2026-07-10")
    hb = archive.harvest_buyer
    ok("buyer kopare", hb("Kvitto\nK\u00f6pare 5555 Media AB\nTotal") == ("5555 Media AB", "5555media"), hb("K\u00f6pare 5555 Media AB"))
    ok("buyer organization", hb("Hi 5555 Media,\nOrganization: 5555 Media (org-x)") == ("5555 Media AB", "5555media"))
    ok("buyer holding", hb("For 5555 Holding AB") == ("5555 Holding AB", None))
    ok("buyer viseo", hb("Hej Viseo AB!") == ("Viseo AB", "viseo"))
    ok("buyer none in signature", hb("Viseo - Ett enklare satt att fa fler kunder") is None)
    mp = archive.merge_pleo_status
    for old, new, want in [("attached 2026-08-24", "i Pleo (QUEUED)", "attached 2026-08-24 (QUEUED)"),
                           ("attached 2026-08-24 (QUEUED)", "i Pleo (QUEUED)", "attached 2026-08-24 (QUEUED)"),
                           ("attached 2026-08-24 (QUEUED)", "attached 2026-08-24", "attached 2026-08-24 (QUEUED)"),
                           ("i Pleo (QUEUED)", "attached 2026-08-24", "attached 2026-08-24 (QUEUED)"),
                           ("attached 2026-08-24 (QUEUED)", "i Pleo (EXPORTED)", "attached 2026-08-24 (EXPORTED)"),
                           ("i Pleo (QUEUED)", "i Pleo (EXPORTED)", "i Pleo (EXPORTED)"),
                           ("fetched", "i Pleo (QUEUED)", "i Pleo (QUEUED)"), ("", "attached 2026-08-24", "attached 2026-08-24"),
                           ("attached 2026-08-24 (QUEUED)", "", "attached 2026-08-24 (QUEUED)")]:
        ok(f"merge_pleo_status {old!r} + {new!r}", mp(old, new) == want, mp(old, new))
    ok("status sentence with export state",
       archive.status_sentence({"lane": "pleo", "pleo_status": "attached 2026-08-24 (QUEUED)"}) == "Bifogat i Pleo 2026-08-24 (QUEUED).",
       archive.status_sentence({"lane": "pleo", "pleo_status": "attached 2026-08-24 (QUEUED)"}))
    meta = {"date": "2026-07-16", "receipt_date": "2026-06-16", "invoice_number": "TEST-0010", "amount_sek": 712.73,
            "amount": 69.0, "currency": "USD", "text_method": "pdfkit", "entity": "viseo", "kind": "receipt",
            "vendor": "Testvendor", "merchant": "TESTVENDOR AB"}
    text = "Testvendor receipt\nInvoice TEST-0010\n$59.00 paid on June 16, 2026\nBill to 5555 Media AB"
    others = [("other-page", {"invoice_number": "TEST-0010", "date": "2026-06-16", "amount_sek": 693.7, "kind": "receipt"})]
    msgs = archive.plausibility(meta, text, others)
    ok("plausibility early receipt_date", any("30 days before" in m for m in msgs), msgs)
    ok("plausibility duplicate invoice", any("TEST-0010 is also on other-page" in m for m in msgs), msgs)
    ok("plausibility amount missing", any("712,73 SEK (69,00 USD) not found" in m for m in msgs), msgs)
    ok("plausibility buyer", any("5555 Media AB" in m and "Viseo AB" in m for m in msgs), msgs)
    ok("plausibility four warnings", len(msgs) == 4, msgs)
    good = dict(meta, receipt_date="2026-07-15")
    clean_text = "Testvendor\nInvoice TEST-0010\n$69.00 paid July 15, 2026"
    ok("plausibility clean", archive.plausibility(good, clean_text, []) == [], archive.plausibility(good, clean_text, []))
    late = archive.plausibility(dict(good, receipt_date="2026-07-31"), clean_text, [])
    ok("plausibility late receipt_date", late == ["receipt_date 2026-07-31 is 15 days after the charge date 2026-07-16; "
                                                   "the file may be a later period's receipt"], late)
    other_vendor = archive.plausibility(good, "Othervendor\nInvoice TEST-0010\n$69.00 paid July 15, 2026", [])
    ok("plausibility vendor not in text", other_vendor == ["vendor Testvendor not named in the receipt text; the file may "
                                                            "belong to another vendor"], other_vendor)
    credit = [("credit-page", {"invoice_number": "TEST-0010", "date": "2026-08-01", "amount_sek": 712.73, "kind": "credit-note"})]
    ok("plausibility credit note shares number", archive.plausibility(good, "", credit) == [], archive.plausibility(good, "", credit))
    ait = archive.amount_in_text
    ok("amount_in_text dotted", ait("Total $6.70 paid", 6.7))
    ok("amount_in_text thousands", ait("Summa 1 234,56 kr", 1234.56) and ait("Total 1,234.56", 1234.56))
    ok("amount_in_text whole amount", ait("Total: 20 USD", 20.0))
    ok("amount_in_text grouped whole amount", ait("Belopp: 1 000 kronor", 1000.0) and ait("Total 1,000", 1000.0)
       and ait("Total 1.000", 1000.0))
    ok("amount_in_text dot thousands comma decimals", ait("Total: 6.926,00 EUR", 6926.0))
    vit = archive.vendor_in_text
    ok("vendor_in_text compact match", vit({"vendor": "Wpengine Db Acf Plugin", "merchant": "WPENGINE DB"}, "Receipt from WP Engine, Inc.")
       and vit({"vendor": "Lovable", "merchant": "LOVABLE"}, "Thanks for using Lovable"))
    ok("vendor_in_text mismatch", not vit({"vendor": "Lovable", "merchant": "LOVABLE"}, "DigitalOcean invoice $79.64"))
    ok("vendor_in_text short names pass", vit({"vendor": "X", "merchant": "X"}, "anything"))
    ok("amount_in_text not inside a longer number", not ait("Invoice 2026-05-11 total 120.00", 20.0))
    ok("amount_in_text absent", not ait("Total $6.70", 5.01))

    work = tempfile.mkdtemp(prefix="test_archive_fit_")
    root = os.path.join(work, "root")
    shared = fake(os.path.join(work, "src", "shared.pdf"), "pdf", "shared-receipt")
    texts = {sha(shared): "Receipt\nInvoice number TESTAPI-0013\nDate paid May 12, 2026\n$6.70 paid on May 12, 2026\nTotal $6.70"}

    def fit_extract(path, preview_path=None, max_pages=3, **kw):
        return {"text": texts.get(sha(path), ""), "method": "pdfkit", "pages": 1, "preview": False, "error": None}

    def jobs():
        return [archive.job("late row", [shared], {"entity": "viseo", "date": "2026-05-29", "merchant": "TESTAPI",
                                                  "amount_sek": "47,80", "currency": "USD", "amount": "5,01",
                                                  "card": "pleo", "pleo_expense_id": "exp-late"}),
                archive.job("right row", [shared], {"entity": "viseo", "date": "2026-05-12", "merchant": "TESTAPI",
                                                   "amount_sek": "63,42", "currency": "USD", "amount": "6,70",
                                                   "card": "pleo", "pleo_expense_id": "exp-right"})]
    right_stem = archive.make_stem("2026-05-12", archive.vendor_for("TESTAPI")[1], 63.42)
    old_extractor = archive.EXTRACTOR
    archive.EXTRACTOR = fit_extract
    res = call("dry-run text fit raises", archive.run_jobs, archive.Archive(root, dry_run=True), jobs(), True)
    if res is not None:
        ok("dry-run text fit plans the fitting row", [lbl for lbl, _ in res["created"]] == ["right row"], res["created"])
        ok("dry-run text fit refuses the other row", len(res["skipped"]) == 1 and res["skipped"][0][0] == "late row"
           and f"same file as {right_stem}" in res["skipped"][0][1], res["skipped"])
    ok("dry-run text fit writes nothing", not os.path.exists(root))
    arc = archive.Archive(root)
    res = call("text fit raises", archive.run_jobs, arc, jobs(), True)
    if res is not None:
        ok("text fit creates the fitting row", [lbl for lbl, _ in res["created"]] == ["right row"], res["created"])
        ok("text fit refuses the other row", len(res["skipped"]) == 1 and res["skipped"][0][0] == "late row"
           and f"same file as {right_stem}" in res["skipped"][0][1], res["skipped"])
        ok("text fit page", pages(root) == [os.path.join(root, "viseo", "2026", right_stem + ".md")], pages(root))
    res = call("text fit re-run raises", archive.run_jobs, arc, jobs(), True)
    if res is not None:
        ok("text fit re-run unchanged", not res["created"] and not res["updated"] and len(res["unchanged"]) == 1
           and len(res["skipped"]) == 1, res)
    archive.EXTRACTOR = archive._no_text_extract
    res = call("no-text order raises", archive.run_jobs, archive.Archive(os.path.join(work, "root2")), jobs(), False)
    if res is not None:
        ok("no-text keeps batch order", [lbl for lbl, _ in res["created"]] == ["late row"], res)
    archive.EXTRACTOR = old_extractor
    shutil.rmtree(work, ignore_errors=True)


def check_receipt_text_helper():
    """receipt_text parses the helper's one JSON object whole: U+2028, U+2029 and U+0085
    inside the text (unescaped by JSONSerialization) must survive."""
    try:
        import receipt_text
    except Exception as exc:  # the wrapper must import without the Swift toolchain
        ok("receipt_text imports", False, repr(exc))
        return
    body = "Line one\u2028Line two\u2029Line three\u0085Line four"
    payload = json.dumps({"text": body, "method": "pdfkit", "pages": 1, "preview": False, "error": None}, ensure_ascii=False)
    ok("helper payload splits on the separators", len(payload.splitlines()) == 4, len(payload.splitlines()))

    class Proc:
        stdout = payload + "\n"
        stderr = ""
        returncode = 0

    work = tempfile.mkdtemp(prefix="test_archive_rt_")
    pdf = fake(os.path.join(work, "x.pdf"), "pdf", "helper-stub")
    old_run, old_ensure = receipt_text.subprocess.run, receipt_text.ensure_binary
    receipt_text.subprocess.run = lambda *a, **k: Proc()
    receipt_text.ensure_binary = lambda: None
    try:
        res = receipt_text.extract(pdf)
    finally:
        receipt_text.subprocess.run, receipt_text.ensure_binary = old_run, old_ensure
        shutil.rmtree(work, ignore_errors=True)
    ok("helper output with U+2028 parsed", res.get("method") == "pdfkit" and res.get("text") == body and res.get("error") is None, res)
    ok("helper output parser tolerates a leading line", receipt_text.parse_helper_output("noise\n" + payload).get("text") == body)
    cleaned, _ = archive.clean_text(body)
    ok("clean_text turns separators into newlines", cleaned == "Line one\nLine two\nLine three\nLine four", repr(cleaned))


def check_merge_and_plausibility():
    """Library-level merge rules in a separate root: no re-extraction without new files or
    retext, the preview recorded on retext, the entity guard, the buyer note, and the check
    warnings for an early receipt_date, a shared invoice number and a missing amount."""
    work = tempfile.mkdtemp(prefix="test_archive_merge_")
    root = os.path.join(work, "root")
    src = fake(os.path.join(work, "src", "blank.pdf"), "pdf", "merge-blank")
    meta = {"entity": "viseo", "date": "2026-07-23", "merchant": "TESTBLANK AB", "amount_sek": "20", "card": "pleo",
            "pleo_expense_id": "exp-blank"}
    stem = "2026-07-23-testblank-20-00"
    page = os.path.join(root, "viseo", "2026", stem + ".md")
    old_extractor = archive.EXTRACTOR
    calls = []

    def preview_extract(path, preview_path=None, max_pages=3, **kw):
        calls.append(os.path.basename(path))
        if preview_path:
            with open(preview_path, "wb") as fh:
                fh.write(b"\x89PNG fake preview")
        return {"text": "", "method": "none", "pages": 1, "preview": bool(preview_path), "error": "no text found"}

    def text_extract(path, preview_path=None, max_pages=3, **kw):
        calls.append(os.path.basename(path))
        return {"text": "TESTBLANK AB\nDatum 2026-07-23\nTotal 20,00 SEK", "method": "pdfkit", "pages": 1, "preview": False, "error": None}

    try:
        archive.EXTRACTOR = archive._no_text_extract
        rec = call("merge add raises", archive.Archive(root).add, [src], meta, text=False)
        ok("merge page created without text", rec is not None and rec.outcome == "created" and rec.get("text_method") == "none", rec)
        archive.EXTRACTOR = text_extract
        rec = call("merge re-add raises", archive.Archive(root).add, [src], meta)
        ok("merge without new files does not re-extract", rec is not None and rec.outcome == "unchanged" and not calls, (rec and rec.outcome, calls))
        archive.EXTRACTOR = preview_extract
        rec = call("retext add raises", archive.Archive(root, retext=True).add, [src], meta)
        fm, body, _ = read_page(root, os.path.relpath(page, root))
        ok("retext records the preview", rec is not None and rec.outcome == "updated" and "text" in getattr(rec, "changes", [])
           and fm is not None and fm.get("preview") == stem + "-preview.png" and "Forhandsvisning" in body,
           (rec and (rec.outcome, getattr(rec, "changes", None)), fm and fm.get("preview")))
        errs = call("retext check raises", archive.Archive(root).check)
        ok("retext leaves no orphan preview", errs is not None and not any("not referenced" in e for e in errs), errs)
        rec = call("retext re-add raises", archive.Archive(root, retext=True).add, [src], meta)
        ok("retext re-run unchanged", rec is not None and rec.outcome == "unchanged", rec and rec.outcome)
        html = os.path.join(work, "src", "blank.html")
        with open(html, "w", encoding="utf-8") as fh:
            fh.write("<html><body><p>TESTBLANK AB</p><p>Datum 2026-07-23</p><p>Total 20,00 SEK</p></body></html>")
        archive.EXTRACTOR = old_extractor
        rec = call("html extra add raises", archive.Archive(root).add, [src, html], meta)
        fm, body, _ = read_page(root, os.path.relpath(page, root))
        ok("new html file re-extracts", rec is not None and rec.outcome == "updated" and fm is not None
           and fm.get("text_method") == "html" and "Total 20,00 SEK" in kvittotext(body), (rec and rec.outcome, fm and fm.get("text_method")))
        rec = call("html extra re-add raises", archive.Archive(root).add, [src, html], meta)
        ok("html extra re-run unchanged", rec is not None and rec.outcome == "unchanged", rec and rec.outcome)
        other = fake(os.path.join(work, "src", "other.pdf"), "pdf", "merge-other")
        try:
            archive.Archive(root).add([other], dict(meta, entity="../outside"))
            ok("entity guard raises", False, "no ValueError")
        except ValueError as exc:
            ok("entity guard raises", "entity must be one of" in str(exc), str(exc))
        ok("entity guard writes nothing outside root", not os.path.exists(os.path.join(work, "outside")))

        def buyer_extract(path, preview_path=None, max_pages=3, **kw):
            return {"text": "Kvitto Testkivra\nK\u00f6pare 5555 Media AB\nDatum 2026-06-01\nTotal 100,00 SEK", "method": "pdfkit",
                    "pages": 1, "preview": False, "error": None}
        archive.EXTRACTOR = buyer_extract
        buyer_src = fake(os.path.join(work, "src", "buyer.pdf"), "pdf", "merge-buyer")
        rec = call("buyer add raises", archive.Archive(root).add, [buyer_src],
                   {"entity": "viseo", "date": "2026-06-01", "merchant": "TESTKIVRA", "amount_sek": "100", "card": "pleo"})
        ok("buyer note filled", rec is not None and rec.get("note") == "Kvittot ar stallt till 5555 Media AB", rec and rec.get("note"))
        errs = call("buyer check raises", archive.Archive(root).check)
        ok("check warns about the buyer", errs is not None and any("5555 Media AB" in e and e.startswith("warning: ") for e in errs), errs)
        archive.EXTRACTOR = text_extract
        early_src = fake(os.path.join(work, "src", "early.pdf"), "pdf", "merge-early")
        rec = call("early add raises", archive.Archive(root).add, [early_src],
                   {"entity": "viseo", "date": "2026-08-20", "merchant": "TESTBLANK AB", "amount_sek": "55", "card": "pleo",
                    "invoice_number": "TB-0001"})
        arc = archive.Archive(root)
        call("set receipt_date raises", arc.set, "2026-08-20-testblank-55-00", receipt_date="2026-07-23")
        call("set invoice raises", arc.set, stem, invoice_number="TB-0001")
        errs = call("plausibility check raises", archive.Archive(root).check)
        ok("check warns about early receipt_date", errs is not None and any("receipt_date 2026-07-23 is 28 days before" in e for e in errs), errs)
        ok("check warns about shared invoice number", errs is not None and any("invoice_number TB-0001 is also on" in e for e in errs), errs)
        ok("check warns about missing amount", errs is not None and any("55,00 SEK not found" in e for e in errs), errs)
        ok("check warnings are not errors", errs is not None and all(e.startswith("warning: ") for e in errs), errs)
        p = run(root, "check")
        ok("cli check exit 0 with warnings only", p.returncode == 0 and "receipt_date 2026-07-23" in p.stdout, (p.returncode, p.stdout))
    finally:
        archive.EXTRACTOR = old_extractor
    shutil.rmtree(work, ignore_errors=True)


def check_sync_fortnox(work):
    """sync-fortnox copies verifikat numbers from a plan's execution.json onto the pages
    with that amex_ref; set accepts source_kind from the known vocabulary only."""
    root = os.path.join(work, "archive-sync")
    src = os.path.join(work, "sync-src")
    os.makedirs(src, exist_ok=True)
    f1 = fake(os.path.join(src, "ATFIX901_receipt.pdf"), "pdf", "sync901")
    f2 = fake(os.path.join(src, "ATFIX902_receipt.pdf"), "pdf", "sync902")
    for f, ref, day in ((f1, "ATFIX901", "2026-07-10"), (f2, "ATFIX902", "2026-07-11")):
        p = run(root, "add", f, "--entity", "viseo", "--date", day, "--merchant", "TESTVENDOR AB",
                "--amount-sek", "250,00", "--card", "amex-1022", "--lane", "amex-fortnox", "--amex-ref", ref)
        ok(f"sync fixture add {ref}", p.returncode == 0, p.stdout + p.stderr)
    plan = os.path.join(work, "fortnox-run-2026-07")
    os.makedirs(plan, exist_ok=True)
    state = {"batch_code": "abc", "salary_done": True,
             "rows": {"ATFIX901": {"file_id": "f-1", "voucher_number": 12, "voucher_series": "A",
                                   "voucher_year": 2026, "connected": True},
                      "ATFIX902": {},
                      "ATFIX903": {"voucher_number": 13, "voucher_series": "A"}},
             "log": ["2026-09-05 10:00:00 kvitto uppladdat: ATFIX901 File.Id f-1",
                     "2026-09-05 10:00:01 verifikat skapat: ATFIX901 -> A12 (TESTVENDOR AB, 250,00 SEK)"]}
    with open(os.path.join(plan, "execution.json"), "w", encoding="utf-8") as fh:
        json.dump(state, fh)
    p = run(root, "sync-fortnox", os.path.join(work, "no-such-plan"))
    ok("sync-fortnox missing plan exit 1", p.returncode == 1 and "execution.json" in p.stderr, (p.returncode, p.stderr))
    before = content_snapshot(root)
    p = run(root, "--dry-run", "sync-fortnox", plan)
    ok("sync-fortnox dry-run exit 0", p.returncode == 0, p.stdout + p.stderr)
    ok("sync-fortnox dry-run says would", "would update 2026-07-10-testvendor-250-00" in p.stdout, p.stdout)
    ok("sync-fortnox dry-run writes nothing", content_snapshot(root) == before)
    p = run(root, "sync-fortnox", plan)
    ok("sync-fortnox exit 0", p.returncode == 0, p.stdout + p.stderr)
    ok("sync-fortnox summary", "updated 1, unchanged 0, no page 1, no verifikat 1" in p.stdout, p.stdout)
    ok("sync-fortnox names the missing page", "ATFIX903" in p.stdout and "A 13" in p.stdout, p.stdout)
    fm, body, _ = read_page(root, "viseo/2026/2026-07-10-testvendor-250-00.md")
    ok("sync-fortnox voucher on page", fm is not None and fm.get("fortnox_voucher") == "A 12"
       and fm.get("fortnox_status") == "bokfort 2026-09-05", fm and (fm.get("fortnox_voucher"), fm.get("fortnox_status")))
    ok("sync-fortnox status sentence", body is not None and "Bokfort i Fortnox, verifikation A 12." in body, body)
    fm2, _, _ = read_page(root, "viseo/2026/2026-07-11-testvendor-250-00.md")
    ok("sync-fortnox leaves the unbooked page alone", fm2 is not None and fm2.get("fortnox_status") == "ej bokfort"
       and "fortnox_voucher" not in fm2, fm2)
    p = run(root, "sync-fortnox", plan)
    ok("sync-fortnox re-run unchanged", p.returncode == 0 and "updated 0, unchanged 1" in p.stdout, p.stdout)
    with open(os.path.join(root, "index.md"), encoding="utf-8") as fh:
        idx = fh.read()
    ok("sync-fortnox index shows the verifikat", "Fortnox: A 12" in idx, idx)
    p = run(root, "set", "2026-07-11-testvendor-250-00", "source_kind=gmail", "source_ref=19f4dac84ce2b2d9")
    ok("set source_kind gmail", p.returncode == 0 and "updated" in p.stdout, p.stdout + p.stderr)
    fm2, body2, _ = read_page(root, "viseo/2026/2026-07-11-testvendor-250-00.md")
    ok("set source_kind lands", fm2 is not None and fm2.get("source_kind") == "gmail"
       and fm2.get("source_ref") == "19f4dac84ce2b2d9" and "Gmail 19f4dac84ce2b2d9" in (body2 or ""), (fm2, body2))
    p = run(root, "set", "2026-07-11-testvendor-250-00", "source_kind=bogus")
    ok("set source_kind bogus refused", p.returncode == 1 and "source_kind" in p.stderr, (p.returncode, p.stderr))


def main():
    real_before = snapshot(REAL_ROOT)
    work = tempfile.mkdtemp(prefix="test_archive_")
    root = os.path.join(work, "archive")

    check_frontmatter_roundtrip()
    check_amounts_and_vendors()
    check_harvest_and_fit()
    check_receipt_text_helper()
    check_merge_and_plausibility()

    # --- ingest pleo-export: dry-run first, then the real run, then a no-op re-run ---
    pleo_dir = write_pleo_fixture(os.path.join(work, "pleo"))
    p = run(root, "--dry-run", "ingest", "pleo-export", pleo_dir)
    ok("pleo dry-run exit 0", p.returncode == 0, p.stderr)
    ok("pleo dry-run says would", "would" in p.stdout, p.stdout)
    ok("pleo dry-run writes nothing", not snapshot(root), snapshot(root))

    p = run(root, "ingest", "pleo-export", pleo_dir)
    ok("pleo ingest exit 0", p.returncode == 0, p.stderr)
    ok("pleo summary", "created 3, updated 0, unchanged 0, skipped 3" in p.stdout, p.stdout)
    ok("pleo skipped rows reported", "2600103" in p.stdout and "2600104" in p.stdout, p.stdout)
    ok("pleo report has no em dash", EM_DASH not in p.stdout and "TESTPOST - AB" in p.stdout, p.stdout)
    ok("pleo second expense on the same file refused", "2600106" in p.stdout and "another Pleo expense" in p.stdout
       and f"({EXP9}, the page has {EXP8})" in p.stdout, p.stdout)
    fm, _, _ = read_page(root, "viseo/2026/2026-07-20-testdubbelkvitto-45-00.md")
    ok("pleo shared file page keeps the first expense", fm is not None and fm.get("pleo_expense_id") == EXP8, fm and fm.get("pleo_expense_id"))
    r1 = "viseo/2026/2026-07-10-testvendor-250-00"
    r2 = "viseo/2026/2026-07-17-testcloud-981-09"
    fm, body, r1_text = read_page(root, r1 + ".md")
    ok("pleo page 1 exists", fm is not None, os.listdir(os.path.join(root, "viseo", "2026")) if os.path.isdir(os.path.join(root, "viseo", "2026")) else "no viseo/2026")
    if fm is not None:
        missing = [k for k in REQUIRED if k not in fm]
        ok("pleo page 1 required keys", not missing, missing)
        ok("pleo page 1 identity", fm.get("type") == "receipt" and fm.get("id") == "2026-07-10-testvendor-250-00"
           and fm.get("kind") == "receipt" and fm.get("entity") == "viseo", fm)
        ok("pleo page 1 title", str(fm.get("title", "")).startswith("Testvendor")
           and "250,00 SEK 2026-07-10" in str(fm.get("title", "")), fm.get("title"))
        ok("pleo page 1 card and lane", fm.get("card") == "pleo" and fm.get("lane") == "pleo", fm)
        ok("pleo page 1 amounts", near(fm.get("amount_sek"), 250.0) and fm.get("currency") == "SEK"
           and near(fm.get("amount"), 250.0), fm)
        ok("pleo page 1 pleo fields", fm.get("pleo_expense_id") == EXP1
           and str(fm.get("pleo_receipt_number")) == "2600101" and fm.get("pleo_category") == "Programvaror"
           and fm.get("pleo_status") == "i Pleo (NOT_EXPORTED)", fm)
        ok("pleo page 1 kontering", str(fm.get("bas_account")) == "5420" and near(fm.get("vat_rate"), 25.0)
           and near(fm.get("vat_sek"), 50.0) and fm.get("vendor_country") == "SE", fm)
        ok("pleo page 1 source", fm.get("source_kind") == "pleo-export"
           and fm.get("source_ref") == "https://app.pleo.io:443/accounting-entries/aaaa/receipts/bbbb/file", fm)
        ok("pleo page 1 files", fm.get("files") == ["2026-07-10-testvendor-250-00.pdf"], fm.get("files"))
        primary = os.path.join(root, r1 + ".pdf")
        ok("pleo page 1 file copied with sha", os.path.exists(primary) and fm.get("sha256") == sha(primary)
           and sha(primary) == sha(os.path.join(pleo_dir, "receipts", "2600101.pdf")), fm.get("sha256"))
        ok("pleo source file not moved", os.path.exists(os.path.join(pleo_dir, "receipts", "2600101.pdf")))
        ok("pleo page 1 no-text", fm.get("text_method") == "none" and "preview" not in fm
           and "text_truncated" not in fm, fm)
        ok("pleo page 1 tags", fm.get("tags") == ["receipt", "viseo", "testvendor", "2026-07"], fm.get("tags"))
        ok("pleo page 1 timestamps", TS_RE.match(str(fm.get("created", ""))) and TS_RE.match(str(fm.get("updated", ""))),
           (fm.get("created"), fm.get("updated")))
        ok("pleo page 1 body headings", body.startswith("# ") and "## Underlag" in body and "## Kvittotext" in body, body[:200])
        ok("pleo page 1 header sentence", "250,00 SEK" in body and "den 2026-07-10" in body
           and "betalt med Pleo-kortet" in body, body[:400])
        ok("pleo page 1 Fil bullet", "- Fil: [2026-07-10-testvendor-250-00.pdf](2026-07-10-testvendor-250-00.pdf) (" in body
           and f"sha256 {fm.get('sha256', '')[:12]}" in body, body)
        ok("pleo page 1 Kalla bullet", "- Kalla: Pleo export 2026-08-21, kvitto 2600101" in body, body)
        ok("pleo page 1 Pleo bullet", f"- Pleo: utgift {EXP1}, kvitto 2600101, i Pleo (NOT_EXPORTED)" in body, body)
        ok("pleo page 1 Kontering bullet", "- Kontering: 5420, moms 25" in body, body)
        ok("pleo page 1 no text sentence", "Ingen text kunde lasas ur filen." in kvittotext(body), body)
        ok("pleo page 1 no Forhandsvisning", "Forhandsvisning" not in body, body)
    fm2, body2, _ = read_page(root, r2 + ".md")
    ok("pleo page 2 exists", fm2 is not None)
    if fm2 is not None:
        ok("pleo page 2 foreign amounts", near(fm2.get("amount_sek"), 981.09) and fm2.get("currency") == "USD"
           and near(fm2.get("amount"), 99.0), fm2)
        ok("pleo page 2 a/b files", fm2.get("files") == ["2026-07-17-testcloud-981-09.pdf",
                                                        "2026-07-17-testcloud-981-09-2.pdf"], fm2.get("files"))
        ok("pleo page 2 files on disk", os.path.exists(os.path.join(root, r2 + ".pdf"))
           and os.path.exists(os.path.join(root, r2 + "-2.pdf")))
        ok("pleo page 2 zero tax rate omitted", "vat_rate" not in fm2, fm2.get("vat_rate"))
        ok("pleo page 2 status", fm2.get("pleo_status") == "i Pleo (EXPORTED)", fm2.get("pleo_status"))
        ok("pleo page 2 header shows foreign amount", "981,09 SEK (99,00 USD)" in body2, body2[:400])
        ok("pleo page 2 Extra bullet",
           "- Extra: [2026-07-17-testcloud-981-09-2.pdf](2026-07-17-testcloud-981-09-2.pdf)" in body2, body2)
    ok("pleo creates 3 pages only", len(pages(root)) == 3, pages(root))

    before = content_snapshot(root)
    p = run(root, "ingest", "pleo-export", pleo_dir)
    ok("pleo re-run exit 0", p.returncode == 0, p.stderr)
    ok("pleo re-run summary", "created 0, updated 0, unchanged 3, skipped 3" in p.stdout, p.stdout)
    ok("pleo re-run changes nothing", content_snapshot(root) == before,
       [k for k in set(before) | set(content_snapshot(root)) if before.get(k) != content_snapshot(root).get(k)])

    # --- ingest matching: html sibling as text source, new1 receipt number, second-source merge ---
    mcsv, mfiles = write_matching_fixture(os.path.join(work, "matching"))
    p = run(root, "ingest", "matching", mcsv, "--files-dir", mfiles, text=True)
    ok("matching ingest exit 0", p.returncode == 0, p.stderr)
    ok("matching summary", "created 2, updated 1, unchanged 0, skipped 1" in p.stdout, p.stdout)
    ok("matching skipped row reported", "2600203" in p.stdout, p.stdout)
    ma = "viseo/2026/2026-08-17-testvendor-981-09"
    fm, body, _ = read_page(root, ma + ".md")
    ok("matching page exists", fm is not None)
    if fm is not None:
        ok("matching identity", fm.get("card") == "pleo" and fm.get("lane") == "pleo" and fm.get("entity") == "viseo"
           and fm.get("pleo_expense_id") == EXP5 and str(fm.get("pleo_receipt_number")) == "2600201"
           and fm.get("pleo_status") == "attached 2026-08-24", fm)
        ok("matching amounts from pleo_amount", near(fm.get("amount_sek"), 981.09) and fm.get("currency") == "USD"
           and near(fm.get("amount"), 99.0), fm)
        ok("matching source gmail", fm.get("source_kind") == "gmail" and fm.get("source_ref") == "1a00b4197fake001", fm)
        files = fm.get("files") or []
        ok("matching three files incl html", len(files) == 3 and files[0] == "2026-08-17-testvendor-981-09.pdf"
           and any(f.endswith(".html") for f in files) and all(os.path.exists(os.path.join(root, "viseo", "2026", f)) for f in files),
           files)
        ok("matching text_method html", fm.get("text_method") == "html", fm.get("text_method"))
        kt = kvittotext(body)
        ok("matching text from html body", "Receipt from Testvendor" in kt and "INV-2026-0042" in kt, kt)
        ok("matching html script dropped", "var hidden" not in kt, kt)
        ok("matching harvested invoice_number", fm.get("invoice_number") == "INV-2026-0042", fm.get("invoice_number"))
        ok("matching harvested receipt_date", str(fm.get("receipt_date")) == "2026-08-17", fm.get("receipt_date"))
        ok("matching Kalla bullet", "- Kalla: Gmail 1a00b4197fake001" in body, body)
    fm, body, _ = read_page(root, "viseo/2026/2026-08-18-testkafe-129-00.md")
    ok("matching new1 page exists", fm is not None)
    if fm is not None:
        ok("matching new1 has no receipt number", not fm.get("pleo_receipt_number"), fm.get("pleo_receipt_number"))
        ok("matching new1 amounts", near(fm.get("amount_sek"), 129.0) and fm.get("currency") == "SEK"
           and near(fm.get("amount"), 129.0), fm)
    fm, body, r1_after = read_page(root, r1 + ".md")
    ok("second source merged into pleo page", fm is not None and len(fm.get("files") or []) == 2
       and fm.get("source_kind") == "gmail" and fm.get("source_ref") == "1a00b4197fake004"
       and fm.get("pleo_status") == "attached 2026-08-24 (NOT_EXPORTED)" and fm.get("pleo_category") == "Programvaror",
       fm and {k: fm.get(k) for k in ("files", "source_kind", "source_ref", "pleo_status", "pleo_category")})
    ok("merged status sentence", "Bifogat i Pleo 2026-08-24 (NOT_EXPORTED)." in body, body[:400])
    ok("merge keeps id and primary", fm is not None and fm.get("id") == "2026-07-10-testvendor-250-00"
       and (fm.get("files") or [""])[0] == "2026-07-10-testvendor-250-00.pdf"
       and fm.get("sha256") == sha(os.path.join(root, r1 + ".pdf")), fm and fm.get("files"))
    ok("merge bumps updated", fm is not None and r1_after != r1_text)
    ok("matching creates 2 pages", len(pages(root)) == 5, pages(root))
    p = run(root, "ingest", "matching", mcsv, "--files-dir", mfiles, text=True)
    ok("matching re-run summary", p.returncode == 0 and "created 0, updated 0, unchanged 3, skipped 1" in p.stdout,
       p.stdout + p.stderr)
    before = content_snapshot(root)
    p = run(root, "ingest", "pleo-export", pleo_dir)
    ok("pleo re-run after matching is a no-op", p.returncode == 0 and "created 0, updated 0, unchanged 3, skipped 3" in p.stdout
       and content_snapshot(root) == before, p.stdout + p.stderr)
    fm, _, _ = read_page(root, r1 + ".md")
    ok("pleo re-run keeps the attach date", fm is not None and fm.get("pleo_status") == "attached 2026-08-24 (NOT_EXPORTED)",
       fm and fm.get("pleo_status"))
    p = run(root, "ingest", "matching", mcsv, "--files-dir", mfiles, text=True)
    ok("matching re-run after pleo is a no-op", p.returncode == 0 and "created 0, updated 0, unchanged 3, skipped 1" in p.stdout
       and content_snapshot(root) == before, p.stdout + p.stderr)

    # --- ingest amex: junk jpg dropped, personal and needs-tag skipped, orphan file reported ---
    ledger, rdir, mpath, mid = write_amex_fixture(os.path.join(work, "amex"))
    p = run(root, "ingest", "amex", "--ledger", ledger, "--receipts-dir", rdir, "--matches", mpath)
    ok("amex ingest exit 0", p.returncode == 0, p.stderr)
    ok("amex summary", "created 2, updated 0, unchanged 0, skipped " in p.stdout, p.stdout)
    ok("amex skipped personal", "ATFIX102" in p.stdout, p.stdout)
    ok("amex skipped needs-tag", "ATFIX105" in p.stdout, p.stdout)
    ok("amex orphan file reported", "ATFIX999" in p.stdout, p.stdout)
    ok("amex junk jpg reported", "ATFIX101_1.jpg" in p.stdout, p.stdout)
    ok("amex business row without file reported", "ATFIX104" in p.stdout and "no receipt file in" in p.stdout
       and "skipped 5" in p.stdout, p.stdout)
    a1 = "viseo/2026/2026-06-05-testsaas-1599-00"
    fm, body, _ = read_page(root, a1 + ".md")
    ok("amex page exists", fm is not None)
    if fm is not None:
        ok("amex identity", fm.get("card") == "amex-1022" and fm.get("lane") == "amex-fortnox"
           and fm.get("amex_ref") == "ATFIX101" and fm.get("entity") == "viseo" and near(fm.get("amount_sek"), 1599.0), fm)
        ok("amex kontering", str(fm.get("bas_account")) == "6540" and fm.get("vat_regime") == "noneu_rc"
           and fm.get("note") == "IT-tjanster", fm)
        ok("amex foreign amount from ext", fm.get("currency") == "USD" and near(fm.get("amount"), 149.0)
           and "1 599,00 SEK (149,00 USD) den 2026-06-05" in body, (fm.get("currency"), fm.get("amount"), body[:300]))
        ok("amex default fortnox_status", fm.get("fortnox_status") == "ej bokfort", fm.get("fortnox_status"))
        ok("amex files pdf first then png, no junk", fm.get("files") == ["2026-06-05-testsaas-1599-00.pdf",
                                                                        "2026-06-05-testsaas-1599-00-2.png"], fm.get("files"))
        ok("amex junk jpg not copied", not any(f.endswith(".jpg") for f in os.listdir(os.path.join(root, "viseo", "2026"))))
        ok("amex source from matches", fm.get("source_kind") == "gmail" and fm.get("source_ref") == mid
           and fm.get("source_subject") == "Your receipt from Testsaas"
           and "testsaas.example" in str(fm.get("source_from", "")), fm)
        ok("amex body", "betalt med Amex 1022" in body and "- Fortnox: ej bokfort" in body
           and "- Kontering: 6540, moms noneu_rc" in body and f"- Kalla: Gmail {mid}" in body
           and "- Anteckning: IT-tjanster" in body, body)
    a3 = "5555media/2026/2026-06-20-testvendor-250-00"
    fm, body, _ = read_page(root, a3 + ".md")
    ok("amex 5555media page exists", fm is not None)
    if fm is not None:
        ok("amex image primary", fm.get("files") == ["2026-06-20-testvendor-250-00.png"] and fm.get("entity") == "5555media"
           and "5555media" in (fm.get("tags") or []) and near(fm.get("amount_sek"), 250.0), fm)
    ok("amex creates 2 pages", len(pages(root)) == 7, pages(root))
    before = content_snapshot(root)
    p = run(root, "ingest", "amex", "--ledger", ledger, "--receipts-dir", rdir, "--matches", mpath)
    ok("amex re-run summary", p.returncode == 0 and "created 0, updated 0, unchanged 2, skipped " in p.stdout,
       p.stdout + p.stderr)
    ok("amex re-run changes nothing", content_snapshot(root) == before)

    # --- library: add with a stub extractor, harvested fields, set keeps Kvittotext ---
    archive.EXTRACTOR = stub_extract
    lib_src = fake(os.path.join(work, "lib", "wincher_invoice.pdf"), "pdf", "lib-wincher")
    arc = archive.Archive(root)
    meta = {"entity": "viseo", "date": "2026-07-10", "merchant": "WINCHERINTERNATIONALCOM STOCKHOLM",
            "amount_sek": 3570.54, "currency": "EUR", "amount": 320.0, "card": "amex-1022",
            "lane": "amex-fortnox", "source_kind": "gmail", "source_ref": "19e1207bfake0777",
            "source_subject": "Subscription Payment Confirmation", "source_from": "no-reply@wincher.example",
            "amex_ref": "ATFIX777", "bas_account": "6540", "vat_regime": "eu_rc", "fortnox_status": "ej bokfort"}
    rec = call("library add raises", arc.add, [lib_src], meta)
    w_stem = "2026-07-10-wincher-3570-54"
    rec_id = getattr(rec, "id", None) or (rec.get("id") if isinstance(rec, dict) else None)
    ok("library add returns record with id", rec_id == w_stem, rec)
    ok("library add copies, never moves", os.path.exists(lib_src))
    wp = "viseo/2026/" + w_stem
    fm, body, _ = read_page(root, wp + ".md")
    ok("library add page exists", fm is not None)
    if fm is not None:
        ok("library add vendor and title", fm.get("vendor") == "Wincher"
           and fm.get("title") == "Wincher 3 570,54 SEK 2026-07-10", (fm.get("vendor"), fm.get("title")))
        ok("library add tags", fm.get("tags") == ["receipt", "viseo", "wincher", "2026-07"], fm.get("tags"))
        ok("library add file", fm.get("files") == [w_stem + ".pdf"] and sha(os.path.join(root, wp + ".pdf")) == sha(lib_src)
           and fm.get("sha256") == sha(lib_src), fm.get("files"))
        ok("library add text_method from extractor", fm.get("text_method") == "pdfkit", fm.get("text_method"))
        kt = kvittotext(body)
        ok("library text nbsp and trailing spaces", "Wincher International AB\n" in kt, repr(kt))
        ok("library text blank lines collapsed", "\n\n\n\n" not in kt and "Slut" in kt, repr(kt))
        ok("library harvested invoice_number", fm.get("invoice_number") == "WI-2026-0710", fm.get("invoice_number"))
        ok("library harvested vendor_vat_number skips own", fm.get("vendor_vat_number") == "EU826012345",
           fm.get("vendor_vat_number"))
        ok("library harvested vat_sek", near(fm.get("vat_sek"), 699.33), fm.get("vat_sek"))
        ok("library harvested receipt_date", str(fm.get("receipt_date")) == "2026-07-10", fm.get("receipt_date"))
        ok("library body header", "Wincher, 3 570,54 SEK (320,00 EUR) den 2026-07-10, betalt med Amex 1022." in body, body[:400])
        ok("library body status sentence", "Ej bokfort i Fortnox." in body, body[:400])
        ok("library body Kalla with subject and from",
           '- Kalla: Gmail 19e1207bfake0777, "Subscription Payment Confirmation" fran no-reply@wincher.example' in body, body)
        ok("library body Kontering", "- Kontering: 6540, moms eu_rc" in body, body)
        ok("library get", call("library get raises", arc.get, w_stem) is not None)
        created0, updated0 = fm.get("created"), fm.get("updated")
        time.sleep(1.1)
        call("library set raises", arc.set, w_stem, note="Kontrollerad mot kontoutdrag")
        fm_b, body_b, _ = read_page(root, wp + ".md")
        ok("library set note in frontmatter", fm_b is not None and fm_b.get("note") == "Kontrollerad mot kontoutdrag",
           fm_b and fm_b.get("note"))
        ok("library set note in body", "- Anteckning: Kontrollerad mot kontoutdrag" in body_b, body_b)
        ok("library set bumps updated only", fm_b is not None and fm_b.get("created") == created0
           and fm_b.get("updated") != updated0 and TS_RE.match(str(fm_b.get("updated", ""))),
           fm_b and (created0, fm_b.get("created"), updated0, fm_b.get("updated")))
        ok("library set keeps Kvittotext", kvittotext(body_b) == kt, repr(kvittotext(body_b))[:300])
    # dedup by sha: adding the same bytes again creates nothing
    n_pages = len(pages(root))
    call("library add same sha raises", arc.add, [lib_src], dict(meta, merchant="ANNAN TEXT"))
    ok("library add same sha creates no page", len(pages(root)) == n_pages, pages(root))

    # --- CLI add and set; stem collisions get -2 and -3 ---
    cafe_src = fake(os.path.join(work, "add", "cafe.pdf"), "pdf", "cli-cafe")
    p = run(root, "add", cafe_src, "--entity", "viseo", "--date", "2026-08-24",
            "--merchant", "CAFÉ ÅÄÖ AB STOCKHOLM", "--amount-sek", "129",
            "--card", "amex-1022", "--lane", "amex-fortnox", "--source-kind", "paper", "--note", "Fika med kund")
    ok("cli add exit 0", p.returncode == 0, p.stderr)
    c_stem = "2026-08-24-cafe-aao-129-00"
    cp = "viseo/2026/" + c_stem
    fm, body, _ = read_page(root, cp + ".md")
    ok("cli add page with folded stem", fm is not None, pages(root))
    if fm is not None:
        ok("cli add fields", near(fm.get("amount_sek"), 129.0) and fm.get("currency") == "SEK" and near(fm.get("amount"), 129.0)
           and fm.get("card") == "amex-1022" and fm.get("source_kind") == "paper" and fm.get("note") == "Fika med kund", fm)
        ok("cli add body", "- Anteckning: Fika med kund" in body and "- Fortnox: ej bokfort" in body
           and "betalt med Amex 1022" in body, body)
        updated0 = fm.get("updated")
        time.sleep(1.1)
        p = run(root, "set", c_stem, "fortnox_voucher=A 12", "fortnox_status=bokford")
        ok("cli set exit 0", p.returncode == 0, p.stderr)
        fm_b, body_b, _ = read_page(root, cp + ".md")
        ok("cli set frontmatter", fm_b is not None and fm_b.get("fortnox_voucher") == "A 12"
           and fm_b.get("fortnox_status") == "bokford", fm_b)
        ok("cli set updated bumped", fm_b is not None and fm_b.get("updated") != updated0
           and fm_b.get("created") == fm.get("created"), fm_b and (updated0, fm_b.get("updated")))
        ok("cli set body Fortnox bullet", "- Fortnox: A 12" in body_b, body_b)
        ok("cli set status sentence", "Bokfort i Fortnox, verifikation A 12." in body_b, body_b[:400])
        ok("cli set keeps Kvittotext", kvittotext(body_b) == kvittotext(body), body_b)
        ok("cli set keeps note", "- Anteckning: Fika med kund" in body_b, body_b)
        for bad in ("id=x", "self=1", "sha256=abc"):
            p = run(root, "set", c_stem, bad)
            ok(f"cli set {bad} clean error", p.returncode == 1 and "not settable" in p.stderr and "Traceback" not in p.stderr,
               (p.returncode, p.stderr))
    for n in (1, 2, 3):
        src = fake(os.path.join(work, "add", f"dubbel{n}.pdf"), "pdf", f"cli-dubbel-{n}")
        p = run(root, "add", src, "--entity", "viseo", "--date", "2026-05-05", "--merchant", "TESTDUBBEL AB",
                "--vendor", "Testdubbel", "--amount-sek", "77.00", "--card", "pocket", "--lane", "pocket",
                "--source-kind", "paper")
        ok(f"cli add dubbel {n} exit 0", p.returncode == 0, p.stderr)
    d_dir = os.path.join(root, "viseo", "2026")
    d_pages = sorted(f for f in os.listdir(d_dir) if f.startswith("2026-05-05-testdubbel-77-00") and f.endswith(".md"))
    ok("stem collision suffixes", d_pages == ["2026-05-05-testdubbel-77-00-2.md", "2026-05-05-testdubbel-77-00-3.md",
                                             "2026-05-05-testdubbel-77-00.md"], d_pages)
    fm, _, _ = read_page(root, "viseo/2026/2026-05-05-testdubbel-77-00-3.md")
    ok("collision page id equals stem", fm is not None and fm.get("id") == "2026-05-05-testdubbel-77-00-3"
       and fm.get("files") == ["2026-05-05-testdubbel-77-00-3.pdf"] and fm.get("card") == "pocket", fm)
    src = os.path.join(work, "add", "dubbel2.pdf")
    p = run(root, "add", src, "--entity", "viseo", "--date", "2026-05-05", "--merchant", "TESTDUBBEL AB",
            "--vendor", "Testdubbel", "--amount-sek", "77", "--card", "pocket", "--lane", "pocket")
    ok("cli add same bytes is dedup", p.returncode == 0 and len(pages(root)) == 12, (p.stderr, len(pages(root))))

    # --- index ---
    arc2 = archive.Archive(root)
    call("library write_index raises", arc2.write_index)
    ok("library write_index", os.path.exists(os.path.join(root, "index.md")))
    p = run(root, "index")
    ok("cli index exit 0", p.returncode == 0, p.stderr)
    fm, body, idx = read_page(root, "index.md")
    ok("index frontmatter", fm is not None and fm.get("type") == "index" and fm.get("title") == "Kvittoarkiv"
       and TS_RE.match(str(fm.get("updated", ""))), fm)
    ok("index heading", body.lstrip().startswith("# Kvittoarkiv"), body[:80])
    ok("index no tables", "|---" not in idx and not any(ln.startswith("|") for ln in idx.splitlines()), idx)
    ok("index no dashes", EM_DASH not in idx and EN_DASH not in idx)
    ok("index entity headings", "## Viseo AB" in body and "## 5555 Media AB" in body
       and body.find("## Viseo AB") < body.find("## 5555 Media AB"), body)
    viseo_part = body.split("## Viseo AB", 1)[1].split("## 5555 Media AB", 1)[0] if "## Viseo AB" in body else ""
    media_part = body.split("## 5555 Media AB", 1)[1] if "## 5555 Media AB" in body else ""
    ok("index months descending", re.findall(r"^### (\S+)", viseo_part, re.M) == ["2026-08", "2026-07", "2026-06", "2026-05"]
       and re.findall(r"^### (\S+)", media_part, re.M) == ["2026-06"],
       (re.findall(r"^### (\S+)", viseo_part, re.M), re.findall(r"^### (\S+)", media_part, re.M)))
    ok("index month totals viseo", "- 3 kvitton, 1 239,09 SEK" in viseo_part and "- 4 kvitton, 4 846,63 SEK" in viseo_part
       and re.search(r"^- 1 kvitto(n)?, 1 599,00 SEK", viseo_part, re.M) and "- 3 kvitton, 231,00 SEK" in viseo_part,
       viseo_part)
    ok("index month total 5555media", re.search(r"^- 1 kvitto(n)?, 250,00 SEK", media_part, re.M), media_part)
    ok("index wincher bullet exact",
       "- 2026-07-10 | Wincher | 3 570,54 SEK | Amex 1022 | Fortnox: ej bokfort | [sida](viseo/2026/2026-07-10-wincher-3570-54.md)"
       in viseo_part, viseo_part)
    ok("index one bullet per receipt", idx.count("[sida](") == 12, idx.count("[sida]("))
    r1_line = next((ln for ln in idx.splitlines() if "[sida](viseo/2026/2026-07-10-testvendor-250-00.md)" in ln), "")
    ok("index pleo status column", r1_line.startswith("- 2026-07-10 | ") and "| 250,00 SEK |" in r1_line
       and "| Pleo-kortet |" in r1_line and "attached 2026-08-24" in r1_line, r1_line)
    ok("index voucher shown", "A 12" in viseo_part and "[sida](5555media/2026/2026-06-20-testvendor-250-00.md)" in media_part,
       viseo_part)

    # --- every generated page: bullets only, no dashes, id equals stem, entity/year match path ---
    for path in pages(root):
        rel = os.path.relpath(path, root)
        fm, body, txt = read_page(root, rel)
        ok(f"page {rel} clean", fm is not None and EM_DASH not in txt and EN_DASH not in txt
           and not any(ln.startswith("|") for ln in txt.splitlines())
           and fm.get("id") == os.path.basename(rel)[:-3]
           and rel.split(os.sep)[0] == fm.get("entity") and rel.split(os.sep)[1] == str(fm.get("date"))[:4], rel)

    # --- find ---
    p = run(root, "find", "--amount-sek", "3570.54")
    ok("find by amount", p.returncode == 0 and len(lines(p.stdout)) == 1 and w_stem in p.stdout and "Wincher" in p.stdout
       and "3 570,54 SEK" in p.stdout and "2026-07-10" in p.stdout and "1022" in p.stdout, p.stdout + p.stderr)
    p = run(root, "find", "--amount-sek", "3570.541")
    ok("find amount within 0.005", len(lines(p.stdout)) == 1, p.stdout)
    p = run(root, "find", "--amount-sek", "3570.55")
    ok("find amount outside 0.005", len(lines(p.stdout)) == 0, p.stdout)
    p = run(root, "find", "--amount-sek", "250")
    ok("find amount across entities", len(lines(p.stdout)) == 2, p.stdout)
    p = run(root, "find", "--amount-sek", "abc")
    ok("find non-numeric amount clean error", p.returncode == 1 and "amount_sek must be a number" in p.stderr
       and "Traceback" not in p.stderr, (p.returncode, p.stderr))
    p = run(root, "find", "--amount-sek", "250", "--entity", "5555media")
    ok("find amount and entity", len(lines(p.stdout)) == 1 and "2026-06-20-testvendor-250-00" in p.stdout, p.stdout)
    p = run(root, "find", "--month", "2026-08")
    ok("find by month", len(lines(p.stdout)) == 3, p.stdout)
    p = run(root, "find", "--month", "2026-08", "--json")
    try:
        got = json.loads(p.stdout)
        got_ids = sorted(d.get("id") for d in got)
    except (ValueError, AttributeError, TypeError):
        got_ids = p.stdout
    ok("find json", got_ids == sorted(["2026-08-17-testvendor-981-09", "2026-08-18-testkafe-129-00", c_stem]), got_ids)
    p = run(root, "find", "--vendor", "Wincher")
    ok("find by vendor", len(lines(p.stdout)) == 1 and w_stem in p.stdout, p.stdout)
    p = run(root, "find", "--date", "2026-05-05")
    ok("find by date", len(lines(p.stdout)) == 3, p.stdout)
    p = run(root, "find", "--amex-ref", "ATFIX101")
    ok("find by amex ref", len(lines(p.stdout)) == 1 and "2026-06-05-testsaas-1599-00" in p.stdout, p.stdout)
    p = run(root, "find", "--pleo-expense-id", EXP1)
    ok("find by pleo expense id", len(lines(p.stdout)) == 1 and "2026-07-10-testvendor-250-00" in p.stdout, p.stdout)
    p = run(root, "find", "--sha256", sha(lib_src))
    ok("find by sha256", len(lines(p.stdout)) == 1 and w_stem in p.stdout, p.stdout)
    hits = call("library find by amount raises", arc2.find, amount_sek=3570.54)
    ok("library find by amount", hits is not None and len(hits) == 1, hits)
    hits = call("library find by month raises", arc2.find, month="2026-08")
    ok("library find by month", hits is not None and len(hits) == 3, hits)
    hits = call("library find by entity raises", arc2.find, entity="5555media")
    ok("library find by entity", hits is not None and len(hits) == 1, hits)

    # --- check: clean, then tampered primary, then an em dash ---
    errs = call("library check raises", arc2.check)
    ok("library check clean", isinstance(errs, list) and errs == [], errs)
    p = run(root, "check")
    ok("cli check clean", p.returncode == 0, p.stdout + p.stderr)
    primary = os.path.join(root, r1 + ".pdf")
    with open(primary, "rb") as fh:
        orig = fh.read()
    with open(primary, "wb") as fh:
        fh.write(b"%PDF-1.4 tampered\n")
    p = run(root, "check")
    ok("cli check tampered exit 1", p.returncode == 1, (p.returncode, p.stdout, p.stderr))
    ok("cli check names the tampered page", "2026-07-10-testvendor-250-00" in p.stdout + p.stderr
       and "sha" in (p.stdout + p.stderr).lower(), p.stdout + p.stderr)
    with open(primary, "wb") as fh:
        fh.write(orig)
    page_path = os.path.join(root, cp + ".md")
    with open(page_path, encoding="utf-8") as fh:
        page_orig = fh.read()
    with open(page_path, "w", encoding="utf-8") as fh:
        fh.write(page_orig + "\nNot " + EM_DASH + " test\n")
    p = run(root, "check")
    ok("cli check em dash exit 1", p.returncode == 1 and c_stem in p.stdout + p.stderr, (p.returncode, p.stdout, p.stderr))
    with open(page_path, "w", encoding="utf-8") as fh:
        fh.write(page_orig)
    p = run(root, "check")
    ok("cli check clean again", p.returncode == 0, p.stdout + p.stderr)

    # --- dry-run set writes nothing ---
    before = content_snapshot(root)
    p = run(root, "--dry-run", "set", c_stem, "note=torrkorning")
    ok("dry-run set exit 0", p.returncode == 0, p.stderr)
    ok("dry-run set says would", "would" in p.stdout, p.stdout)
    ok("dry-run set writes nothing", content_snapshot(root) == before)
    fm, _, _ = read_page(root, cp + ".md")
    ok("dry-run set left note alone", fm is not None and fm.get("note") == "Fika med kund", fm and fm.get("note"))

    check_sync_fortnox(work)

    # --- the real archive root and the fixtures are untouched ---
    ok("real archive root untouched", snapshot(REAL_ROOT) == real_before)
    ok("fixture files still present", os.path.exists(os.path.join(mfiles, "TESTMATCH1_testvendor.pdf"))
       and os.path.exists(os.path.join(rdir, "ATFIX101_invoice.pdf")) and os.path.exists(cafe_src))

    n_fail = sum(not c for _, c, _ in checks)
    if n_fail:
        print(f"\ntest_archive: {len(checks)} checks, {n_fail} failed (work dir kept: {work})")
        sys.exit(1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"test_archive: {len(checks)} checks, 0 failed")


if __name__ == "__main__":
    main()
