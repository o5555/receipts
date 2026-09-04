#!/usr/bin/env python3
"""Kvittotavlan collectors and CLI: the receipts repo's progress model and its HTML page.

build_model(root, today, archive_root) reads the repo's inputs and returns one
JSON-serialisable dict (the contract the renderer in dashboard_html.py consumes):
freshness of every source, headline figures, an ordered todo list, and the Pleo,
Amex and archive sections. Inputs, all optional and read-only:

  data/pleo/expenses_*/            newest Pleo export (csv + receipts/), sha256 per file
  out/pleo-missing-receipts-*.csv  live list of Pleo rows without a receipt (two formats)
  out/pleo-kvittofel-*.txt         attachment-error worklist, compared against the exports
  out/amex-*.classified2.csv       classified Amex ledger (fallback *.classified.csv)
  out/amex-needs-tag-*.csv         rows waiting for Oscar's tag; data/tags/overrides.csv answers them
  data/amex/activity*.csv          coverage end of the Amex exports
  out/receipts-amex/map-*.csv      fetched Amex receipts per month
  out/fortnox-run-*/               month plans (manifest, plan.md, execution.json, uploads.json)
  archive/                         the kvittoarkiv, through scripts/archive.py's Archive

A missing input gives empty lists, None dates and the source state "missing"; nothing
raises on an absent file. Amounts are floats rounded to two decimals; dates ISO;
months "YYYY-MM"; labels Swedish. Merchant aliasing follows scripts/gmail_match.py
(exact, longest substring, regex, distinctive token) over scripts/vendor_aliases.json,
vendor names come from archive.resolve_vendor. No network, no Gmail, no Pleo, no Fortnox;
the only file written is the HTML page (--out, default out/dashboard.html).

Usage:
  scripts/dashboard.py [--root DIR] [--today YYYY-MM-DD] [--out PATH] [--archive DIR]
  scripts/dashboard.py --json --today 2026-09-04      # the model on stdout instead of HTML
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sys
from collections import Counter, OrderedDict
from datetime import date as date_type, datetime, timedelta, timezone
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO = SCRIPTS.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import archive  # noqa: E402 - sibling module
from archive import Archive, parse_amount, sv_amount  # noqa: E402
from dashboard_html import month_name as sv_month, plural, render  # noqa: E402

ENTITY = "viseo"
ENTITY_NAME = archive.ENTITY_NAMES.get(ENTITY, "Viseo AB")
STALE_DAYS = 14
ALIASES_PATH = SCRIPTS / "vendor_aliases.json"

ROUTE_LABELS = {
    "kivra": "Kivra-appen",
    "portal": "leverantörsportal",
    "pocket": "eget utlägg",
    "gmail": "Gmail",
    "forward": "bifoga via MCP",
    "forward or attach": "bifoga via MCP",
    "attach direct": "bifoga via MCP",
    "attach": "bifoga via MCP",
    "confirm in app": "bifoga via MCP",
    "mcp": "bifoga via MCP",
    "ask Nicolina": "fråga Nicolina",
    "attached": "bifogat sedan listan gjordes",
}
# route strings from the format-b list that all mean "attach through the Pleo MCP"
MCP_ROUTES = {"forward", "forward or attach", "attach direct", "attach", "confirm in app"}
MCP_ROUTE = "mcp"
ROUTE_OWNER_OSCAR = {"kivra", "pocket"}
KIVRA_HINT = "Kivra-appen, BankID, Oscar laddar ner"
GMAIL_HINT = "Gmail, bifoga via MCP"

GROUP_KEYS = ["same-file", "previous-period", "other-company", "unclear"]
GROUP_LABELS = {
    "same-file": "Samma fil på två utgifter",
    "previous-period": "Föregående periods kvitto",
    "other-company": "Ställt till annat bolag",
    "unclear": "Fel leverantör eller oklart underlag",
}
GROUP_PHRASES = {
    "same-file": ("{n} delar fil med ett annat köp", "{n} delar fil med ett annat köp"),
    "previous-period": ("{n} har föregående periods kvitto", "{n} har föregående periods kvitto"),
    "other-company": ("{n} är ställd till ett annat bolag", "{n} är ställda till ett annat bolag"),
    "unclear": ("{n} har fel leverantör eller oklart underlag", "{n} har fel leverantör eller oklart underlag"),
}
STATUS_LABELS = {"open": "öppen", "refiled": "ny fil bifogad", "gone": "saknas i exporten"}

STAGE_LABELS = OrderedDict([
    ("no-csv", "väntar på Amex-CSV"),
    ("untagged", "otaggade rader kvar"),
    ("no-plan", "plan saknas"),
    ("planned", "plan klar, väntar på godkännande"),
    ("partial", "delvis bokförd"),
    ("booked", "bokförd"),
    ("nothing-to-book", "inget att bokföra"),
])

WARNING_KINDS = [
    ("is also on", "duplicate-invoice", "samma fakturanummer på två köp"),
    ("days before the charge date", "earlier-period", "kvitto från en tidigare period"),
    ("days after the charge date", "later-period", "kvitto från en senare period"),
    ("made out to", "other-company", "ställt till annat bolag"),
    ("not found in the receipt text", "amount-missing", "beloppet finns inte i texten"),
    ("not named in the receipt text", "vendor-missing", "leverantören nämns inte i texten"),
    ("not referenced", "orphan-file", "fil utan sida"),
    ("index.md", "index-stale", "index behöver köras om"),
]
WARNING_LABELS = {k: label for _, k, label in WARNING_KINDS}
WARNING_LABELS["other"] = "övrigt"

SEVERITY_RANK = {"crit": 0, "warn": 1, "info": 2}


# ----------------------------------------------------------------------------- small helpers

def r2(x):
    """Float rounded to two decimals; 0.0 for None or junk."""
    try:
        return round(float(x or 0), 2)
    except (TypeError, ValueError):
        return 0.0


def norm_ws(s):
    return re.sub(r"\s+", " ", (s or "").replace(" ", " ")).strip()


def iso(s):
    """Any of the repo's date spellings -> 'YYYY-MM-DD' or None (DD-MM-YYYY, YYYY-MM-DD, MM/DD/YYYY)."""
    s = str(s or "").strip()
    if not s:
        return None
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        try:
            return date_type(int(m.group(3)), int(m.group(1)), int(m.group(2))).isoformat()
        except ValueError:
            return None
    return archive.iso_date(s)


def to_date(s):
    try:
        return date_type.fromisoformat(str(s)[:10])
    except (TypeError, ValueError):
        return None


def month_end(month):
    """'2026-08' -> date(2026, 8, 31); None for junk."""
    m = re.fullmatch(r"(\d{4})-(\d{2})", str(month or ""))
    if not m:
        return None
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        return None
    nxt = date_type(y + (mo == 12), 1 if mo == 12 else mo + 1, 1)
    return nxt - timedelta(days=1)


def month_add(month, n):
    """'2026-08' plus n months -> 'YYYY-MM'."""
    y, mo = int(month[:4]), int(month[5:7])
    idx = y * 12 + (mo - 1) + n
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def month_of(d):
    return str(d or "")[:7] if re.match(r"\d{4}-\d{2}", str(d or "")) else None


def last_full_month(coverage_end):
    """The last month the coverage date closes: its own month when it is the last day, else the one before."""
    d = to_date(coverage_end)
    if d is None:
        return None
    m = d.strftime("%Y-%m")
    return m if month_end(m) == d else month_add(m, -1)


def rel(path, root):
    """Path relative to root when inside it, else the absolute path; None for None."""
    if path is None:
        return None
    p = Path(path)
    try:
        return str(p.resolve().relative_to(Path(root).resolve()))
    except ValueError:
        return str(p)


def file_date(path):
    """The first YYYY-MM-DD (or YYYY-MM) in a file or folder name, else None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", Path(path).name)
    if m:
        return m.group(1)
    m = re.search(r"(\d{4}-\d{2})(?!\d)", Path(path).name)
    return m.group(1) if m else None


def mtime_date(path):
    try:
        return datetime.fromtimestamp(os.path.getmtime(path)).date().isoformat()
    except OSError:
        return None


def newest(paths, key=file_date):
    """The path with the greatest (key, name); None when there are none."""
    paths = [Path(p) for p in paths]
    if not paths:
        return None
    return max(paths, key=lambda p: (key(p) or "", p.name))


def read_csv(path):
    """Rows of a csv as dicts (utf-8-sig, blank keys stripped); [] when unreadable."""
    try:
        with open(path, "rb") as fh:
            text = fh.read().decode("utf-8-sig", errors="replace")
    except OSError:
        return []
    rows = []
    for r in csv.DictReader(io.StringIO(text)):
        rows.append({(k or "").strip(): (v or "").strip() if isinstance(v, str) else v for k, v in r.items()})
    return rows


def read_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def sha256_file(path):
    h = hashlib.sha256()
    try:
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return None
    return h.hexdigest()


def card_label(card):
    """'1022' -> 'Amex 1022'; archive card keys through archive.CARD_LABELS."""
    c = str(card or "").strip()
    if not c:
        return ""
    if c.lower() in archive.CARD_LABELS:
        return archive.CARD_LABELS[c.lower()]
    if c.lower().startswith("amex"):
        return c
    return f"Amex {c}"


def vendor_name(merchant, amount=None, currency=None):
    """Canonical vendor name for a merchant text (archive rules), never empty."""
    name, _slug = archive.resolve_vendor({"merchant": merchant or "", "amount": amount, "currency": currency})
    return name


# ----------------------------------------------------------------------------- alias matching (gmail_match rules)

_ALIASES = None


def alias_table():
    """(vendors dict in file order, stopword set) from scripts/vendor_aliases.json; empty when absent."""
    global _ALIASES
    if _ALIASES is None:
        data = read_json(ALIASES_PATH) or {}
        vendors = OrderedDict()
        for k, v in (data.get("vendors") or {}).items():
            if isinstance(v, dict) and v.get("merchants"):
                vendors[k] = v
        stop = set(str(w).upper() for w in (data.get("generic") or {}).get("merchant_stopwords") or [])
        _ALIASES = (vendors, stop)
    return _ALIASES


def merchant_tokens(merchant, stop):
    toks = []
    for t in re.split(r"[^A-Z0-9]+", (merchant or "").upper()):
        if len(t) >= 4 and t not in stop and not re.search(r"\d", t) and t not in toks:
            toks.append(t)
    return toks


def _pick_vendor(cands, amount_sek, foreign):
    if len(cands) == 1:
        return cands[0]
    for k, v in cands:
        for a in v.get("amounts") or []:
            if not isinstance(a, dict):
                continue
            have = parse_amount(a.get("amount"))
            if have is None:
                continue
            if foreign and a.get("currency") == foreign.get("currency") and abs(have - foreign["amount"]) <= 0.01:
                return k, v
            if v.get("bill_currency") == "SEK" and a.get("currency") == "SEK" and amount_sek is not None \
                    and abs(have - amount_sek) <= 0.01:
                return k, v
    return cands[0]


def alias_vendor(merchant, amount_sek=None, foreign=None):
    """(alias key, vendor dict) for a merchant text the way gmail_match resolves it: exact match,
    then the longest merchant substring (case-insensitive, whitespace collapsed), then the
    merchant regex, then a token only one vendor uses; ties broken by a listed amount.
    (None, None) when nothing matches."""
    vendors, stop = alias_table()
    m = norm_ws(merchant).upper()
    if not m or not vendors:
        return None, None
    exact = [(k, v) for k, v in vendors.items() if v.get("merchant_exact")
             and m in [norm_ws(str(x)).upper() for x in v["merchants"]]]
    if exact:
        return _pick_vendor(exact, amount_sek, foreign)
    subs = []
    for k, v in vendors.items():
        if v.get("merchant_exact"):
            continue
        best = max((len(norm_ws(str(x))) for x in v["merchants"] if norm_ws(str(x)).upper()
                    and norm_ws(str(x)).upper() in m), default=0)
        if best:
            subs.append((best, k, v))
    if subs:
        subs.sort(key=lambda t: -t[0])
        return _pick_vendor([(k, v) for ln, k, v in subs if ln == subs[0][0]], amount_sek, foreign)
    rx = []
    for k, v in vendors.items():
        pat = v.get("merchant_regex")
        if not pat:
            continue
        try:
            if re.search(pat, m, re.I):
                rx.append((k, v))
        except re.error:
            continue
    if rx:
        return _pick_vendor(rx, amount_sek, foreign)
    toks = set(merchant_tokens(m, stop))
    if toks:
        owners = {}
        for k, v in vendors.items():
            for x in list(v["merchants"]) + [v.get("name") or ""]:
                for t in merchant_tokens(str(x), stop):
                    owners.setdefault(t, set()).add(k)
        tok = [(k, vendors[k]) for t in sorted(toks) for k in sorted(owners.get(t, ())) if len(owners[t]) == 1]
        tok = [(k, v) for i, (k, v) in enumerate(tok) if k not in [x[0] for x in tok[:i]]]
        if tok:
            return _pick_vendor(tok, amount_sek, foreign)
    return None, None


# ----------------------------------------------------------------------------- Pleo export

def pleo_export_dirs(root):
    return sorted(p for p in Path(root).glob("data/pleo/expenses_*") if p.is_dir())


def load_pleo_export(root, export_dir):
    """Parsed Pleo export folder: rows, receipt files, sha256 per receipt number, same-file groups.
    None when export_dir is None or holds no csv."""
    if export_dir is None:
        return None
    export_dir = Path(export_dir)
    csvs = sorted(export_dir.glob("export_*.csv"))
    if not csvs:
        return None
    raw = read_csv(csvs[0])
    receipts_dir = export_dir / "receipts"
    files = []
    if receipts_dir.is_dir():
        files = sorted(f for f in receipts_dir.iterdir() if f.is_file() and not f.name.startswith("."))
    by_number = {}
    for f in files:
        number = re.sub(r"[a-z]?\.\w+$", "", f.name)
        by_number.setdefault(number, []).append(f)
    shas = {}
    sha_owners = {}
    for number, fs in by_number.items():
        s = set()
        for f in fs:
            h = sha256_file(f)
            if h:
                s.add(h)
                sha_owners.setdefault(h, set()).add(number)
        shas[number] = sorted(s)
    groups = sorted(sorted(nums) for nums in sha_owners.values() if len(nums) > 1)
    rows = []
    for r in raw:
        receipt = str(r.get("Receipt") or "").strip()
        urls = str(r.get("Receipt urls") or "").strip()
        etype = str(r.get("Expense Type") or "").strip()
        orig_cur = str(r.get("Orig. currency") or "").strip().upper()
        orig_amt = parse_amount(r.get("Orig. amount"))
        foreign = None
        if orig_cur and orig_cur != "SEK" and orig_amt is not None:
            foreign = {"amount": abs(orig_amt), "currency": orig_cur}
        rows.append({
            "date": iso(r.get("Date")),
            "receipt_no": receipt,
            "expense_id": str(r.get("Expense ID") or "").strip(),
            "type": etype,
            "merchant": norm_ws(r.get("Source description")),
            "amount_sek": r2(abs(parse_amount(r.get("Amount")) or 0)),
            "currency": str(r.get("Currency") or "SEK").strip() or "SEK",
            "foreign": foreign,
            "export_status": str(r.get("Export Status") or "").strip(),
            "has_receipt": bool(urls) or receipt in by_number,
            # reimbursement (payout) rows never carry a receipt; only card purchases can lack one
            "payout": bool(etype) and "card" not in etype.lower(),
        })
    stamp = file_date(export_dir) or mtime_date(csvs[0])
    return {"dir": export_dir, "rel": rel(export_dir, root), "date": stamp, "rows": rows,
            "files": len(files), "shas": shas, "same_file_groups": groups,
            "by_receipt": {r["receipt_no"]: r for r in rows if r["receipt_no"]},
            "by_expense": {r["expense_id"]: r for r in rows if r["expense_id"]}}


def pleo_months(export):
    """Per-month Pleo figures newest first plus totals; empty when there is no export. Rows
    without a receipt split into card purchases (missing, missing_sek) and payouts
    (payouts, payouts_sek), which never carry a receipt."""
    empty = {"rows": 0, "with_receipt": 0, "missing": 0, "amount_sek": 0.0, "missing_sek": 0.0,
             "payouts": 0, "payouts_sek": 0.0}
    if not export:
        return [], empty
    per = OrderedDict()
    for r in export["rows"]:
        m = month_of(r["date"]) or "okänd"
        x = per.setdefault(m, {"month": m, "rows": 0, "with_receipt": 0, "missing": 0, "amount_sek": 0.0,
                               "missing_sek": 0.0, "payouts": 0, "payouts_sek": 0.0,
                               "exported": 0, "queued": 0, "not_exported": 0})
        x["rows"] += 1
        x["amount_sek"] += r["amount_sek"]
        if r["has_receipt"]:
            x["with_receipt"] += 1
        elif r.get("payout"):
            x["payouts"] += 1
            x["payouts_sek"] += r["amount_sek"]
        else:
            x["missing"] += 1
            x["missing_sek"] += r["amount_sek"]
        st = r["export_status"].upper()
        if st == "EXPORTED":
            x["exported"] += 1
        elif st == "QUEUED":
            x["queued"] += 1
        else:
            x["not_exported"] += 1
    months = sorted(per.values(), key=lambda x: x["month"], reverse=True)
    for x in months:
        x["amount_sek"] = r2(x["amount_sek"])
        x["missing_sek"] = r2(x["missing_sek"])
        x["payouts_sek"] = r2(x["payouts_sek"])
    totals = {"rows": sum(x["rows"] for x in months),
              "with_receipt": sum(x["with_receipt"] for x in months),
              "missing": sum(x["missing"] for x in months),
              "amount_sek": r2(sum(x["amount_sek"] for x in months)),
              "missing_sek": r2(sum(x["missing_sek"] for x in months)),
              "payouts": sum(x["payouts"] for x in months),
              "payouts_sek": r2(sum(x["payouts_sek"] for x in months))}
    return months, totals


# ----------------------------------------------------------------------------- live missing list

def route_for(merchant, expense_type, amount_sek=None, foreign=None):
    """(route, hint) for a Pleo row without a receipt: Kivra rows always 'kivra', out-of-pocket
    rows 'pocket', an alias vendor's route_if_none (portal or request -> 'portal', ask -> 'ask
    Nicolina') with its portal text as the hint, else 'gmail'."""
    m = norm_ws(merchant).upper()
    key, v = alias_vendor(merchant, amount_sek, foreign)
    portal = norm_ws((v or {}).get("portal"))
    if "KIVRA" in m:
        return "kivra", KIVRA_HINT
    if "out of pocket" in (expense_type or "").lower():
        return "pocket", portal or "kvittot läggs på utlägget i Pleo"
    rin = str((v or {}).get("route_if_none") or "none")
    if rin in ("portal", "request"):
        return "portal", portal or "leverantörens portal"
    if rin == "ask":
        return "ask Nicolina", portal or "fråga Nicolina"
    return "gmail", GMAIL_HINT


def route_label(route):
    return ROUTE_LABELS.get(route, route)


def route_owner(route):
    if route in ROUTE_OWNER_OSCAR:
        return "Oscar"
    if route == "ask Nicolina":
        return "Nicolina"
    return "systemet"


def load_missing_live(root, export, attached_by_expense=None):
    """The newest out/pleo-missing-receipts-*.csv as {file, date, rows, by_route}; rows [] and
    file None without one. Both column layouts parse; the export fills in what the csv lacks.
    The format-b route strings that all mean "attach through the Pleo MCP" share the route
    key "mcp" so the list groups them once. A row whose expense id has an archive page with
    pleo_status "attached ..." (attached_by_expense: expense id -> that status) keeps its place
    in rows under the route "attached" but is no work: it stays out of by_route."""
    path = newest(Path(root).glob("out/pleo-missing-receipts-*.csv"))
    out = {"file": None, "date": None, "rows": [], "by_route": []}
    if path is None:
        return out
    out["file"] = rel(path, root)
    out["date"] = file_date(path) or mtime_date(path)
    by_receipt = (export or {}).get("by_receipt") or {}
    by_expense = (export or {}).get("by_expense") or {}
    attached_by_expense = attached_by_expense or {}
    routes = OrderedDict()
    for r in read_csv(path):
        receipt_no = r.get("pleo_receipt_no") or r.get("receipt_no") or r.get("receipt") or ""
        expense_id = r.get("expense_id") or ""
        exp = by_expense.get(expense_id) or by_receipt.get(receipt_no) or {}
        expense_id = expense_id or exp.get("expense_id") or ""
        merchant = norm_ws(r.get("merchant") or exp.get("merchant"))
        amount = parse_amount(r.get("amount_sek") if r.get("amount_sek") not in (None, "") else r.get("amount"))
        if amount is None:
            amount = exp.get("amount_sek")
        amount = r2(abs(amount or 0))
        etype = r.get("type") or exp.get("type") or ""
        status = r.get("export_status") or exp.get("export_status") or ""
        foreign = None
        cur = str(r.get("orig_currency") or "").strip().upper()
        oa = parse_amount(r.get("orig_amount"))
        if cur and cur != "SEK" and oa is not None:
            foreign = {"amount": abs(oa), "currency": cur}
        elif exp.get("foreign"):
            foreign = exp["foreign"]
        derived, hint = route_for(merchant, etype, amount, foreign)
        route = norm_ws(r.get("route")) if "route" in r else ""
        if not route or derived == "kivra":
            route = derived
        elif norm_ws(r.get("action")):
            hint = norm_ws(r.get("action"))
        if route.lower() in MCP_ROUTES:
            route = MCP_ROUTE
        attached = attached_by_expense.get(expense_id) if expense_id else None
        if attached:
            route = "attached"
            hint = f"{attached} enligt arkivet, syns i nästa Pleo-export"
        vendor = vendor_name(merchant, foreign["amount"] if foreign else amount, foreign["currency"] if foreign else "SEK")
        row = {"receipt_no": receipt_no, "expense_id": expense_id, "date": iso(r.get("date")) or exp.get("date"),
               "merchant": merchant, "vendor": vendor, "amount_sek": amount, "type": etype,
               "export_status": status, "route": route, "route_label": route_label(route), "hint": hint}
        out["rows"].append(row)
        if route == "attached":
            continue
        g = routes.setdefault(route, {"route": route, "route_label": route_label(route), "count": 0,
                                      "amount_sek": 0.0, "owner": route_owner(route)})
        g["count"] += 1
        g["amount_sek"] += amount
    for g in routes.values():
        g["amount_sek"] = r2(g["amount_sek"])
    out["by_route"] = list(routes.values())
    return out


# ----------------------------------------------------------------------------- attachment-error worklist

ITEM_RE = re.compile(r"^- (\d{7})\s+(?:(\d{4}-\d{2}-\d{2})\s+)?(.+)$")
HEADING_RE = re.compile(r"^(\d+)\. (.+)$")
STATUS_TOKEN_RE = re.compile(r"\s*\((EXPORTED|QUEUED|NOT_EXPORTED)\)\s*$")


def parse_worklist(text):
    """(baseline export date or None, [(heading, [(receipt_no, date, problem)])]) from the txt."""
    m = re.search(r"Pleo-exporten (\d{4}-\d{2}-\d{2})", text)
    baseline = m.group(1) if m else None
    groups = []
    for line in text.splitlines():
        line = line.rstrip()
        h = HEADING_RE.match(line)
        if h:
            groups.append((h.group(2).strip(), []))
            continue
        it = ITEM_RE.match(line)
        if it and groups:
            chunks = [c for c in re.split(r"\s{2,}", it.group(3).strip()) if c]
            problem = " ".join(chunks[1:]) if len(chunks) > 1 else ""
            problem = STATUS_TOKEN_RE.sub("", problem).strip()
            groups[-1][1].append((it.group(1), it.group(2), problem))
    return baseline, groups


def load_worklist(root, exports, newest_export, warn_receipts):
    """The newest out/pleo-kvittofel-*.txt as the flagged section: groups with items whose status
    compares the baseline export named in the file with the newest export: a different file
    in the newer export is a refile, a row without any file (detached, nothing attached yet)
    stays open, a receipt number missing from the newer export is gone."""
    path = newest(Path(root).glob("out/pleo-kvittofel-*.txt"))
    out = {"file": None, "date": None, "baseline_export": None, "compared_export": None, "groups": []}
    if path is None:
        return out
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    out["file"] = rel(path, root)
    out["date"] = file_date(path) or mtime_date(path)
    baseline_date, groups = parse_worklist(text)
    baseline = None
    if baseline_date:
        for e in exports:
            if e["date"] == baseline_date:
                baseline = e
    if baseline is None:
        baseline = newest_export
    compared = None
    if baseline is not None and newest_export is not None and newest_export["dir"] != baseline["dir"] \
            and (newest_export["date"] or "") > (baseline["date"] or ""):
        compared = newest_export
    out["baseline_export"] = baseline["rel"] if baseline else (f"data/pleo/expenses_{baseline_date}" if baseline_date else None)
    out["compared_export"] = compared["rel"] if compared else None
    lookup_new = (compared or baseline or {}).get("by_receipt") or {}
    lookup_base = (baseline or {}).get("by_receipt") or {}
    keyed = len(groups) == len(GROUP_KEYS)
    for i, (heading, items) in enumerate(groups):
        key = GROUP_KEYS[i] if keyed else f"group-{i + 1}"
        label = GROUP_LABELS[key] if keyed else heading
        g = {"key": key, "label": label, "count": 0, "open": 0, "refiled": 0, "gone": 0, "items": []}
        for receipt_no, d, problem in items:
            exp = lookup_new.get(receipt_no) or lookup_base.get(receipt_no) or {}
            if compared is None:
                status = "open"
            elif receipt_no not in compared["by_receipt"]:
                status = "gone"
            elif compared["shas"].get(receipt_no) and \
                    compared["shas"].get(receipt_no) != baseline["shas"].get(receipt_no, []):
                status = "refiled"
            else:
                status = "open"
            merchant = exp.get("merchant") or ""
            foreign = exp.get("foreign")
            amount = exp.get("amount_sek")
            vendor = vendor_name(merchant, foreign["amount"] if foreign else amount,
                                 foreign["currency"] if foreign else "SEK") if merchant else ""
            g["items"].append({"receipt_no": receipt_no, "date": d or exp.get("date"), "merchant": merchant,
                               "vendor": vendor, "amount_sek": r2(amount) if amount is not None else None,
                               "export_status": exp.get("export_status") or "", "expense_id": exp.get("expense_id") or "",
                               "problem": problem, "status": status, "status_label": STATUS_LABELS[status],
                               "check_still_warns": receipt_no in warn_receipts})
            g["count"] += 1
            g[status] += 1
        out["groups"].append(g)
    return out


# ----------------------------------------------------------------------------- Amex ledger, receipts, plans

def load_ledger(root):
    """Newest classified Amex ledger (classified2 first) as {file, rel, date, rows}; None without one."""
    paths = sorted(Path(root).glob("out/amex-*.classified2.csv")) or sorted(Path(root).glob("out/amex-*.classified.csv"))
    if not paths:
        return None
    path = max(paths, key=lambda p: (os.path.getmtime(p), p.name))
    rows = []
    for r in read_csv(path):
        d = iso(r.get("date"))
        if not d:
            continue
        rows.append({"ref": r.get("ref") or "", "date": d, "month": d[:7], "card": r.get("card") or "",
                     "account": r.get("account") or "", "amount_sek": r2(abs(parse_amount(r.get("amount")) or 0)),
                     "merchant": norm_ws(r.get("merchant")), "tag": (r.get("tag") or "").strip().lower(),
                     "entity": (r.get("entity") or ENTITY).strip().lower() or ENTITY,
                     "bas_account": r.get("bas_account") or "", "vat_regime": r.get("vat_regime") or ""})
    return {"file": path, "rel": rel(path, root), "date": mtime_date(path), "rows": rows}


def load_overrides(root):
    path = Path(root) / "data" / "tags" / "overrides.csv"
    if not path.is_file():
        return set()
    return {r.get("ref") for r in read_csv(path) if r.get("ref")}


def load_needs_tag(root, overrides):
    """(file rel or None, rows) from the newest out/amex-needs-tag-*.csv."""
    path = newest(Path(root).glob("out/amex-needs-tag-*.csv"))
    if path is None:
        return None, []
    rows = []
    for r in read_csv(path):
        rows.append({"ref": r.get("ref") or "", "date": iso(r.get("date")), "merchant": norm_ws(r.get("merchant")),
                     "amount_sek": r2(abs(parse_amount(r.get("amount")) or 0)), "card": card_label(r.get("card")),
                     "why": r.get("why") or "", "answered": (r.get("ref") or "") in overrides})
    return rel(path, root), rows


def amex_csv_coverage(root):
    """(number of data/amex/activity*.csv files, max Datum as ISO or None). The file dates are
    not consulted: data/ is copied between machines, so only the purchases inside count."""
    paths = sorted(Path(root).glob("data/amex/activity*.csv"))
    end = None
    for p in paths:
        for r in read_csv(p):
            d = iso(r.get("Datum"))
            if d and (end is None or d > end):
                end = d
    return len(paths), end


def load_receipt_maps(root):
    """{ref: file path} over out/receipts-amex/map-*.csv, files that exist only."""
    found = {}
    for p in sorted(Path(root).glob("out/receipts-amex/map-*.csv")):
        for r in read_csv(p):
            f = r.get("file") or ""
            if not r.get("ref") or not f:
                continue
            fp = Path(f) if os.path.isabs(f) else Path(root) / f
            if fp.is_file():
                found[r["ref"]] = fp
    return found


def load_plans(root):
    """{month: plan dict} for every out/fortnox-run-<YYYY-MM>/ with a manifest or plan.md."""
    plans = {}
    for d in sorted(Path(root).glob("out/fortnox-run-*")):
        m = re.fullmatch(r"fortnox-run-(\d{4}-\d{2})", d.name)
        if not m or not d.is_dir():
            continue
        manifest = read_csv(d / "manifest.csv") if (d / "manifest.csv").is_file() else []
        plan_md = d / "plan.md"
        if not manifest and not plan_md.is_file():
            continue
        # the manifest lists excluded rows as well; a voucher row is one with a payload and an
        # excluded row carries its reason in status ("excluded bas_account saknas")
        excluded = {}
        if any(r.get("voucher_payload") for r in manifest):
            for r in manifest:
                if r.get("ref") and not r.get("voucher_payload"):
                    excluded[r["ref"]] = re.sub(r"^excluded\s*", "", r.get("status") or "").strip() or "utan angiven orsak"
            manifest = [r for r in manifest if r.get("voucher_payload")]
        state = read_json(d / "execution.json") or {}
        rows = state.get("rows") if isinstance(state.get("rows"), dict) else {}
        vouchers = {}
        for ref, st in rows.items():
            if isinstance(st, dict) and st.get("voucher_number"):
                vouchers[ref] = f"{st.get('voucher_series') or ''} {st['voucher_number']}".strip()
        ups = read_json(d / "uploads.json") or {}
        uploads = [u for u in (ups.get("uploads") or []) if isinstance(u, dict) and u.get("inbox_file_id")] \
            if isinstance(ups, dict) else []
        warnings = 0
        if plan_md.is_file():
            try:
                section = False
                for line in plan_md.read_text(encoding="utf-8", errors="replace").splitlines():
                    if line.startswith("## "):
                        section = line.strip().lower() == "## varningar"
                        continue
                    if section and line.startswith("- "):
                        warnings += 1
            except OSError:
                pass
        plans[m.group(1)] = {
            "dir": rel(d, root), "built": mtime_date(plan_md if plan_md.is_file() else d / "manifest.csv"),
            "vouchers": len(manifest), "receipts": sum(1 for r in manifest if r.get("receipt_file")),
            "uploaded": len(uploads), "booked": len(vouchers), "total_sek": r2(sum(abs(parse_amount(r.get("amount")) or 0) for r in manifest)),
            "warnings": warnings,
            "_refs": {r.get("ref") for r in manifest if r.get("ref")}, "_vouchers": vouchers,
            "_uploaded": {u.get("ref") for u in uploads}, "_excluded": excluded,
        }
    return plans


def stage_for(month, covered, ledger_covered, needs_tag_open, business, plan):
    """Stage key for a ledger month: no-csv until an export closes the month, no-plan while the
    ledger does not reach it or has business rows without a plan, nothing-to-book when the
    month holds no business rows and no plan, then untagged, planned, partial, booked by the
    plan's numbers."""
    if covered is None or month > covered:
        return "no-csv"
    if ledger_covered is None or month > ledger_covered:
        return "no-plan"
    if needs_tag_open > 0:
        return "untagged"
    if plan is None:
        return "no-plan" if business > 0 else "nothing-to-book"
    if plan["booked"] <= 0:
        return "planned"
    if plan["booked"] < plan["vouchers"]:
        return "partial"
    return "booked"


def build_amex(root, ledger, needs_tag_file, needs_tag_rows, overrides, csv_cov, plans, receipt_map, arc_by_ref):
    """The amex section plus the private figures the summary and todo rules need. csv_cov is
    amex_csv_coverage's pair; the ledger's coverage is its rows' last date (or the window end
    in its file name), closing the same month as the exports it was built from. A business
    row's status_label names what still blocks it: a missing receipt, a missing BAS account,
    the manifest reason when a plan exists without it, the plan state otherwise."""
    csv_files, csv_end = csv_cov
    rows = ledger["rows"] if ledger else []
    ledger_end = max((r["date"] for r in rows), default=None)
    if ledger:
        m = re.search(r"_(\d{4}-\d{2}-\d{2})\.classified", ledger["file"].name)
        if m and (ledger_end is None or m.group(1) > ledger_end):
            ledger_end = m.group(1)
    coverage_end = csv_end or ledger_end
    csv_covered = last_full_month(csv_end) if csv_end else None
    ledger_covered = last_full_month(ledger_end) if ledger_end else None
    if ledger_end and csv_end == ledger_end and csv_covered:
        ledger_covered = csv_covered
    next_month = month_add(ledger_covered, 1) if ledger_covered else None
    csv_present = bool(next_month and csv_covered and csv_covered >= next_month)
    covered = csv_covered or ledger_covered
    public_plans = {m: {k: v for k, v in p.items() if not k.startswith("_")} for m, p in plans.items()}

    business_rows = []
    for r in rows:
        if r["tag"] != "business":
            continue
        plan = plans.get(r["month"])
        rec = arc_by_ref.get(r["ref"])
        rfile = receipt_map.get(r["ref"])
        receipt = rfile is not None or rec is not None
        voucher = (plan or {}).get("_vouchers", {}).get(r["ref"]) or (rec.get("fortnox_voucher") if rec else None) or None
        in_plan = bool(plan and r["ref"] in plan["_refs"])
        if voucher:
            status = "bokförd"
        elif r["entity"] != ENTITY:
            status = f"{archive.ENTITY_NAMES.get(r['entity'], r['entity'])}, utanför Viseo-planen"
        elif plan and not in_plan:
            status = f"utanför planen: {plan['_excluded'].get(r['ref']) or 'saknas i manifestet'}"
        elif not r["bas_account"]:
            status = "konto saknas" if receipt else "kvitto och konto saknas"
        elif not receipt:
            status = "kvitto saknas"
        elif in_plan and plan and r["ref"] in plan["_uploaded"]:
            status = "plan klar, kvitto uppladdat"
        elif in_plan:
            status = "plan klar"
        else:
            status = "kvitto funnet, plan saknas"
        business_rows.append({
            "ref": r["ref"], "date": r["date"], "month": r["month"], "merchant": r["merchant"],
            "vendor": vendor_name(r["merchant"], r["amount_sek"], "SEK"), "amount_sek": r["amount_sek"],
            "card": card_label(r["card"]), "entity": r["entity"], "bas_account": r["bas_account"],
            "vat_regime": r["vat_regime"], "receipt": receipt,
            "receipt_file": rel(rfile, root) if rfile is not None else None,
            "archive_id": rec.get("id") if rec else None, "voucher": str(voucher) if voucher else None,
            "status_label": status,
        })
    business_rows.sort(key=lambda x: (x["date"], x["ref"]), reverse=True)
    receipt_by_ref = {b["ref"]: b["receipt"] for b in business_rows}

    months = []
    for m in sorted({r["month"] for r in rows} | set(plans), reverse=True):
        mrows = [r for r in rows if r["month"] == m]
        biz = [r for r in mrows if r["tag"] == "business"]
        nt = [r for r in mrows if r["tag"] == "needs-tag"]
        nt_open = [r for r in nt if r["ref"] not in overrides]
        found = sum(1 for r in biz if receipt_by_ref.get(r["ref"]))
        plan = public_plans.get(m)
        stage = stage_for(m, covered, ledger_covered, len(nt_open), len(biz), plan)
        months.append({
            "month": m, "rows": len(mrows), "business": len(biz), "business_sek": r2(sum(r["amount_sek"] for r in biz)),
            "business_other_entity": sum(1 for r in biz if r["entity"] != ENTITY),
            "personal": sum(1 for r in mrows if r["tag"] == "personal"), "needs_tag": len(nt),
            "needs_tag_open": len(nt_open), "skip": sum(1 for r in mrows if r["tag"] == "skip"),
            "receipts_found": found, "receipts_missing": len(biz) - found, "plan": plan,
            "stage": stage, "stage_label": STAGE_LABELS[stage],
            "_business_viseo": sum(1 for r in biz if r["entity"] == ENTITY),
            "_business_viseo_sek": r2(sum(r["amount_sek"] for r in biz if r["entity"] == ENTITY)),
        })
    section = {
        "ledger_file": ledger["rel"] if ledger else None, "ledger_date": ledger["date"] if ledger else None,
        "coverage_end": coverage_end, "months": [{k: v for k, v in x.items() if not k.startswith("_")} for x in months],
        "needs_tag": needs_tag_rows, "business_rows": business_rows,
        "next_month": next_month, "next_month_csv_present": csv_present,
    }
    private = {"months": months, "ledger_end": ledger_end, "ledger_covered": ledger_covered,
               "csv_covered": csv_covered, "csv_files": csv_files, "csv_end": csv_end,
               "needs_tag_file": needs_tag_file,
               "needs_tag_open": sum(x["needs_tag_open"] for x in months) if ledger
               else sum(1 for r in needs_tag_rows if not r["answered"]),
               "needs_tag_open_sek": r2(sum(r["amount_sek"] for r in needs_tag_rows if not r["answered"])) if needs_tag_rows
               else r2(sum(r["amount_sek"] for r in rows if r["tag"] == "needs-tag" and r["ref"] not in overrides))}
    return section, private


# ----------------------------------------------------------------------------- archive

def warning_kind(text):
    for needle, key, _label in WARNING_KINDS:
        if needle in text:
            return key
    return "other"


def parse_warning(msg):
    """'warning: viseo/2026/<stem>.md: text' -> (page_id, month, text); page_id None without a path."""
    body = msg[len("warning: "):] if msg.startswith("warning: ") else msg
    m = re.match(r"^(\S+?/\S+?): (.+)$", body)
    if not m:
        return None, None, body
    stem = Path(m.group(1)).stem
    return stem, (stem[:7] if re.match(r"\d{4}-\d{2}", stem) else None), m.group(2)


def build_archive(archive_root, root):
    """The archive section plus private lookups (pages by amex_ref, warning receipt numbers)."""
    aroot = Path(archive_root)
    section = {"root": str(aroot), "pages": 0, "total_sek": 0.0, "updated": None, "previews": 0, "errors": [],
               "warnings_total": 0, "warning_kinds": [], "warnings": [], "months": [],
               "status": {"attached": 0, "in_pleo": 0, "no_status": 0, "fortnox_booked": 0, "fortnox_open": 0},
               "text_methods": {}}
    private = {"present": False, "by_ref": {}, "warn_receipts": set(), "receipt_by_id": {}, "attached_by_expense": {},
               "rel": rel(aroot, root)}
    if not aroot.is_dir():
        return section, private
    private["present"] = True
    arc = Archive(aroot)
    recs = arc.records()
    per = OrderedDict()
    methods = Counter()
    total = 0.0
    previews = 0
    st = section["status"]
    for rid, rec in recs.items():
        amount = float(parse_amount(rec.get("amount_sek")) or 0)
        if rec.get("kind") == "credit-note":
            amount = -amount
        total += amount
        m = month_of(rec.get("date")) or "okänd"
        x = per.setdefault(m, {"month": m, "pages": 0, "amount_sek": 0.0, "pleo": 0, "amex": 0, "attached": 0,
                               "in_pleo": 0, "warnings": 0})
        x["pages"] += 1
        x["amount_sek"] += amount
        lane = str(rec.get("lane") or "")
        if lane == "pleo":
            x["pleo"] += 1
        elif lane.startswith("amex"):
            x["amex"] += 1
        ps = str(rec.get("pleo_status") or "")
        if ps.startswith("attached"):
            x["attached"] += 1
            st["attached"] += 1
            if rec.get("pleo_expense_id"):
                private["attached_by_expense"][str(rec["pleo_expense_id"])] = ps
        elif ps.startswith("i Pleo"):
            x["in_pleo"] += 1
            st["in_pleo"] += 1
        elif not ps:
            st["no_status"] += 1
        fs = str(rec.get("fortnox_status") or "")
        if fs.startswith("bokfort") or fs.startswith("bokförd"):
            st["fortnox_booked"] += 1
        elif fs.startswith("ej"):
            st["fortnox_open"] += 1
        methods[str(rec.get("text_method") or "none")] += 1
        if rec.get("preview") and rec.dir is not None and (rec.dir / str(rec["preview"])).is_file():
            previews += 1
        if rec.get("amex_ref"):
            private["by_ref"].setdefault(str(rec["amex_ref"]), rec)
        if rec.get("pleo_receipt_number"):
            private["receipt_by_id"][rid] = str(rec["pleo_receipt_number"])
    msgs = arc.check()
    errors = [m for m in msgs if not m.startswith("warning: ")]
    warnings = []
    kinds = Counter()
    for m in msgs:
        if not m.startswith("warning: "):
            continue
        page_id, month, text = parse_warning(m)
        kind = warning_kind(text)
        kinds[kind] += 1
        warnings.append({"page_id": page_id, "month": month, "kind": kind, "text": text})
        if month in per:
            per[month]["warnings"] += 1
        rec = recs.get(page_id) if page_id else None
        if rec is not None and rec.get("pleo_receipt_number"):
            private["warn_receipts"].add(str(rec["pleo_receipt_number"]))
    updated = None
    index = aroot / "index.md"
    if index.is_file():
        try:
            meta, _body = archive.fm_load(index.read_text(encoding="utf-8"))
            updated = str(meta.get("updated")) if meta.get("updated") else None
        except (OSError, ValueError):
            updated = None
    months = sorted(per.values(), key=lambda x: x["month"], reverse=True)
    for x in months:
        x["amount_sek"] = r2(x["amount_sek"])
    section.update({
        "pages": len(recs), "total_sek": r2(total), "updated": updated, "previews": previews, "errors": errors,
        "warnings_total": len(warnings),
        "warning_kinds": [{"key": k, "label": WARNING_LABELS.get(k, k), "count": n}
                          for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))],
        "warnings": warnings, "months": months,
        "text_methods": dict(sorted(methods.items(), key=lambda kv: (-kv[1], kv[0]))),
    })
    return section, private


# ----------------------------------------------------------------------------- sources, summary, todo

def source(label, path, d, today, detail, state=None, stale_after=None):
    age = None
    dd = to_date(d)
    if dd is not None:
        age = (today - dd).days
    if state is None:
        state = "missing" if d is None and path is None else "ok"
        if state == "ok" and stale_after is not None and age is not None and age > stale_after:
            state = "stale"
    return {"label": label, "path": path, "date": d, "age_days": age, "detail": detail, "state": state}


def build_sources(today, export, pleo, worklist, amex_priv, amex_sec, ledger, plans, arc_sec, arc_priv):
    s = OrderedDict()
    if export:
        n_rows, n_files = len(export["rows"]), export["files"]
        s["pleo_export"] = source("Pleo-export", export["rel"], export["date"], today,
                                  f"{n_rows} {plural(n_rows, 'rad', 'rader')}, {n_files} {plural(n_files, 'kvittofil', 'kvittofiler')}",
                                  stale_after=STALE_DAYS)
    else:
        s["pleo_export"] = source("Pleo-export", None, None, today, "underlag saknas: ingen mapp data/pleo/expenses_*", "missing")
    ml = pleo["missing_live"]
    if ml["file"]:
        n_att = sum(1 for r in ml["rows"] if r["route"] == "attached")
        detail = f"{len(ml['rows'])} {plural(len(ml['rows']), 'rad', 'rader')}"
        if n_att:
            detail += f", {n_att} {plural(n_att, 'bifogad', 'bifogade')} sedan listan gjordes"
        s["pleo_missing"] = source("Pleo-rader utan kvitto (live)", ml["file"], ml["date"], today, detail, stale_after=STALE_DAYS)
    else:
        s["pleo_missing"] = source("Pleo-rader utan kvitto (live)", None, None, today,
                                   "underlag saknas: ingen out/pleo-missing-receipts-*.csv", "missing")
    if worklist["file"]:
        n = sum(g["count"] for g in worklist["groups"])
        ng = len(worklist["groups"])
        head = f"{n} {plural(n, 'post', 'poster')} i {ng} {plural(ng, 'grupp', 'grupper')}"
        base = worklist["baseline_export"] or ""
        base_date = file_date(base) if base else None
        if worklist["compared_export"]:
            cmp_date = file_date(worklist["compared_export"]) or worklist["compared_export"]
            detail = f"{head}, exporten {base_date or base} jämförd mot {cmp_date}"
        else:
            detail = f"{head}, jämförd mot exporten {base_date or base or 'okänd'}"
        s["worklist"] = source("Kvittofel i Pleo", worklist["file"], worklist["date"], today, detail)
    else:
        s["worklist"] = source("Kvittofel i Pleo", None, None, today, "underlag saknas: ingen out/pleo-kvittofel-*.txt", "missing")
    csv_files, csv_end, csv_covered = amex_priv["csv_files"], amex_priv["csv_end"], amex_priv["csv_covered"]
    if csv_files and csv_end:
        prev = month_add(today.strftime("%Y-%m"), -1)
        stale = (csv_covered or "") < prev
        detail = f"{csv_files} {plural(csv_files, 'fil', 'filer')}, sista köp {csv_end}"
        if stale:
            missing_from = month_add(csv_covered, 1) if csv_covered else prev
            detail += f", {sv_month(missing_from).split(' ')[0]} saknas"
        s["amex_csv"] = source("Amex-export", "data/amex", csv_end, today, detail, "stale" if stale else "ok")
    elif csv_files:
        s["amex_csv"] = source("Amex-export", "data/amex", None, today,
                               f"{csv_files} {plural(csv_files, 'fil', 'filer')} utan läsbar Datum-kolumn", "missing")
    else:
        s["amex_csv"] = source("Amex-export", None, None, today, "underlag saknas: inga data/amex/activity*.csv", "missing")
    if ledger:
        biz = sum(x["business"] for x in amex_priv["months"])
        nr, nt = len(ledger["rows"]), amex_priv["needs_tag_open"]
        s["amex_ledger"] = source("Amex-ledger (klassad)", ledger["rel"], ledger["date"], today,
                                  f"{nr} {plural(nr, 'rad', 'rader')}, {biz} företag, {nt} {plural(nt, 'otaggad', 'otaggade')}")
    else:
        s["amex_ledger"] = source("Amex-ledger (klassad)", None, None, today, "underlag saknas: ingen out/amex-*.classified2.csv", "missing")
    if plans:
        planned = sum(p["vouchers"] for p in plans.values())
        booked = sum(p["booked"] for p in plans.values())
        built = max((p["built"] or "" for p in plans.values()), default="") or None
        tail = "inget bokfört" if booked == 0 else f"{booked} av {planned} verifikat bokförda"
        s["fortnox_runs"] = source("Fortnox-planer", "out/fortnox-run-*", built, today,
                                   ", ".join(sv_month(m) for m in sorted(plans)) + ", " + tail)
    else:
        s["fortnox_runs"] = source("Fortnox-planer", None, None, today, "underlag saknas: ingen out/fortnox-run-*", "missing")
    if arc_priv["present"]:
        d = (arc_sec["updated"] or "")[:10] or None
        state = "stale" if arc_sec["errors"] else "ok"
        np_, nw = arc_sec["pages"], arc_sec["warnings_total"]
        s["archive"] = source("Kvittoarkivet", arc_priv["rel"], d, today,
                              f"{np_} {plural(np_, 'sida', 'sidor')}, {len(arc_sec['errors'])} fel, {nw} {plural(nw, 'varning', 'varningar')}", state)
    else:
        s["archive"] = source("Kvittoarkivet", arc_priv["rel"], None, today, "underlag saknas: arkivmappen finns inte", "missing")
    return s


def build_summary(pleo, amex_sec, amex_priv, plans, arc_sec):
    ml = pleo["missing_live"]
    open_live = [r for r in ml["rows"] if r["route"] != "attached"]
    flagged = pleo["flagged"]["groups"]
    return {
        "pleo_rows": pleo["totals"]["rows"], "pleo_with_receipt": pleo["totals"]["with_receipt"],
        "pleo_missing_export": pleo["totals"]["missing"] + pleo["totals"]["payouts"],
        "pleo_missing_live": len(open_live) if ml["file"] else None,
        "pleo_missing_live_sek": r2(sum(r["amount_sek"] for r in open_live)) if ml["file"] else None,
        "pleo_missing_live_attached": len(ml["rows"]) - len(open_live) if ml["file"] else None,
        "flagged_total": sum(g["count"] for g in flagged), "flagged_open": sum(g["open"] for g in flagged),
        "flagged_refiled": sum(g["refiled"] for g in flagged),
        "amex_business_rows": len(amex_sec["business_rows"]),
        "amex_business_sek": r2(sum(b["amount_sek"] for b in amex_sec["business_rows"])),
        "amex_receipts_found": sum(1 for b in amex_sec["business_rows"] if b["receipt"]),
        "amex_needs_tag_open": amex_priv["needs_tag_open"],
        "amex_vouchers_planned": sum(p["vouchers"] for p in plans.values()),
        "amex_vouchers_booked": sum(p["booked"] for p in plans.values()),
        "archive_pages": arc_sec["pages"], "archive_sek": arc_sec["total_sek"],
        "archive_errors": len(arc_sec["errors"]), "archive_warnings": arc_sec["warnings_total"],
        "next_amex_month": amex_sec["next_month"], "next_amex_csv_present": amex_sec["next_month_csv_present"],
    }


def todo_item(key, severity, owner, title, detail, count=None, amount_sek=None, command=None, link=None):
    return {"key": key, "severity": severity, "owner": owner, "title": title, "detail": detail, "count": count,
            "amount_sek": r2(amount_sek) if amount_sek is not None else None, "command": command, "link": link}


def build_todo(today, sources, pleo, amex_sec, amex_priv, plans, arc_sec, arc_priv):
    todo = []
    groups = pleo["flagged"]["groups"]
    open_items = [it for g in groups for it in g["items"] if it["status"] == "open"]
    if open_items:
        parts = []
        for g in groups:
            if g["open"]:
                one, many = GROUP_PHRASES.get(g["key"], ("{n} " + g["label"].lower(), "{n} " + g["label"].lower()))
                parts.append((one if g["open"] == 1 else many).format(n=g["open"]))
        detail = ", ".join(parts) + ". Ladda sedan ner en ny export."
        n = len(open_items)
        todo.append(todo_item("pleo-reattach", "crit", "Oscar", f"Bifoga rätt kvitto på {n} Pleo-{plural(n, 'utgift', 'utgifter')}", detail,
                              n, sum(it["amount_sek"] or 0 for it in open_items),
                              "scripts/archive.py ingest pleo-export data/pleo/expenses_<datum> && scripts/archive.py check", "pleo-fel"))
    for g in pleo["missing_live"]["by_route"]:
        n = g["count"]
        rows = [r for r in pleo["missing_live"]["rows"] if r["route"] == g["route"]]
        vendors = []
        for r in rows:
            v = r["vendor"] or r["merchant"]
            if v and v not in vendors:
                vendors.append(v)
        kvitton = plural(n, "kvitto", "kvitton")
        if g["route"] == "kivra":
            title = f"Hämta {n} Kivra-{kvitton} i Kivra-appen"
            detail = "Nicolina vill ha ett kvitto per avgift. Finns bara bakom BankID."
        elif g["route"] == "pocket":
            title = f"Lägg kvitto på {n} {plural(n, 'eget utlägg', 'egna utlägg')} i Pleo"
            detail = ", ".join(f"{r['vendor'] or r['merchant']} ({r['hint']})" if r["hint"] else (r["vendor"] or r["merchant"]) for r in rows) + "."
        elif g["route"] == "portal":
            title = f"Hämta {n} {kvitton} från {plural(n, 'leverantörsportalen', 'leverantörsportaler')}"
            counts = Counter(r["vendor"] or r["merchant"] for r in rows)
            detail = ", ".join(f"{v} {counts[v]}" for v in vendors if v in counts) + ". Var kvittot finns står i tabellen."
        elif g["route"] == "gmail":
            title = f"Sök {n} {kvitton} i Gmail och bifoga via MCP"
            detail = ", ".join(vendors) + "." if vendors else "Sök i Gmail, bifoga via Pleo MCP."
        elif g["route"] == MCP_ROUTE:
            title = f"Bifoga {n} {kvitton} via Pleo MCP"
            detail = ", ".join(vendors) + "." if vendors else "Kvittot finns redan, bifoga via Pleo MCP."
        elif g["route"] == "ask Nicolina":
            title = f"Fråga Nicolina om {n} Pleo-{plural(n, 'rad', 'rader')}"
            detail = ", ".join(vendors) + "." if vendors else ""
        else:
            title = f"{route_label(g['route'])}: {n} Pleo-{plural(n, 'rad', 'rader')} utan kvitto"
            detail = ", ".join(vendors) + "." if vendors else ""
        todo.append(todo_item(f"pleo-missing:{g['route']}", "warn", g["owner"], title, detail, n, g["amount_sek"], None, "pleo-saknas"))
    nm = amex_sec["next_month"]
    if nm and not amex_sec["next_month_csv_present"]:
        end = month_end(nm)
        # raised once the month is over, the same day the amex_csv source turns stale
        if end is not None and today > end:
            last = amex_priv["csv_end"]
            where = f"Senaste köp i data/amex är {last}." if last else "Inga filer i data/amex."
            todo.append(todo_item("amex-csv", "warn", "Oscar", f"Amex-CSV för {sv_month(nm)} saknas",
                                  f"{where} Exportera {sv_month(nm).split(' ')[0]} från Amex-portalen till data/amex/.", None, None,
                                  "scripts/amex_import.py data/amex/*.csv --out out/amex.normalized.json --csv out/amex.normalized.csv"
                                  f" && scripts/month_end.py --month {nm} --reason \"...\"", "amex"))
    if amex_priv["needs_tag_open"] > 0:
        n = amex_priv["needs_tag_open"]
        lst = amex_priv["needs_tag_file"] or amex_sec["ledger_file"] or "ledgern"
        todo.append(todo_item("amex-tags", "warn", "Oscar", f"Tagga {n} Amex-{plural(n, 'rad', 'rader')}",
                              f"Listan ligger i {lst}. Svaren skrivs till data/tags/overrides.csv (ref,tag,note).",
                              n, amex_priv["needs_tag_open_sek"], None, "amex-otaggat"))
    for m in sorted(plans, reverse=True):
        p = plans[m]
        if p["booked"] < p["vouchers"]:
            found = plural(p["vouchers"], "kvitto funnet", "kvitton funna")
            detail = f"{p['vouchers']} verifikat, {sv_amount(p['total_sek'])}, {p['receipts']} av {p['vouchers']} {found}."
            if p["booked"]:
                detail += f" {p['booked']} redan {plural(p['booked'], 'bokfört', 'bokförda')}."
            detail += " Dry-run visar koden, du lämnar den tillbaka."
            todo.append(todo_item(f"fortnox-execute:{m}", "info", "Oscar", f"Godkänn Fortnox-planen för {sv_month(m)}", detail,
                                  p["vouchers"], p["total_sek"],
                                  f"scripts/fortnox_execute.py out/fortnox-run-{m} && scripts/archive.py sync-fortnox out/fortnox-run-{m}", "amex"))
    ledger_covered, csv_covered = amex_priv["ledger_covered"], amex_priv["csv_covered"]
    if csv_covered and (ledger_covered or "") < csv_covered:
        m = month_add(ledger_covered, 1) if ledger_covered else csv_covered
        while m <= csv_covered:
            todo.append(todo_item(f"amex-month-end:{m}", "info", "systemet", f"Kör månadsavslutet för {sv_month(m)}",
                                  f"Amex-CSV:n täcker {sv_month(m)} men ledgern slutar {amex_priv['ledger_end'] or 'ingenstans'}.",
                                  None, None, "scripts/amex_import.py data/amex/*.csv --out out/amex.normalized.json --csv out/amex.normalized.csv"
                                  f" && scripts/month_end.py --month {m} --reason \"...\"", "amex"))
            m = month_add(m, 1)
    for x in amex_priv["months"]:
        if x["plan"] is None and x["_business_viseo"] > 0 and ledger_covered and x["month"] <= ledger_covered:
            n, sek = x["_business_viseo"], x["_business_viseo_sek"]
            todo.append(todo_item(f"fortnox-plan-missing:{x['month']}", "info", "systemet", f"Ingen Fortnox-plan för {sv_month(x['month'])}",
                                  f"{n} {plural(n, 'företagsrad', 'företagsrader')} för {ENTITY_NAME}, {sv_amount(sek)}. Planen byggs av month_end.py.",
                                  n, sek, f"scripts/month_end.py --month {x['month']} --reason \"...\"", "amex"))
    pe = sources["pleo_export"]
    if pe["age_days"] is not None and pe["age_days"] > STALE_DAYS:
        age = pe["age_days"]
        todo.append(todo_item("pleo-export-stale", "info", "Oscar", f"Pleo-exporten är {age} {plural(age, 'dag', 'dagar')} gammal",
                              "Ladda ner en ny från Pleo (Export, Download) till data/pleo/ så arkivet och tavlan följer med.",
                              None, None, "scripts/archive.py ingest pleo-export data/pleo/expenses_<datum>", "underlag"))
    flagged_numbers = {it["receipt_no"] for g in groups for it in g["items"]}
    receipt_by_id = arc_priv["receipt_by_id"]

    def on_worklist(w):
        """True when the warning's page, or the partner page of a duplicate-invoice pair, is a
        flagged receipt: the pair is one finding and Oscar already has it on the list."""
        if receipt_by_id.get(w["page_id"]) in flagged_numbers:
            return True
        if w["kind"] == "duplicate-invoice":
            m = re.search(r"is also on (\S+)", w["text"])
            return bool(m) and receipt_by_id.get(m.group(1)) in flagged_numbers
        return False

    review = [w for w in arc_sec["warnings"] if not on_worklist(w)]
    if review:
        kinds = Counter(w["kind"] for w in review)
        detail = ", ".join(f"{n} {WARNING_LABELS.get(k, k)}" for k, n in sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0]))) + "."
        n = len(review)
        todo.append(todo_item("archive-review", "info", "Oscar", f"Titta på {n} {plural(n, 'arkivvarning', 'arkivvarningar')} utanför kvittofel-listan",
                              detail, n, None, "scripts/archive.py check", "arkiv"))
    if arc_sec["errors"]:
        n = len(arc_sec["errors"])
        detail = "; ".join(arc_sec["errors"][:3]) + (" ..." if n > 3 else "")
        todo.append(todo_item("archive-errors", "crit", "systemet", f"{n} fel i kvittoarkivet", detail, n, None,
                              "scripts/archive.py check", "arkiv"))
    # severity first, then the larger count, then the larger amount (a 59 426,85 SEK single row
    # outranks a 129,00 SEK single row)
    todo.sort(key=lambda t: (SEVERITY_RANK.get(t["severity"], 9),
                             -(t["count"] if t["count"] is not None else -1),
                             -(t["amount_sek"] or 0)))
    return todo


# ----------------------------------------------------------------------------- the model

def build_model(root=None, today=None, archive_root=None):
    """The dashboard model for the repo at root (default: the parent of scripts/), dated today
    (date or 'YYYY-MM-DD', default date.today()), with the archive at archive_root (default
    <root>/archive). Every key is present whatever inputs exist; JSON-serialisable."""
    root = Path(root or REPO).resolve()
    if isinstance(today, str):
        today = to_date(today) or date_type.today()
    today = today or date_type.today()
    archive_root = Path(archive_root) if archive_root else root / "archive"

    exports = [e for e in (load_pleo_export(root, d) for d in pleo_export_dirs(root)) if e]
    export = exports[-1] if exports else None
    arc_sec, arc_priv = build_archive(archive_root, root)
    months, totals = pleo_months(export)
    missing_live = load_missing_live(root, export, arc_priv["attached_by_expense"])
    flagged = load_worklist(root, exports, export, arc_priv["warn_receipts"])
    pleo = {"export_dir": export["rel"] if export else None, "export_date": export["date"] if export else None,
            "months": months, "totals": totals, "missing_live": missing_live, "flagged": flagged,
            "same_file_groups_now": export["same_file_groups"] if export else []}

    ledger = load_ledger(root)
    overrides = load_overrides(root)
    needs_tag_file, needs_tag_rows = load_needs_tag(root, overrides)
    plans = load_plans(root)
    receipt_map = load_receipt_maps(root)
    amex_sec, amex_priv = build_amex(root, ledger, needs_tag_file, needs_tag_rows, overrides, amex_csv_coverage(root),
                                     plans, receipt_map, arc_priv["by_ref"])
    public_plans = {m: {k: v for k, v in p.items() if not k.startswith("_")} for m, p in plans.items()}

    sources = build_sources(today, export, pleo, flagged, amex_priv, amex_sec, ledger, public_plans, arc_sec, arc_priv)
    summary = build_summary(pleo, amex_sec, amex_priv, public_plans, arc_sec)
    todo = build_todo(today, sources, pleo, amex_sec, amex_priv, public_plans, arc_sec, arc_priv)
    return {
        "generated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "today": today.isoformat(),
        "entity": ENTITY,
        "entity_name": ENTITY_NAME,
        "root": str(root),
        "sources": dict(sources),
        "summary": summary,
        "todo": todo,
        "pleo": pleo,
        "amex": amex_sec,
        "archive": arc_sec,
    }


# ----------------------------------------------------------------------------- CLI

def main(argv=None):
    ap = argparse.ArgumentParser(description="Kvittotavlan: build the receipts progress page (or its JSON model).")
    ap.add_argument("--root", default=str(REPO), help="repo root (default: the parent of scripts/)")
    ap.add_argument("--today", default=None, help="YYYY-MM-DD used for ages and the next Amex month (default: today)")
    ap.add_argument("--out", default=None, help="HTML output path (default: <root>/out/dashboard.html)")
    ap.add_argument("--json", action="store_true", help="print the model as JSON to stdout instead of writing HTML")
    ap.add_argument("--archive", default=None, help="archive root (default: <root>/archive)")
    args = ap.parse_args(argv)
    today = None
    if args.today:
        today = to_date(args.today)
        if today is None:
            print(f"error: --today must be YYYY-MM-DD, got {args.today!r}", file=sys.stderr)
            return 2
    model = build_model(args.root, today, args.archive)
    if args.json:
        json.dump(model, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
        return 0
    html = render(model)
    out = Path(args.out) if args.out else Path(args.root).resolve() / "out" / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    top = model["todo"][0]["title"] if model["todo"] else "inget att göra just nu"
    print(f"skrev {out} ({len(html.encode('utf-8')) // 1024} kB); {len(model['todo'])} punkter att göra, överst: {top}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
