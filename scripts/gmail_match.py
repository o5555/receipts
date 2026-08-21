#!/usr/bin/env python3
"""Read-only Gmail receipt matcher for the receipts ledgers.

Takes a ledger of charges (Pleo or Amex, several formats), searches Oscar's aggregated
mailbox for the receipt email of each charge, scores candidates by amount, date, sender,
subject and attachment, and writes a worklist with a recommended route (forward, attach,
pack, portal, none). It never sends, forwards, downloads attachments, or writes to Pleo
or Fortnox. Mail transport is Thor's gmail_dwd_read.py (read-only scope, audit log).

Usage:
  scripts/gmail_match.py WORKLIST --reason "..." [--out out/gmail-matches.json] [--csv out/gmail-matches.csv]
  scripts/gmail_match.py --check out/receipts/MATCHING.csv --reason "..."
  scripts/gmail_match.py WORKLIST --check FILE --reason "..." --run-date 2026-08-21
  scripts/gmail_match.py WORKLIST --reason "..." --dry-run      # print queries, no API call
  scripts/gmail_match.py WORKLIST --reason "..." --entity 5555  # a 5555 Media AB export (default viseo)

Default output and cache paths are under the repository's out/ folder whatever the current directory is;
relative paths given on the command line are taken from the current directory.
The Pleo company (and so the address to forward from) comes from the worklist via --entity, never from a
vendor alias. Card last-four digits are never read from email text; matching is by account, amount and date.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import os
import re
import sys
import time
import zoneinfo
from datetime import date, datetime, timedelta, timezone
from email.utils import parseaddr
from pathlib import Path
from urllib.parse import quote

sys.path.insert(0, "/Users/odin/thor/projects/email-access")
import gmail_dwd_read as g  # noqa: E402

MAILBOX = "oscar.sandstrom@viseo.se"
TZ = zoneinfo.ZoneInfo("Europe/Stockholm")
FORWARD_TO = "forward@fetch.pleo.io"
ROOT = Path(__file__).resolve().parents[1]
# Pleo Fetch attributes a forwarded mail to the company of the sender address. The identity is a property of the
# worklist (which company's Pleo export it is), selected with --entity. Every Viseo forward on record was sent from
# oscar.sandstrom@viseo.se and Pleo answered those, so that is the Viseo identity until Oscar says otherwise.
FORWARD_FROM = {"viseo": "oscar.sandstrom@viseo.se", "5555": "oscar@5555.media"}
ENTITY_NAMES = {"viseo": "Viseo AB", "5555": "5555 Media AB"}
# Input text that says the receipt already went to Fetch: never instruct a second forward.
FORWARDED_HINT_RE = re.compile(r"confirm in app|delvis kvittomatchning|already forwarded|forwarded (today|yesterday|on \d|\d{4}-\d{2}-\d{2})", re.I)
OPEN_WINDOW_TTL = timedelta(hours=6)  # a list fetched while its window was still open is reused only this long
SCRIPT_VERSION = 2
EXTRACT_VERSION = 2  # bump when extract_full changes; cached full extracts with another version are refetched
CARD_TO_ACCOUNT = {"1022": "-61022", "2004": "-62004", "3003": "-13003", "1006": "-61006"}
CURRENCY_NAMES = {
    "UNITED STATES DOLLAR": "USD", "EUROPEAN UNION EURO": "EUR", "POUND STERLING": "GBP",
    "DANISH KRONE": "DKK", "NORWEGIAN KRONE": "NOK", "SWISS FRANC": "CHF", "JAPANESE YEN": "JPY",
}
ISO_CURRENCIES = {"SEK", "USD", "EUR", "GBP", "DKK", "NOK", "CHF", "JPY"}
FAIL_TERMS = ("failed", "could not", "declined", "misslyckades")
KNOWN_ID_RE = re.compile(r"\bGmail\s+([0-9a-f]{16})\b")
HEX16_RE = re.compile(r"\b[0-9a-f]{16}\b")
PAD_RE = re.compile("[\u034f\u200b\u200c\u200d\u00ad\ufeff]")


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def dash(s):
    return (s or "").replace("\u2014", "-").replace("\u2013", "-")


def norm_ws(s):
    return re.sub(r"\s+", " ", (s or "").replace("\u00a0", " ")).strip()


# ----------------------------------------------------------------------------- amounts

def parse_amount_sv(s):
    """'1 234,56', 'U+2212 minus 1343,05', '123,75', '+271,68', '1.234,56' -> float. Raises ValueError on garbage,
    including a dot used as the decimal sign ('981.09'): a dot is only accepted as a thousands separator."""
    t = (s or "").replace("\u2212", "-").replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    if "." in t and not re.fullmatch(r"[+-]?\d{1,3}(?:\.\d{3})+(?:,\d+)?", t):
        raise ValueError(f"not a Swedish amount (dot is not a decimal sign here): {s!r}")
    t = t.replace(".", "").replace(",", ".")
    if not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", t):
        raise ValueError(f"not a Swedish amount: {s!r}")
    return float(t)


def parse_amount_any(s):
    """'1,234.56', '1 234,56', '1.234,56', '6.926,00', '1234.56', '1234,56', '981.09' -> float."""
    t = (s or "").replace("\u2212", "-").replace("\u00a0", "").replace("\u202f", "").replace(" ", "").strip()
    neg = t.startswith("-")
    t = t.lstrip("+-")
    if not t or not re.fullmatch(r"[\d.,]+", t):
        raise ValueError(f"not an amount: {s!r}")
    if "," in t and "." in t:
        dec = "," if t.rfind(",") > t.rfind(".") else "."
        t = t.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in t or "." in t:
        sep = "," if "," in t else "."
        head, _, tail = t.rpartition(sep)
        if t.count(sep) > 1 or (len(tail) == 3 and head):
            t = t.replace(sep, "")
        else:
            t = t.replace(sep, ".")
    v = float(t)
    return -v if neg else v


def currency_code(name):
    n = norm_ws(name).upper()
    if n in ISO_CURRENCIES:
        return n
    return CURRENCY_NAMES.get(n)


FOREIGN_RE = re.compile(
    r"(\d[\d.,\u00a0 ]*)\s*(" + "|".join(re.escape(k) for k in CURRENCY_NAMES) + r"|[A-Z]{3})(?![A-Za-z])"
)


def parse_foreign_text(s):
    """'240.00 UNITED STATES DOLLAR', 'Foreign Spend Amount: 316,75 EUROPEAN UNION EURO Commission ...',
    '53,71 USD', {'amount': '240.00', 'currency': 'UNITED STATES DOLLAR'} -> {'amount': float, 'currency': ISO} or None."""
    if not s:
        return None
    if isinstance(s, dict):
        cur = currency_code(str(s.get("currency", "")))
        try:
            amt = parse_amount_any(str(s.get("amount", "")))
        except ValueError:
            return None
        return {"amount": abs(amt), "currency": cur} if cur else None
    for m in FOREIGN_RE.finditer(norm_ws(str(s))):
        cur = currency_code(m.group(2))
        if not cur:
            continue
        try:
            amt = parse_amount_any(m.group(1).strip())
        except ValueError:
            continue
        return {"amount": abs(amt), "currency": cur}
    return None


def sv_amount(x, cur="SEK"):
    """78429.63 -> '78 429,63 SEK' (ASCII space as thousands separator)."""
    if x is None:
        return ""
    s = f"{abs(x):,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", " ")
    return f"{s} {cur}".strip()


CUR_PRE = r"(?:US\$|USD|EUR|SEK|GBP|DKK|NOK|CHF|\$|\u20ac|\u00a3|\bkr)"
CUR_POST = r"(?:US\$|USD|EUR|SEK|GBP|DKK|NOK|CHF|kr\b|:-|\$|\u20ac|\u00a3)"
AMOUNT_RE = re.compile(
    r"(?<![\d.,])"
    r"(?P<pre>" + CUR_PRE + r"\s?)?"
    r"(?P<num>\d{1,3}(?:[ .,]\d{3})+(?:[.,]\d{2})?|\d+(?:[.,]\d{2})?)(?!\d)"
    r"(?P<post>\s?" + CUR_POST + r")?(?!\d)"
)


def _currency_of(tok, line):
    t = (tok or "").strip()
    if not t:
        return None
    if t in ("$", "US$", "USD"):
        return "USD"
    if t in ("\u20ac", "EUR"):
        return "EUR"
    if t in ("\u00a3", "GBP"):
        return "GBP"
    if t == "CHF":
        return "CHF"
    if t == "DKK":
        return "DKK"
    if t == "NOK":
        return "NOK"
    if t in ("kr", "SEK", ":-"):
        if "DKK" in line:
            return "DKK"
        if "NOK" in line:
            return "NOK"
        return "SEK"
    return None


def find_amounts(text, source="body", limit=40):
    """Return [{'value', 'currency', 'context', 'source'}] for every money-looking number in text."""
    hits, seen = [], set()
    text = PAD_RE.sub("", text or "").replace("\u00a0", " ").replace("\u202f", " ")
    for m in AMOUNT_RE.finditer(text):
        num = m.group("num")
        s, e = m.start("num"), m.end("num")
        before = text[max(0, s - 4):s]
        after = text[e:e + 2]
        if re.search(r"(#\s?|No\.\s?|\bnr\s?|[A-Za-z]|[A-Za-z0-9][-/])$", before):
            continue
        if re.match(r"[/-]\d", after) or re.match(r"0\d", num):
            continue
        try:
            value = parse_amount_any(num)
        except ValueError:
            continue
        ls = text.rfind("\n", 0, s) + 1
        le = text.find("\n", e)
        line = text[ls:le if le >= 0 else len(text)]
        cur = _currency_of((m.group("post") or "").strip(), line) or _currency_of((m.group("pre") or "").strip(), line)
        has_decimals = bool(re.search(r"[.,]\d{2}$", num))
        if (cur is None and not has_decimals) or value == 0:
            continue
        key = (round(value, 2), cur)
        if key in seen:
            continue
        seen.add(key)
        ctx = norm_ws(text[max(0, m.start() - 40):m.end() + 40])
        hits.append({"value": round(value, 2), "currency": cur, "context": ctx, "source": source})
        if len(hits) >= limit:
            break
    return hits


def strip_html(s):
    s = PAD_RE.sub("", s or "")
    s = re.sub(r"(?is)<(script|style|head)\b.*?</\1\s*>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|tr|li|h[1-6]|table|title|blockquote)\s*>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s).replace("\u00a0", " ")
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n[ \n]*", "\n", s)
    return s.strip()


# ----------------------------------------------------------------------------- inputs

SIGNATURES = [
    ("matching_csv", {"pleo_receipt_no", "expense_id", "pleo_amount", "gmail_message_id"}),
    ("pleo_missing_hand", {"date", "pleo_receipt_no", "merchant", "amount_sek", "route", "evidence"}),
    ("pleo_missing_profile", {"pleo_receipt_no", "expense_id", "amount", "orig_amount", "orig_currency", "export_status"}),
    ("pleo_export", {"Date", "Receipt", "Expense Type", "Source description", "Expense ID"}),
    ("amex_queue", {"reference", "amount_sek", "evidence_kind", "statement_period"}),
    ("amex_classified", {"ref", "amount", "tag", "pack_status"}),
    ("amex_normalized", None),
    ("regression_set", {"set", "ledger", "row_key", "expected_message_id"}),
]


def detect_format(header):
    keys = set(header.keys() if isinstance(header, dict) else header)
    for fmt, sig in SIGNATURES:
        if fmt == "amex_normalized":
            if {"ref", "amount", "account"} <= keys and ({"kind", "source_file"} <= keys or "dash_id" in keys):
                return fmt
        elif sig <= keys:
            return fmt
    raise SystemExit(f"unrecognised worklist header: {sorted(keys)}")


def read_table(path):
    p = Path(path)
    if p.suffix.lower() == ".json":
        data = json.load(open(p, encoding="utf-8"))
        if isinstance(data, dict) and "rows" in data:
            data = data["rows"]
        if not isinstance(data, list) or not data or not isinstance(data[0], dict):
            raise SystemExit(f"{path}: expected a JSON list of objects")
        return list(data[0].keys()), data
    with open(p, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        rows = list(rd)
        return rd.fieldnames or [], rows


def base_row(**kw):
    r = {"row_key": "", "ledger": "pleo", "source_format": "", "expense_id": "", "account": "pleo", "card": "",
         "date": "", "merchant": "", "kind": "charge", "amount_sek": None, "foreign": None, "tag": "",
         "known_message_id": "", "status_hint": "", "search": True, "skip_reason": "", "check": None, "source": {}}
    r.update(kw)
    return r


def known_ids_from_text(s):
    return KNOWN_ID_RE.findall(s or "")


def first_known(s):
    ids = known_ids_from_text(s)
    return ids[0] if ids else ""


def iso_from_ddmmyyyy(s):
    return datetime.strptime(s.strip(), "%d-%m-%Y").date().isoformat()


MATCHING_AMOUNT_RE = re.compile(r"^(?P<sek>[\d\u00a0 ]+,\d{2}) SEK(?: \((?P<f>[\d\u00a0 ,.]+) (?P<cur>[A-Z]{3})\))?$")
MERCHANT_FOREIGN_RE = re.compile(r"(\d+[.,]\d{2}) (EUR|USD|GBP|DKK|NOK|CHF)$")


def foreign_from_merchant(merchant):
    m = MERCHANT_FOREIGN_RE.search(norm_ws(merchant))
    if not m:
        return None
    return {"amount": parse_amount_any(m.group(1)), "currency": m.group(2)}


def rows_from_matching(rows):
    out = []
    for r in rows:
        m = MATCHING_AMOUNT_RE.match(norm_ws(r.get("pleo_amount", "")))
        if not m:
            warn(f"MATCHING row {r.get('pleo_receipt_no')}: cannot parse pleo_amount {r.get('pleo_amount')!r}")
            continue
        foreign = None
        if m.group("f") and m.group("cur") != "SEK":
            foreign = {"amount": parse_amount_any(m.group("f")), "currency": m.group("cur")}
        merchant = norm_ws(r.get("merchant", ""))
        foreign = foreign or foreign_from_merchant(merchant)
        out.append(base_row(row_key=r["pleo_receipt_no"].strip(), ledger="pleo", source_format="matching_csv",
                            expense_id=r.get("expense_id", "").strip(), date=r["date"].strip(), merchant=merchant,
                            amount_sek=parse_amount_sv(m.group("sek")), foreign=foreign,
                            known_message_id=r.get("gmail_message_id", "").strip(), status_hint=norm_ws(r.get("status", "")),
                            source=dict(r)))
    return out


def rows_from_pleo_missing_hand(rows):
    out = []
    for r in rows:
        merchant = norm_ws(r.get("merchant", ""))
        evidence, action = norm_ws(r.get("evidence", "")), norm_ws(r.get("action", ""))
        # the hand list is typed by a person: accept both '981,09' and '981.09' and say so
        try:
            amount = abs(parse_amount_sv(r["amount_sek"]))
        except ValueError:
            amount = abs(parse_amount_any(r["amount_sek"]))
            warn(f"hand list row {r.get('pleo_receipt_no')}: amount {r['amount_sek']!r} read as {sv_amount(amount)}")
        foreign = foreign_from_merchant(merchant) or parse_foreign_text(evidence)  # evidence says e.g. '53,71 USD exact'
        if foreign and foreign["currency"] == "SEK":
            foreign = None
        row = base_row(row_key=r["pleo_receipt_no"].strip(), ledger="pleo", source_format="pleo_missing_hand",
                       date=r["date"].strip(), merchant=merchant, amount_sek=amount, foreign=foreign,
                       known_message_id=first_known(evidence), status_hint=norm_ws(r.get("route", "")), source=dict(r))
        if re.search(r"marked (as )?personal", evidence + " " + action, re.I):
            row["search"] = False
            row["skip_reason"] = "not searched (marked personal in the hand list)"
        out.append(row)
    return out


def rows_from_pleo_missing_profile(rows):
    out = []
    for r in rows:
        if (r.get("type") or "Card Purchase") != "Card Purchase":
            continue
        amt = float(str(r["amount"]).replace(",", "."))
        foreign = None
        if (r.get("orig_currency") or "SEK") != "SEK" and r.get("orig_amount"):
            foreign = {"amount": abs(parse_amount_sv(r["orig_amount"])), "currency": r["orig_currency"].strip()}
        out.append(base_row(row_key=r["pleo_receipt_no"].strip(), ledger="pleo", source_format="pleo_missing_profile",
                            expense_id=r.get("expense_id", "").strip(), date=r["date"].strip(),
                            merchant=norm_ws(r["merchant"]), kind="refund" if amt > 0 else "charge", amount_sek=abs(amt),
                            foreign=foreign, status_hint=norm_ws(r.get("export_status", "")), source=dict(r)))
    return out


def rows_from_pleo_export(rows, all_rows):
    out = []
    for r in rows:
        if r.get("Expense Type") != "Card Purchase":
            continue
        if (r.get("Reconciled Entries") or "").strip():
            continue
        if (r.get("Personal Expense") or "").strip() == "Marked as personal":
            continue
        if (r.get("Receipt urls") or "").strip() and not all_rows:
            continue
        amt = parse_amount_sv(r["Amount"])
        foreign = None
        if (r.get("Orig. currency") or "SEK") != "SEK" and r.get("Orig. amount"):
            foreign = {"amount": abs(parse_amount_sv(r["Orig. amount"])), "currency": r["Orig. currency"].strip()}
        out.append(base_row(row_key=r["Receipt"].strip(), ledger="pleo", source_format="pleo_export",
                            expense_id=r.get("Expense ID", "").strip(), date=iso_from_ddmmyyyy(r["Date"]),
                            merchant=norm_ws(r["Source description"]), kind="refund" if amt > 0 else "charge",
                            amount_sek=abs(amt), foreign=foreign, status_hint=norm_ws(r.get("Export Status", "")),
                            source=dict(r)))
    return out


def _amex_common(r, ref, amount, merchant, tag, account, foreign, known, hint, fmt, all_tags, kind=None):
    mu = merchant.upper()
    if "BETALNING MOTTAGEN" in mu or kind == "payment":
        return None
    kind = kind or ("refund" if amount < 0 else "charge")
    row = base_row(row_key=ref, ledger="amex", source_format=fmt, account=account or "", card=(account or "")[-4:],
                   date=r["date"].strip(), merchant=merchant, kind=kind, amount_sek=abs(amount), foreign=foreign,
                   tag=tag, known_message_id=known, status_hint=hint, source=dict(r))
    if tag in ("personal", "skip") and not all_tags:
        row["search"] = False
        row["skip_reason"] = f"not searched (tag {tag})"
    return row


def rows_from_amex_queue(rows, all_tags):
    out = []
    for r in rows:
        card = (r.get("card") or "").strip()
        hint = f"{r.get('evidence_kind', '')}: {r.get('evidence_where', '')}".strip(": ").strip()
        row = _amex_common(r, r["reference"].strip(), float(r["amount_sek"]), norm_ws(r["merchant"]), (r.get("tag") or "").strip(),
                           CARD_TO_ACCOUNT.get(card, ""), parse_foreign_text(r.get("foreign", "")),
                           first_known(r.get("evidence_where", "")), hint, "amex_queue", all_tags)
        if row:
            out.append(row)
    return out


def rows_from_amex_ledger(rows, all_tags, fmt):
    out = []
    for r in rows:
        amount = float(str(r["amount"]).replace(",", "."))
        account = (r.get("account") or "").strip() or CARD_TO_ACCOUNT.get(str(r.get("card", "")).strip(), "")
        foreign = parse_foreign_text(r.get("foreign")) or parse_foreign_text(r.get("ext", ""))
        hint = norm_ws(str(r.get("pack_status") or r.get("dash_status") or ""))
        row = _amex_common(r, str(r["ref"]).strip(), amount, norm_ws(r["merchant"]), (r.get("tag") or "").strip(), account,
                           foreign, "", hint, fmt, all_tags, kind=r.get("kind"))
        if row:
            out.append(row)
    return out


def _split(s, n):
    parts = [p.strip() for p in (s or "").split(";")]
    if len(parts) == n:
        return parts
    return [parts[0] if parts else ""] * n


def rows_from_regression(rows):
    out = []
    for r in rows:
        dates = [d.strip() for d in (r.get("charge_date") or "").split(";")]
        n = len(dates)
        keys = _split(r.get("row_key"), n)
        if len([p for p in (r.get("row_key") or "").split(";")]) != n:
            keys = [f"{keys[0]}#{i + 1}" if n > 1 else keys[0] for i in range(n)]
        merchants = _split(r.get("merchant"), n)
        amounts = _split(r.get("amount_sek"), n)
        ids = _split(r.get("expense_id_or_ref"), n)
        ledger = (r.get("ledger") or "pleo").strip()
        for i in range(n):
            skip = ""
            try:
                d = date.fromisoformat(dates[i]).isoformat()
            except ValueError:
                d, skip = "", "no usable charge date"
            amt, kind = None, "charge"
            try:
                if amounts[i]:
                    amt = parse_amount_sv(amounts[i])
                    kind = "refund" if amounts[i].strip().startswith("+") else "charge"
                    amt = abs(amt)
            except ValueError:
                amt = None
            orig = norm_ws(r.get("orig_amount", ""))
            foreign = parse_foreign_text(orig) if orig else None
            if foreign and foreign["currency"] == "SEK":
                foreign = None
            if "refund" in orig.lower():
                kind = "refund"
            account, card, expense_id = ("pleo", "", "") if ledger == "pleo" else ("", "", "")
            m = re.search(r"card (\d{4})", ids[i] or "")
            if m:
                card = m.group(1)
                account = CARD_TO_ACCOUNT.get(card, "")
            elif re.search(r"account (-\d+)", ids[i] or ""):
                account = re.search(r"account (-\d+)", ids[i]).group(1)
                card = account[-4:]
            elif re.fullmatch(r"[0-9a-f-]{36}", ids[i] or ""):
                expense_id = ids[i]
            if amt is None and not foreign:
                skip = skip or "no usable amount"
            row = base_row(row_key=keys[i], ledger="amex" if ledger in ("amex", "dashboard") else "pleo",
                           source_format="regression_set", expense_id=expense_id, account=account, card=card, date=d,
                           merchant=norm_ws(merchants[i]), kind=kind, amount_sek=amt, foreign=foreign,
                           known_message_id=(r.get("expected_message_id") or "").strip() if HEX16_RE.fullmatch((r.get("expected_message_id") or "").strip()) else "",
                           status_hint=norm_ws(r.get("set", "")), source=dict(r),
                           check={"set": (r.get("set") or "").strip(), "expected": (r.get("expected_message_id") or "").strip(),
                                  "secondary": (r.get("secondary_message_id") or "").strip(), "notes": r.get("notes") or ""})
            if skip:
                row["search"] = False
                row["skip_reason"] = skip
            out.append(row)
    return out


def load_worklist(path, args):
    header, rows = read_table(path)
    fmt = detect_format(header)
    all_tags = getattr(args, "all_tags", False)
    if fmt == "matching_csv":
        work = rows_from_matching(rows)
    elif fmt == "pleo_missing_hand":
        work = rows_from_pleo_missing_hand(rows)
    elif fmt == "pleo_missing_profile":
        work = rows_from_pleo_missing_profile(rows)
    elif fmt == "pleo_export":
        work = rows_from_pleo_export(rows, getattr(args, "all_rows", False))
    elif fmt == "amex_queue":
        work = rows_from_amex_queue(rows, all_tags)
    elif fmt in ("amex_classified", "amex_normalized"):
        work = rows_from_amex_ledger(rows, all_tags, fmt)
    else:
        work = rows_from_regression(rows)
    for w in work:
        w["source_format"] = fmt
    return fmt, work


# ----------------------------------------------------------------------------- aliases

VENDOR_KEYS = {"name", "entity", "merchants", "merchant_exact", "merchant_regex", "amounts", "senders", "sender_exact",
               "subject_terms", "query", "bill_currency", "lag_band", "window", "attachments", "route_if_none", "portal",
               "decoy_senders", "notes"}


def load_aliases(path):
    if not os.path.exists(path):
        raise SystemExit(f"alias table not found: {path}")
    data = json.load(open(path, encoding="utf-8"))
    for key in ("defaults", "generic", "vendors"):
        if key not in data:
            raise SystemExit(f"{path}: missing top-level key {key!r}")
    d = data["defaults"]
    d.setdefault("lag_band", {}).setdefault("pleo", [-2, 1])
    d["lag_band"].setdefault("amex", [-1, 1])
    d.setdefault("window", {"before": 10, "after": 5})
    d.setdefault("strong", 70)
    d.setdefault("possible", 45)
    d.setdefault("weak", 25)
    gen = data["generic"]
    for key in ("receipt_terms", "refund_terms", "decoy_terms", "decoy_senders", "corroboration_senders",
                "forward_prefixes", "reply_prefixes", "merchant_stopwords"):
        if key not in gen:
            raise SystemExit(f"{path}: generic.{key} is required")
    for k, v in data["vendors"].items():
        for req in ("name", "merchants"):
            if req not in v:
                raise SystemExit(f"{path}: vendor {k} lacks {req!r}")
        unknown = set(v) - VENDOR_KEYS
        if unknown:
            warn(f"aliases: vendor {k} has unknown keys {sorted(unknown)}")
        v.setdefault("senders", [])
        v.setdefault("sender_exact", [])
        v.setdefault("subject_terms", [])
        v.setdefault("decoy_senders", [])
        v.setdefault("entity", "viseo")
        v.setdefault("route_if_none", "none")
        if "merchant_regex" in v:
            re.compile(v["merchant_regex"], re.I)
    data["_sha1"] = hashlib.sha1(open(path, "rb").read()).hexdigest()
    return data


def merchant_tokens(merchant, stop):
    toks = []
    for t in re.split(r"[^A-Z0-9]+", (merchant or "").upper()):
        if len(t) >= 4 and t not in stop and not re.search(r"\d", t) and t not in toks:
            toks.append(t)
    return toks


def _pick_vendor(cands, row):
    if len(cands) == 1:
        return cands[0][0], cands[0][1], False
    f = row.get("foreign")
    for k, v in cands:
        for a in v.get("amounts", []) or []:
            if f and a.get("currency") == f["currency"] and abs(a["amount"] - f["amount"]) <= 0.01:
                return k, v, False
            if v.get("bill_currency") == "SEK" and a.get("currency") == "SEK" and row.get("amount_sek") is not None \
                    and abs(a["amount"] - row["amount_sek"]) <= 0.01:
                return k, v, False
    return cands[0][0], cands[0][1], True


def vendor_for(row, aliases):
    m = norm_ws(row["merchant"]).upper()
    vendors = aliases["vendors"]
    stop = set(aliases["generic"]["merchant_stopwords"])
    exact = [(k, v) for k, v in vendors.items() if v.get("merchant_exact") and m in [x.upper() for x in v["merchants"]]]
    if exact:
        return _pick_vendor(exact, row)
    subs = []
    for k, v in vendors.items():
        if v.get("merchant_exact"):
            continue
        best = max((len(x) for x in v["merchants"] if norm_ws(x).upper() in m), default=0)
        if best:
            subs.append((best, k, v))
    if subs:
        subs.sort(key=lambda t: -t[0])
        return _pick_vendor([(k, v) for ln, k, v in subs if ln == subs[0][0]], row)
    rx = [(k, v) for k, v in vendors.items() if v.get("merchant_regex") and re.search(v["merchant_regex"], m, re.I)]
    if rx:
        return _pick_vendor(rx, row)
    toks = set(merchant_tokens(m, stop))
    if toks:
        owners = {}  # token -> vendor keys that use it; only tokens owned by exactly one vendor are distinctive
        for k, v in vendors.items():
            for x in list(v["merchants"]) + [v["name"]]:
                for t in merchant_tokens(x, stop):
                    owners.setdefault(t, set()).add(k)
        tok = [(k, vendors[k]) for t in sorted(toks) for k in owners.get(t, ()) if len(owners[t]) == 1]
        tok = [(k, v) for i, (k, v) in enumerate(tok) if k not in [x[0] for x in tok[:i]]]
        if tok:
            return _pick_vendor(tok, row)
    return None, None, False


# ----------------------------------------------------------------------------- queries

def window_for(row, vendor, args):
    d = date.fromisoformat(row["date"])
    w = (vendor or {}).get("window") or {}
    before = int(w.get("before", args.days_before))
    after = int(w.get("after", args.days_after))
    return d - timedelta(days=before), d + timedelta(days=after)


def gmail_date(d):
    return d.strftime("%Y/%m/%d")


def amount_terms(x):
    if x is None or x < 10:
        return []
    en = f"{x:.2f}"
    terms = [en, sv_amount(x, "")]
    if x >= 1000:
        terms += [f"{x:,.2f}", f"{x:,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", ".")]
    return terms


def build_queries(row, vendor, window, aliases):
    gen = aliases["generic"]
    stop = set(gen["merchant_stopwords"])
    W = f"after:{gmail_date(window[0] - timedelta(days=1))} before:{gmail_date(window[1] + timedelta(days=1))}"
    qs = []
    v = vendor or {}
    senders = list(v.get("senders", [])) + list(v.get("sender_exact", []))
    if vendor and (senders or v.get("query")):
        q = v.get("query") or "from:(" + " OR ".join(senders) + ")"
        qs.append({"kind": "vendor", "q": f"{q} {W}", "max": 50})
    if vendor and v.get("subject_terms"):
        qs.append({"kind": "subject", "q": "subject:(" + " OR ".join(f'"{t}"' for t in v["subject_terms"]) + f") {W}", "max": 30})
    if not vendor or not senders:
        toks = merchant_tokens(row["merchant"], stop)
        for word in merchant_tokens(v.get("name", ""), stop):
            if word not in toks:
                toks.append(word)
        toks = toks[:5]
        if toks:
            qs.append({"kind": "merchant", "q": "(" + " OR ".join(f'"{t.lower()}"' for t in toks)
                       + f") (receipt OR kvitto OR invoice OR faktura OR payment OR betalning) {W}", "max": 30})
        else:
            row["no_query"] = True
    terms = amount_terms(row.get("amount_sek"))
    if row.get("foreign"):
        terms += amount_terms(row["foreign"]["amount"])
    terms = [t for i, t in enumerate(terms) if t not in terms[:i]]
    if terms:
        qs.append({"kind": "amount", "q": "(" + " OR ".join(f'"{t}"' for t in terms) + f") {W}", "max": 30})
    return qs


# ----------------------------------------------------------------------------- transport

def now_utc():
    return datetime.now(timezone.utc)


def atomic_write_text(path, text):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, p)


class Cache:
    def __init__(self, path, enabled=True):
        self.path = path
        self.enabled = bool(enabled and path)
        self.dirty = False
        self.data = {"version": 1, "mailbox": MAILBOX, "lists": {}, "messages": {}, "full": {}}
        if self.enabled and os.path.exists(path):
            try:
                d = json.load(open(path, encoding="utf-8"))
                if d.get("version") == 1 and d.get("mailbox") == MAILBOX:
                    self.data = d
                else:
                    warn(f"cache {path} has another version or mailbox; starting empty")
            except (ValueError, OSError) as e:
                warn(f"cache {path} unreadable ({e}); starting empty")

    @staticmethod
    def list_key(q, mx):
        return hashlib.sha1(f"{q}|{mx}".encode("utf-8")).hexdigest()

    def get_list(self, q, mx, refresh=False):
        """A list is reused for 7 days if its date window had already closed when it was fetched (and for good once
        the window had been closed 14 days, late indexing included). A list fetched while the window was still open
        may have missed mail that arrived later, so it is reused only for OPEN_WINDOW_TTL and then relisted."""
        if not self.enabled or refresh:
            return None
        e = self.data["lists"].get(self.list_key(q, mx))
        if not e:
            return None
        try:
            fetched = datetime.fromisoformat(e["fetched_at"])
            closed_days = (fetched.date() - date.fromisoformat(e["window_end"])).days  # days closed at fetch time
            age = now_utc() - fetched
            if closed_days < 1:
                return list(e["ids"]) if age <= OPEN_WINDOW_TTL else None
            if age <= timedelta(days=7) or closed_days > 14:
                return list(e["ids"])
        except (ValueError, KeyError):
            return None
        return None

    def put_list(self, q, mx, ids, window_end):
        if not self.enabled:
            return
        self.data["lists"][self.list_key(q, mx)] = {"q": q, "max": mx, "ids": list(ids), "fetched_at": now_utc().isoformat(),
                                                    "window_end": window_end.isoformat()}
        self.dirty = True

    def get_meta(self, mid):
        e = self.data["messages"].get(mid) if self.enabled else None
        return e["meta"] if e else None

    def put_meta(self, mid, meta):
        if self.enabled:
            self.data["messages"][mid] = {"meta": meta, "fetched_at": now_utc().isoformat()}
            self.dirty = True

    def get_full(self, mid):
        e = self.data["full"].get(mid) if self.enabled else None
        return e if e and e.get("extract_version") == EXTRACT_VERSION else None

    def put_full(self, mid, full):
        if self.enabled:
            full = {k: v for k, v in full.items() if k != "text_excerpt"}  # never keep mail bodies on disk
            full["fetched_at"] = now_utc().isoformat()
            self.data["full"][mid] = full
            self.dirty = True

    def purge(self):
        """Drop what no current list references, stale extracts, and any body text an older version stored."""
        referenced = set()
        for e in self.data["lists"].values():
            referenced.update(e.get("ids") or [])
        dropped = 0
        for bucket in ("messages", "full"):
            for mid in [m for m in self.data[bucket] if m not in referenced]:
                del self.data[bucket][mid]
                dropped += 1
        for mid in [m for m, e in self.data["full"].items() if e.get("extract_version") != EXTRACT_VERSION]:
            del self.data["full"][mid]
            dropped += 1
        for e in self.data["full"].values():
            if "text_excerpt" in e:
                del e["text_excerpt"]
                dropped += 1
        if dropped:
            self.dirty = True
        return dropped

    def save(self):
        if self.enabled and self.dirty:
            atomic_write_text(self.path, json.dumps(self.data, ensure_ascii=False, indent=1))
            self.dirty = False


def walk_parts(part):
    yield part
    for p in part.get("parts", []) or []:
        yield from walk_parts(p)


def msg_meta(data):
    s = g.message_summary(data, include_body=False)
    h = s["headers"]
    return {"id": s["id"], "threadId": s.get("threadId") or s["id"], "internalDate": str(s.get("internalDate") or "0"),
            "labelIds": s.get("labelIds") or [], "snippet": html.unescape(s.get("snippet") or ""),
            "headers": {k: h.get(k, "") for k in ("from", "to", "cc", "reply-to", "subject", "date", "message-id")}}


def extract_full(msg):
    payload = msg.get("payload") or {}
    plain, htmls = [], []
    for p in walk_parts(payload):
        mt = (p.get("mimeType") or "").lower()
        data = (p.get("body") or {}).get("data")
        if not data:
            continue
        if mt == "text/plain":
            plain.append(PAD_RE.sub("", g.decode_part_body(data)))
        elif mt == "text/html":
            htmls.append(strip_html(g.decode_part_body(data)))
    plain_text = "\n".join(plain)
    html_text = "\n".join(htmls)
    amounts = find_amounts(plain_text, "body")
    seen = {(h["value"], h["currency"]) for h in amounts}
    for h in find_amounts(html_text, "body"):
        if (h["value"], h["currency"]) not in seen:
            seen.add((h["value"], h["currency"]))
            amounts.append(h)
    links = []
    for u in re.findall(r'https?://[^\s"\'<>)]+', plain_text + "\n" + html_text):
        if not re.search(r"unsubscribe|w3\.org|mailto:|\.(png|gif|jpe?g|css)(\?|$)", u, re.I) and u not in links:
            links.append(u)
    atts = [{"filename": a["filename"], "mime_type": a["mime_type"], "size": a["size"]} for a in g.attachment_metadata(payload)]
    # Only what the scorer uses is kept: amounts, attachments, links, part kinds. No body text leaves this function.
    return {"amounts": amounts[:40], "attachments": atts, "links": links[:20], "has_html": bool(htmls),
            "has_text": bool(plain), "text_chars": len(plain_text) + len(html_text), "extract_version": EXTRACT_VERSION}


class GmailClient:
    def __init__(self, mailbox, reason, cache, sleep_ms=0, dry_run=False, refresh=False):
        self.mailbox = mailbox
        self.base = f"users/{quote(mailbox)}"
        self.reason = reason
        self.cache = cache
        self.sleep_ms = sleep_ms
        self.dry_run = dry_run
        self.refresh = refresh
        self.session = None if dry_run else g.session_for(mailbox)
        self.counters = {"list": 0, "metadata": 0, "full": 0, "list_cached": 0, "metadata_cached": 0, "full_cached": 0,
                         "retries": 0, "errors": 0}

    def _get(self, path, params):
        if self.dry_run:
            raise g.AccessError("dry run: no API calls")
        action = "list" if path.endswith("/messages") else "read"
        for attempt in range(3):
            try:
                d = g.gmail_get(self.session, path, params=params)
                if self.sleep_ms:
                    time.sleep(self.sleep_ms / 1000.0)
                return d
            except g.AccessError as e:
                # every attempt is a real request against the mailbox, so failed ones are audited too
                retry = attempt < 2 and bool(re.search(r"Gmail API error (429|5\d\d)", str(e)))
                g.audit(action, self.mailbox, self.reason, {"outcome": "retry" if retry else "failed", "attempt": attempt + 1,
                                                           "path": path, "error": str(e)[:200], "via": "gmail_match"})
                if retry:
                    self.counters["retries"] += 1
                    time.sleep(2 ** (attempt + 1))
                    continue
                self.counters["errors"] += 1
                raise

    def list_ids(self, q, max_results, window_end):
        cached = self.cache.get_list(q, max_results, self.refresh)
        if cached is not None:
            self.counters["list_cached"] += 1
            return cached, True
        ids, tok = [], None
        while True:
            p = {"q": q, "maxResults": min(max_results - len(ids), 500)}
            if tok:
                p["pageToken"] = tok
            d = self._get(f"{self.base}/messages", p)
            self.counters["list"] += 1
            got = [m["id"] for m in d.get("messages", []) or []]
            g.audit("list", self.mailbox, self.reason, {"query": q, "max_results": max_results, "returned": len(got), "via": "gmail_match"})
            ids += got
            tok = d.get("nextPageToken")
            if not tok or len(ids) >= max_results:
                break
        ids = ids[:max_results]
        self.cache.put_list(q, max_results, ids, window_end)
        return ids, False

    def metadata(self, mid):
        m = self.cache.get_meta(mid)
        if m:
            self.counters["metadata_cached"] += 1
            return m
        d = self._get(f"{self.base}/messages/{quote(mid)}", {"format": "metadata"})
        self.counters["metadata"] += 1
        g.audit("read", self.mailbox, self.reason, {"message_id": mid, "format": "metadata", "via": "gmail_match"})
        m = msg_meta(d)
        self.cache.put_meta(mid, m)
        return m

    def full(self, mid):
        f = self.cache.get_full(mid)
        if f:
            self.counters["full_cached"] += 1
            return f
        d = self._get(f"{self.base}/messages/{quote(mid)}", {"format": "full"})
        self.counters["full"] += 1
        g.audit("read", self.mailbox, self.reason, {"message_id": mid, "format": "full", "via": "gmail_match"})
        f = extract_full(d)
        self.cache.put_full(mid, f)
        return f


# ----------------------------------------------------------------------------- scoring

def addr_parts(header):
    name, addr = parseaddr(header or "")
    addr = addr.lower()
    return name, addr, addr.rsplit("@", 1)[-1] if "@" in addr else ""


def link_host(u):
    m = re.match(r"https?://([^/:?#]+)", u or "")
    return m.group(1).lower() if m else ""


def in_senders(addr, dom, entries):
    for e in entries or []:
        e = e.lower()
        if "@" in e:
            if addr == e:
                return True
        elif dom == e or dom.endswith("." + e):
            return True
    return False


def build_candidate(meta, row, window):
    name, addr, dom = addr_parts(meta["headers"].get("from"))
    _, raddr, rdom = addr_parts(meta["headers"].get("reply-to"))
    ts = int(meta.get("internalDate") or 0) / 1000.0
    dt_utc = datetime.fromtimestamp(ts, tz=timezone.utc)
    local = dt_utc.astimezone(TZ).date()
    charge = date.fromisoformat(row["date"])
    return {"message_id": meta["id"], "thread_id": meta.get("threadId") or meta["id"], "internalDate": meta.get("internalDate"),
            "from": meta["headers"].get("from", ""), "from_name": name, "from_addr": addr, "from_domain": dom,
            "reply_to": raddr, "reply_to_domain": rdom, "to": meta["headers"].get("to", ""),
            "subject": meta["headers"].get("subject", ""), "date_utc": dt_utc.isoformat(), "date_local": local.isoformat(),
            "lag_days": (local - charge).days, "in_window": window[0] <= local <= window[1],
            "snippet": meta.get("snippet", ""), "amount_evidence": find_amounts(meta.get("snippet", ""), "snippet"),
            "attachments": [], "has_pdf": False, "has_html": False, "links": [], "fetched": "metadata", "same_thread": []}


def is_fetch_forward(cand):
    return FORWARD_TO in (cand.get("to") or "").lower()


def classify(cand, vendor, aliases, row=None):
    gen = aliases["generic"]
    subj = (cand["subject"] or "").strip().lower()
    if in_senders(cand["from_addr"], cand["from_domain"], gen["corroboration_senders"]):
        return "corroboration"
    if in_senders(cand["from_addr"], cand["from_domain"], (vendor or {}).get("decoy_senders", []) + gen["decoy_senders"]):
        return "decoy"
    if any(t in subj for t in gen["decoy_terms"]):
        return "decoy"
    if row is not None and row.get("kind") != "refund" and any(t in subj for t in gen["refund_terms"]):
        return "refund"  # a credit note is never the receipt of a charge, whatever amount it carries
    if any(subj.startswith(p) for p in gen["forward_prefixes"]) or is_fetch_forward(cand):
        return "forward"
    if cand["from_addr"] in [x.lower() for x in gen.get("own_senders") or [MAILBOX]]:
        return "own"
    if any(subj.startswith(p) for p in gen["reply_prefixes"]):
        return "reply"
    return "receipt"


def amount_level(row, hits, cls, allow_fx=False):
    """allow_fx: accept a SEK figure within 3 percent as the vendor's own conversion of a foreign charge. Only valid
    when the row is billed in a foreign currency and the mail sits close to the charge; for a vendor billed in SEK
    a nearby SEK amount is another month's receipt, which counts as a contradiction."""
    sek, f = row.get("amount_sek"), row.get("foreign")
    order = {"sek_exact": 40, "foreign_exact": 35, "unlabelled": 20, "sek_fx": 15}
    best, best_hit, comparable = "none", None, False
    for h in hits:
        v, cur = h["value"], h["currency"]
        if cur == "SEK" and sek is not None or (f and cur == f["currency"]):
            comparable = True  # a labelled hit in a currency the row can be checked against
        lvl = None
        if cur == "SEK" and sek is not None and abs(v - sek) <= 0.01:
            lvl = "sek_exact"
        elif f and cur == f["currency"] and abs(v - f["amount"]) <= 0.01:
            lvl = "foreign_exact"
        elif cur is None and ((sek is not None and abs(v - sek) <= 0.01) or (f and abs(v - f["amount"]) <= 0.01)):
            lvl = "unlabelled"
        elif allow_fx and cur == "SEK" and sek and abs(v - sek) <= 0.03 * sek:
            lvl = "sek_fx"
        if lvl and order[lvl] > order.get(best, -1):
            best, best_hit = lvl, h
    if best == "none" and comparable and cls == "receipt":
        return "contradiction", -15, None
    return best, order.get(best, 0), best_hit


def lag_band_for(row, vendor, aliases):
    return list((vendor or {}).get("lag_band") or aliases["defaults"]["lag_band"][row["ledger"]])


def band_distance(lag, band):
    if band[0] <= lag <= band[1]:
        return 0
    return band[0] - lag if lag < band[0] else lag - band[1]


def score_candidate(row, vendor, cand, aliases, phase):
    gen = aliases["generic"]
    stop = set(gen["merchant_stopwords"])
    v = vendor or {}
    parts = {}
    band = lag_band_for(row, vendor, aliases)
    out = band_distance(cand["lag_days"], band)
    foreign_billed = bool(row.get("foreign")) or (v.get("bill_currency") or "SEK") != "SEK"
    allow_fx = foreign_billed and cand["in_window"] and out <= 5  # FX settlement lag is days, not a month
    level, parts["amount"], best_hit = amount_level(row, cand["amount_evidence"], cand["class"], allow_fx)
    if not cand["in_window"]:
        parts["date"] = 0
    else:
        parts["date"] = 25 if out == 0 else max(5, 25 - 3 * out)
    toks = merchant_tokens(row["merchant"], stop)
    name_words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", v.get("name", ""))]
    if vendor and cand["from_addr"] in [x.lower() for x in v.get("sender_exact", [])]:
        parts["sender"] = 20
    elif vendor and in_senders(cand["from_addr"], cand["from_domain"], v.get("senders", [])):
        parts["sender"] = 15
    elif vendor and in_senders(cand["reply_to"], cand["reply_to_domain"], v.get("senders", [])):
        parts["sender"] = 12
    elif vendor and any(w in cand["from_name"].lower() for w in name_words if w not in ("the", "inc", "llc", "ltd")):
        parts["sender"] = 10
    elif not vendor and any(t.lower() in (cand["from_name"] + " " + cand["from_domain"]).lower() for t in toks):
        parts["sender"] = 10
    else:
        parts["sender"] = 0
    subj = (cand["subject"] or "").lower()
    refund_hit = any(t in subj for t in gen["refund_terms"])
    if refund_hit and row["kind"] != "refund":
        parts["subject"] = -20
    else:
        if any(t.lower() in subj for t in v.get("subject_terms", [])) or (refund_hit and row["kind"] == "refund"):
            s = 10
        elif any(t in subj for t in gen["receipt_terms"]):
            s = 5
        else:
            s = 0
        if any(t.lower() in subj for t in toks):
            s += 3
        parts["subject"] = min(s, 10)
    parts["attachment"] = 0
    if phase == "full":
        kind = v.get("attachments") if vendor else None
        has_link = any(re.search(r"invoice|receipt|pdf|faktura|kvitto", u, re.I) or in_senders("", link_host(u), v.get("senders", []))
                       for u in cand["links"])
        if kind == "pdf":
            parts["attachment"] = 5 if cand["has_pdf"] else 0
        elif kind in ("html", "none"):
            parts["attachment"] = 5 if cand["has_html"] else 0
        elif kind == "link":
            parts["attachment"] = 5 if has_link else 0
        else:
            parts["attachment"] = 5 if cand["has_pdf"] else (2 if cand["has_html"] else 0)
    c = 0
    if cand["class"] == "forward":
        c -= 15
    elif cand["class"] == "reply":
        c -= 25
    if any(t in subj for t in FAIL_TERMS):
        c -= 20
    parts["class"] = c
    total = max(-100, min(100, sum(parts.values())))
    cand["score"], cand["score_parts"], cand["amount_match"], cand["amount_best"] = total, parts, level, best_hit
    return total, parts, level


def confidence_for(score, aliases):
    d = aliases["defaults"]
    if score >= d["strong"]:
        return "strong"
    if score >= d["possible"]:
        return "possible"
    if score >= d["weak"]:
        return "weak"
    return "dropped"


def rank(row, vendor, cands, aliases, max_per_row):
    band = lag_band_for(row, vendor, aliases)
    by_thread = {}
    for c in cands:
        t = c["thread_id"]
        if t not in by_thread or c["score"] > by_thread[t]["score"]:
            by_thread[t] = c
    kept = []
    for c in cands:
        w = by_thread[c["thread_id"]]
        if w is c:
            kept.append(c)
        else:
            w["same_thread"].append(c["message_id"])
    kept.sort(key=lambda c: (-c["score"], band_distance(c["lag_days"], band), -int(c["internalDate"] or 0)))
    for c in kept:
        c["confidence"] = confidence_for(c["score"], aliases)
        c["notes"] = []
        out = band_distance(c["lag_days"], band)
        if out > 14:
            if c["confidence"] == "strong":
                c["confidence"] = "possible"
            c["notes"].append(f"lag {c['lag_days']} days, verify the month")
        if c["confidence"] in ("strong", "possible") and not two_signals(c):
            c["confidence"] = "weak"  # a vendor domain (or a receipt word) alone is not a match, whatever the date says
            c["notes"].append("only one signal, not routed")
    good = [c for c in kept if c["confidence"] in ("strong", "possible")]
    final = good if good else [c for c in kept if c["confidence"] == "weak" and two_signals(c)][:2]
    final = final[:max_per_row]
    for i, c in enumerate(final, 1):
        c["rank"] = i
    return final


def has_signal(c):
    """At least one vendor or amount signal, not just a date inside the window (gate for the full fetch)."""
    p = c["score_parts"]
    return p["sender"] > 0 or p["subject"] >= 10 or c["amount_match"] not in ("none", "contradiction")


def two_signals(c):
    """A candidate is only a match (or worth showing as weak) when the amount matches, or the sender and the subject
    both point at a receipt. Sender alone is marketing; a receipt word alone is some other vendor's receipt."""
    p = c["score_parts"]
    return c["amount_match"] not in ("none", "contradiction") or (p["sender"] > 0 and p["subject"] >= 5)


def match_row(row, aliases, client, args, run_date, ctx):
    entity = ctx.get("entity", "viseo")
    res = {k: row[k] for k in ("row_key", "ledger", "source_format", "expense_id", "account", "card", "date", "merchant",
                               "kind", "amount_sek", "foreign", "tag")}
    res.update({"entity": entity, "vendor": None, "vendor_ambiguous": False, "window": None, "queries": [], "candidates": [],
                "corroboration": [], "rejected": [], "forwards": [], "best": None, "second": None, "route": "none",
                "confidence": "none", "route_reason": "", "forward_from": "", "forward_deadline": None, "age_days": None,
                "recheck_after": None, "pack_secondary": None, "known_message_id": row.get("known_message_id", ""), "check": "",
                "check_rank": None, "error": None, "status_hint": row.get("status_hint", ""), "no_query": False,
                "source": row.get("source", {})})
    vkey, vendor, ambiguous = vendor_for(row, aliases)
    res["vendor"], res["vendor_ambiguous"] = vkey, ambiguous
    if not row.get("search", True):
        res["route_reason"] = row.get("skip_reason") or "not searched"
        if row.get("date"):
            res.update(route_for(row, vendor, None, None, run_date, aliases, entity))
            res["route"], res["route_reason"] = "none", row.get("skip_reason") or "not searched"
        return res
    window = window_for(row, vendor, args)
    res["window"] = [window[0].isoformat(), window[1].isoformat()]
    queries = build_queries(row, vendor, window, aliases)
    res["no_query"] = bool(row.get("no_query"))
    res["queries"] = [{"kind": q["kind"], "q": q["q"], "max": q["max"], "ids": None, "cached": None} for q in queries]
    if args.dry_run:
        amt = sv_amount(row["amount_sek"]) if row["amount_sek"] is not None else "(no amount)"
        f = f" ({sv_amount(row['foreign']['amount'], row['foreign']['currency'])})" if row.get("foreign") else ""
        print(f"{row['row_key']} {row['ledger']} {row['date']} {row['merchant']} {amt}{f} {row['kind']} vendor={vkey or '-'}"
              f"{' (ambiguous)' if ambiguous else ''} window {res['window'][0]}..{res['window'][1]}")
        for q in queries:
            print(f"   {q['kind']:8} max {q['max']:<3} {q['q']}")
        if not queries:
            print("   (no query)")
        return res
    try:
        ids = []
        for i, q in enumerate(queries):
            got, cached = client.list_ids(q["q"], q["max"], window[1])
            res["queries"][i]["ids"], res["queries"][i]["cached"] = len(got), cached
            for mid in got:
                if mid not in ids:
                    ids.append(mid)
        ids = ids[:80]
        cands, corro, rejected, forwards = [], [], [], []
        for mid in ids:
            meta = client.metadata(mid)
            c = build_candidate(meta, row, window)
            c["class"] = classify(c, vendor, aliases, row)
            score_candidate(row, vendor, c, aliases, "metadata")
            if c["class"] == "corroboration":
                corro.append(c)
            elif c["class"] in ("decoy", "own", "refund"):
                rejected.append(c)
            else:
                cands.append(c)
            if c["class"] == "forward" and is_fetch_forward(c):
                forwards.append(c)
        cands.sort(key=lambda c: -c["score"])
        # full fetch only where metadata already shows a vendor or amount signal; a date inside the window is not one
        for c in [c for c in cands if has_signal(c)][:args.max_full]:
            full = client.full(c["message_id"])
            seen = {(h["value"], h["currency"]) for h in c["amount_evidence"]}
            for h in full["amounts"]:
                if (h["value"], h["currency"]) not in seen:
                    seen.add((h["value"], h["currency"]))
                    c["amount_evidence"].append(h)
            c["attachments"] = full["attachments"]
            c["has_pdf"] = any((a.get("filename") or "").lower().endswith(".pdf") for a in full["attachments"])
            c["has_html"] = bool(full.get("has_html"))
            c["links"] = full.get("links", [])
            c["fetched"] = "full"
            score_candidate(row, vendor, c, aliases, "full")
        final = rank(row, vendor, cands, aliases, args.max_per_row)
        corro.sort(key=lambda c: -c["score"])
        rejected.sort(key=lambda c: -c["score"])
        res["candidates"] = [export_candidate(c) for c in final]
        res["corroboration"] = [export_candidate(c) for c in corro[:3]]
        reasons = {"own": "sent by the mailbox owner, not a receipt", "refund": "refund mail, not the receipt of a charge"}
        res["rejected"] = [dict(export_candidate(c), reason=reasons.get(c["class"], "decoy sender or subject"))
                           for c in rejected[:3]]
        best = final[0] if final else None
        second = final[1] if len(final) > 1 else None
        res["best"] = best["message_id"] if best else None
        res["second"] = second["message_id"] if second else None
        res["forwards"] = forwards_for(best, forwards, ctx.get("forward_index") or {}, aliases)
        res.update(route_for(row, vendor, best, second, run_date, aliases, entity, window, res["forwards"]))
    except g.AccessError as e:
        res["error"] = str(e)
        res["route_reason"] = "aborted on API error; rerun"
        warn(f"row {row['row_key']}: {e}")
    finally:
        client.cache.save()
    return res


def forward_subject_key(subject, aliases):
    s = norm_ws(subject).lower()
    changed = True
    while changed:
        changed = False
        for p in aliases["generic"]["forward_prefixes"] + aliases["generic"]["reply_prefixes"]:
            if s.startswith(p):
                s, changed = s[len(p):].strip(), True
    return s


def build_forward_index(client, rows, run_date, aliases):
    """One list call per run: every mail Oscar sent to the Fetch address since the earliest searched Pleo charge,
    indexed by thread and by subject, so a forward made long after the charge (outside the row window) is still seen."""
    dates = [r["date"] for r in rows if r["ledger"] == "pleo" and r.get("search", True) and r.get("date")]
    if not dates:
        return {}
    since = date.fromisoformat(min(dates)) - timedelta(days=1)
    q = f"to:{FORWARD_TO} after:{gmail_date(since)}"
    ids, _ = client.list_ids(q, 100, run_date)
    idx = {"by_thread": {}, "by_subject": {}, "query": q, "n": len(ids)}
    for mid in ids:
        meta = client.metadata(mid)
        name, addr, _ = addr_parts(meta["headers"].get("from"))
        local = datetime.fromtimestamp(int(meta.get("internalDate") or 0) / 1000.0, tz=timezone.utc).astimezone(TZ).date()
        f = {"message_id": mid, "thread_id": meta.get("threadId") or mid, "from_addr": addr, "date_local": local.isoformat(),
             "subject": meta["headers"].get("subject", "")}
        idx["by_thread"].setdefault(f["thread_id"], []).append(f)
        idx["by_subject"].setdefault(forward_subject_key(f["subject"], aliases), []).append(f)
    return idx


def forwards_for(best, in_window, index, aliases):
    """Forwards to Fetch that belong to the best candidate: same thread, or the same subject when the subject carries a
    receipt or invoice number (subjects like 'Your invoice from Apple.' repeat every month, so thread only for those)."""
    if not best:
        return []
    key = forward_subject_key(best["subject"], aliases)
    by_subject = bool(re.search(r"\d{3,}", key))
    found = {}
    for c in in_window:
        if c["thread_id"] == best["thread_id"] or (by_subject and forward_subject_key(c["subject"], aliases) == key):
            found[c["message_id"]] = {"message_id": c["message_id"], "thread_id": c["thread_id"], "from_addr": c["from_addr"],
                                      "date_local": c["date_local"], "subject": c["subject"]}
    for f in index.get("by_thread", {}).get(best["thread_id"], []) + (index.get("by_subject", {}).get(key, []) if by_subject else []):
        if f["date_local"] >= best["date_local"]:
            found.setdefault(f["message_id"], f)
    return sorted(found.values(), key=lambda f: f["date_local"])


def export_candidate(c):
    keys = ("rank", "message_id", "thread_id", "from", "from_domain", "reply_to", "to", "subject", "date_utc", "date_local",
            "lag_days", "attachments", "has_pdf", "has_html", "links", "class", "fetched", "score", "confidence", "score_parts",
            "amount_match", "amount_evidence", "same_thread", "notes")
    out = {k: c.get(k) for k in keys}
    out["amount_evidence"] = (c.get("amount_evidence") or [])[:12]
    out["amount_best"] = c.get("amount_best")
    return out


# ----------------------------------------------------------------------------- routing

def hint_text(row):
    src = row.get("source") or {}
    return " ".join(str(x) for x in (row.get("status_hint", ""), src.get("evidence", ""), src.get("action", "")) if x)


def route_for(row, vendor, best, second, run_date, aliases, entity="viseo", window=None, forwards=None):
    """Decide what a person (or the MCP lane) should do with the row. The Pleo company and so the forwarding identity is
    the worklist's entity; a vendor alias can only add a note. Amex rows never get a forwarding address."""
    d = date.fromisoformat(row["date"])
    age = (run_date - d).days
    deadline = d + timedelta(days=39)
    v = vendor or {}
    ffrom = FORWARD_FROM.get(entity, FORWARD_FROM["viseo"])
    out = {"route": "none", "confidence": best["confidence"] if best else "none", "route_reason": "", "forward_from": "",
           "forward_deadline": None, "age_days": age, "recheck_after": None, "pack_secondary": None}
    conf = out["confidence"]
    notes = []
    if v.get("entity") and v["entity"] != entity:
        notes.append(f"alias table lists {v.get('name') or 'this vendor'} under {ENTITY_NAMES.get(v['entity'], v['entity'])}, "
                     f"the worklist is {ENTITY_NAMES.get(entity, entity)}; confirm the company with Oscar")
    forwards = forwards or []
    already = bool(forwards) or bool(FORWARDED_HINT_RE.search(hint_text(row)))
    if best and conf in ("strong", "possible"):
        mid = best["message_id"]
        if row["ledger"] == "pleo":
            if already:
                out["route"] = "confirm"
                if forwards:
                    f = forwards[-1]
                    out["route_reason"] = (f"already forwarded {f['date_local']} from {f['from_addr']} ({f['message_id']}); "
                                           f"confirm the match in the Pleo receipt inbox, do not forward again")
                    if f["from_addr"] != ffrom:
                        notes.append(f"that forward went from {f['from_addr']}, the {ENTITY_NAMES.get(entity, entity)} identity is {ffrom}")
                else:
                    out["route_reason"] = f"already forwarded according to the input; confirm {mid} in the Pleo receipt inbox, do not forward again"
            elif age < 40:
                out["route"] = "forward"
                out["forward_from"], out["forward_deadline"] = ffrom, deadline.isoformat()
                out["route_reason"] = f"forward {mid} from {ffrom} to {FORWARD_TO} before {deadline.isoformat()}"
            else:
                out["route"] = "attach"
                target = f"on expense {row['expense_id']}" if row.get("expense_id") else f"; find the expense by receipt number {row['row_key']}"
                out["route_reason"] = f"attach {mid} via Pleo MCP {target}".replace(" ;", ";")
        elif (row.get("tag") or "") != "business":
            out["route"] = "none"
            out["route_reason"] = f"receipt found ({mid}) but the row needs Oscar's tag (now {row.get('tag') or 'untagged'}) before any pack"
        else:
            out["route"] = "pack"
            out["route_reason"] = f"include {mid} in the Fortnox or accountant pack for card {row.get('card') or row.get('account')}"
            if second and (second.get("confidence") == "strong" or
                           (second.get("confidence") == "possible" and second.get("has_pdf") and not best.get("has_pdf"))):
                out["pack_secondary"] = second["message_id"]
                out["route_reason"] += f"; secondary {second['message_id']}" + (" (carries the PDF)" if second.get("has_pdf") and not best.get("has_pdf") else "")
        if conf == "possible":
            out["route_reason"] += " (review amount first)"
        if best.get("notes"):
            notes.extend(best["notes"])
    else:
        rin = v.get("route_if_none", "none")
        recent = age <= 1
        if rin == "portal":
            out["route"] = "portal"
            out["route_reason"] = v.get("portal") or "vendor portal"
        elif rin in ("request", "ask"):
            out["route_reason"] = v.get("portal") or rin
        elif vendor and recent:
            out["route_reason"] = f"too recent, recheck after {(d + timedelta(days=2)).isoformat()}"
        else:
            span = f"{(d - window[0]).days} days before to {(window[1] - d).days} days after" if window else "the search window"
            out["route_reason"] = f"no receipt mail found {span}; check the vendor portal or ask for a paper receipt"
        if recent:
            out["recheck_after"] = (d + timedelta(days=2)).isoformat()
    if notes:
        out["route_reason"] += " (" + "; ".join(notes) + ")"
    if row["kind"] == "refund":
        out["route_reason"] = "refund " + out["route_reason"]
    hint = norm_ws(row.get("status_hint") or "")
    # a single-word hint ('portal') is compared with the route, never as a substring of the reason text
    repeated = " " in hint and hint.lower() in out["route_reason"].lower()
    if hint and not hint.lower().startswith("ready") and hint.lower() != out["route"] and not repeated:
        out["route_reason"] += f"; input hint: {hint}"
    out["route_reason"] = dash(out["route_reason"])
    return out


# ----------------------------------------------------------------------------- outputs

# Person-facing columns first, the agent's columns after. best_* is filled only for strong or possible candidates;
# a weak hint goes to weak_hint so nobody mistakes a neighbouring mail for the receipt.
CSV_COLUMNS = ["row_key", "ledger", "entity", "date", "merchant", "amount", "foreign", "tag", "account", "kind", "age_days",
               "route", "route_reason", "confidence", "forward_from", "forward_deadline", "gmail_link", "best_subject",
               "best_from", "best_date", "best_attachments", "amount_evidence", "weak_hint", "best_message_id",
               "best_thread_id", "best_lag_days", "best_score", "second_message_id", "second_score", "candidates_n",
               "forwarded", "known_message_id", "check", "error"]
SHORT_COLUMNS = ["date", "merchant", "amount", "route", "what_to_do", "deadline", "gmail_link", "mail_subject", "mail_date",
                 "confidence"]


def gmail_link(mid):
    return f"https://mail.google.com/mail/u/0/#all/{mid}" if mid else ""


def evidence_text(c):
    h = (c or {}).get("amount_best")
    if not h:
        return ""
    return dash(f"{sv_amount(h['value'], h.get('currency') or '?')} ({h.get('source', 'body')}: {norm_ws(h.get('context', ''))[:60]})")


def write_json(path, run, rows):
    atomic_write_text(path, json.dumps({"run": run, "rows": rows}, ensure_ascii=False, indent=1, default=str))


def csv_row(r):
    best = r["candidates"][0] if r["candidates"] else None
    second = r["candidates"][1] if len(r["candidates"]) > 1 else None
    shown = best if best and best.get("confidence") in ("strong", "possible") else None
    f = r.get("foreign")
    weak = ""
    if best and not shown:
        weak = dash(f"weak {best['score']}: {best['from']} / {best['subject']} ({best['date_local']})")
    return {
        "row_key": r["row_key"], "ledger": r["ledger"], "entity": r.get("entity", ""), "date": r["date"],
        "merchant": dash(r["merchant"]), "amount": sv_amount(r["amount_sek"]) if r["amount_sek"] is not None else "",
        "foreign": sv_amount(f["amount"], f["currency"]) if f else "", "tag": r.get("tag") or "", "account": r.get("account") or "",
        "kind": r["kind"], "age_days": r.get("age_days"), "route": r["route"], "route_reason": dash(r["route_reason"]),
        "confidence": r["confidence"], "forward_from": r.get("forward_from") or "", "forward_deadline": r.get("forward_deadline") or "",
        "gmail_link": gmail_link(shown["message_id"]) if shown else "",
        "best_subject": dash(shown["subject"]) if shown else "", "best_from": dash(shown["from"]) if shown else "",
        "best_date": shown["date_local"] if shown else "",
        "best_attachments": dash("; ".join(a["filename"] for a in shown["attachments"])) if shown else "",
        "amount_evidence": evidence_text(shown), "weak_hint": weak,
        "best_message_id": shown["message_id"] if shown else "", "best_thread_id": shown["thread_id"] if shown else "",
        "best_lag_days": shown["lag_days"] if shown else "", "best_score": shown["score"] if shown else "",
        "second_message_id": second["message_id"] if second else "", "second_score": second["score"] if second else "",
        "candidates_n": len(r["candidates"]),
        "forwarded": "; ".join(f"{x['date_local']} {x['message_id']}" for x in r.get("forwards") or []),
        "known_message_id": r.get("known_message_id", ""), "check": r.get("check", ""), "error": dash(r.get("error") or ""),
    }


def _write_rows(path, columns, dicts):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=columns)
        w.writeheader()
        for d in dicts:
            w.writerow(d)
    os.replace(tmp, p)


def write_csv(path, rows):
    _write_rows(path, CSV_COLUMNS, [csv_row(r) for r in rows])


def write_short_csv(path, rows):
    """The worklist as a person reads it: one plain sentence per row, a deadline only where a forward is due."""
    out = []
    for r in rows:
        c = csv_row(r)
        out.append({"date": c["date"], "merchant": c["merchant"], "amount": c["amount"], "route": c["route"],
                    "what_to_do": c["route_reason"], "deadline": c["forward_deadline"] if c["route"] == "forward" else "",
                    "gmail_link": c["gmail_link"], "mail_subject": c["best_subject"], "mail_date": c["best_date"],
                    "confidence": c["confidence"]})
    _write_rows(path, SHORT_COLUMNS, out)


# ----------------------------------------------------------------------------- regression

POSITIVE_SETS = {"ground_truth", "positive", "positive_medium", "matching"}
INFO_SETS = {"pending", "positive_weak"}


def load_check(path, args):
    fmt, rows = load_worklist(path, args)
    checks = {}
    for r in rows:
        if fmt == "matching_csv":
            checks[r["row_key"]] = {"set": "matching", "expected": r.get("known_message_id", ""), "secondary": "", "notes": ""}
        elif fmt == "regression_set":
            checks[r["row_key"]] = r["check"]
        else:
            raise SystemExit(f"--check expects MATCHING.csv or regression_set.csv, got {fmt}")
    return fmt, rows, checks


def evaluate_check(res, chk, aliases):
    ids = [c["message_id"] for c in res["candidates"]]
    strong = aliases["defaults"]["strong"]
    expected = chk.get("expected") or ""
    secondary = chk.get("secondary") or ""
    s = chk.get("set") or "matching"
    best = res["candidates"][0] if res["candidates"] else None
    note = []
    rank_ = ids.index(expected) + 1 if expected in ids else None
    if res.get("error"):
        return "FAIL", rank_, f"error: {res['error'][:60]}"
    if not res.get("window"):
        return "SKIP", None, res.get("route_reason") or "not searched"
    if s in INFO_SETS:
        return "INFO", rank_, "reported only"
    if s == "negative" or expected.upper() == "NONE":
        decoys = [d for d in HEX16_RE.findall(chk.get("notes") or "")]
        seen = {c["message_id"]: c["score"] for c in res["candidates"] + res["rejected"]}
        for d in decoys:
            if d in seen:
                note.append(f"decoy {d} scored {seen[d]}")
        if best and best["confidence"] == "strong":
            return "FAIL", None, "decoy reached strong" + ("; " + "; ".join(note) if note else "")
        return "PASS", None, "; ".join(note)
    if s == "forward_only":
        if secondary and secondary in ids:
            return "PASS", ids.index(secondary) + 1, "forward id found"
        return "FAIL", None, "forward id not among candidates"
    if not expected:
        return "SKIP", None, "no expected id"
    if rank_:
        if secondary:
            note.append(f"secondary {'present' if secondary in ids else 'absent'}")
        if rank_ == 1:
            note.append("strict yes")
            return "PASS", rank_, "; ".join(note)
        if s == "positive_medium":
            return "PASS", rank_, "; ".join(note + [f"rank {rank_} accepted for positive_medium"])
        return "FAIL", rank_, "; ".join(note + [f"expected id ranked {rank_}, best is {best['message_id'] if best else '-'}"])
    return "FAIL", None, "expected id not among candidates"


def run_check(results, checks, aliases, counters):
    lines = []
    order = {"FAIL": 0, "PASS": 1, "INFO": 2, "SKIP": 3}
    for res in results:
        chk = checks.get(res["row_key"])
        if not chk:
            continue
        verdict, rank_, note = evaluate_check(res, chk, aliases)
        res["check"] = verdict.lower()
        res["check_rank"] = rank_
        best = res["candidates"][0] if res["candidates"] else None
        lines.append((order[verdict], {
            "result": verdict, "set": chk.get("set") or "matching", "row_key": res["row_key"], "date": res["date"],
            "merchant": res["merchant"][:24], "amount": sv_amount(res["amount_sek"]) if res["amount_sek"] is not None else "",
            "expected": (chk.get("expected") or "-")[:16], "rank": str(rank_) if rank_ else "-",
            "best": best["message_id"] if best else "-", "score": str(best["score"]) if best else "-", "note": note}))
    lines.sort(key=lambda t: t[0])
    widths = [("result", 6), ("set", 13), ("row_key", 24), ("date", 10), ("merchant", 24), ("amount", 14), ("expected", 16),
              ("rank", 4), ("best", 16), ("score", 5)]
    print("  ".join(f"{k:<{w}}" for k, w in widths) + "  note")
    for _, ln in lines:
        print(dash("  ".join(f"{str(ln[k]):<{w}}" for k, w in widths) + "  " + ln["note"]))
    n = {k: sum(1 for o, ln in lines if ln["result"] == k) for k in ("PASS", "FAIL", "INFO", "SKIP")}
    at_rank1 = sum(1 for _, ln in lines if ln["rank"] == "1")
    cached = counters["list_cached"] + counters["metadata_cached"] + counters["full_cached"]
    print(f"{len(lines)} rows: {n['PASS']} pass, {n['FAIL']} fail, {n['INFO']} info, {n['SKIP']} skipped; {at_rank1} at rank 1. "
          f"API: {counters['list']} lists, {counters['metadata']} metadata, {counters['full']} full, {cached} cached.")
    return n["FAIL"]


# ----------------------------------------------------------------------------- main

def parse_args(argv):
    ap = argparse.ArgumentParser(description="Read-only Gmail receipt matcher (see docstring).")
    ap.add_argument("worklist", nargs="?", help="ledger CSV or JSON (omit when --check is the worklist)")
    ap.add_argument("--reason", required=True, help="audit reason, written to every API audit record")
    ap.add_argument("--out", default=str(ROOT / "out" / "gmail-matches.json"), help="JSON result (default in the repo's out/)")
    ap.add_argument("--csv", default=str(ROOT / "out" / "gmail-matches.csv"), help="full CSV (default in the repo's out/)")
    ap.add_argument("--short-csv", default=None, help="optional person-facing CSV: date, merchant, amount, route, what to do")
    ap.add_argument("--entity", choices=sorted(FORWARD_FROM), default="viseo",
                    help="which company's Pleo the worklist belongs to; sets the address to forward from (default viseo)")
    ap.add_argument("--aliases", default=str(Path(__file__).resolve().parent / "vendor_aliases.json"))
    ap.add_argument("--cache", default=str(ROOT / "out" / "gmail-cache.json"), help="list and metadata cache (default in the repo's out/)")
    ap.add_argument("--days-before", type=int, default=10)
    ap.add_argument("--days-after", type=int, default=5)
    ap.add_argument("--max-per-row", type=int, default=5)
    ap.add_argument("--max-full", type=int, default=6)
    ap.add_argument("--run-date", default=None, help="YYYY-MM-DD, default today in Europe/Stockholm")
    ap.add_argument("--rows", default="", help="comma-separated row keys to restrict the run")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--all-tags", action="store_true", help="Amex: also search personal and skip rows")
    ap.add_argument("--all-rows", action="store_true", help="Pleo export: also search rows that already have a receipt")
    ap.add_argument("--refresh", action="store_true", help="ignore cached list results")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="print queries and windows, no API call, no output files")
    ap.add_argument("--sleep-ms", type=int, default=0)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--check", default=None, help="regression file: out/receipts/MATCHING.csv or regression_set.csv")
    a = ap.parse_args(argv)
    if not (a.reason or "").strip():
        ap.error("--reason must not be empty")
    if not a.worklist and not a.check:
        ap.error("WORKLIST or --check is required")
    return a


def main(argv=None):
    try:
        args = parse_args(argv)
    except SystemExit as e:
        return 2 if e.code else 0
    try:
        run_date = date.fromisoformat(args.run_date) if args.run_date else datetime.now(TZ).date()
    except ValueError:
        print("--run-date must be YYYY-MM-DD", file=sys.stderr)
        return 2
    try:
        mailbox = g.assert_mailbox_allowed(MAILBOX)
        aliases = load_aliases(args.aliases)
        checks = {}
        if args.check:
            cfmt, crows, checks = load_check(args.check, args)
        if args.worklist:
            fmt, rows = load_worklist(args.worklist, args)
            input_path = args.worklist
        else:
            fmt, rows, input_path = cfmt, crows, args.check
        rows_in = len(rows)
        if args.rows:
            wanted = {k.strip() for k in args.rows.split(",") if k.strip()}
            rows = [r for r in rows if r["row_key"] in wanted]
        if args.limit:
            rows = rows[:args.limit]
        cache = Cache(args.cache, enabled=not args.no_cache and not args.dry_run)
        client = GmailClient(mailbox, args.reason, cache, sleep_ms=args.sleep_ms, dry_run=args.dry_run, refresh=args.refresh)
    except g.AccessError as e:
        print(str(e), file=sys.stderr)
        return 2
    except SystemExit as e:
        print(str(e), file=sys.stderr)
        return 2
    print(f"{input_path}: format {fmt}, {rows_in} rows, {len(rows)} selected, {sum(1 for r in rows if r.get('search', True))} to search,"
          f" run date {run_date}, entity {ENTITY_NAMES[args.entity]} (forward from {FORWARD_FROM[args.entity]})", file=sys.stderr)
    ctx = {"entity": args.entity, "forward_index": {}}
    if not args.dry_run:
        try:
            ctx["forward_index"] = build_forward_index(client, rows, run_date, aliases)
        except g.AccessError as e:
            warn(f"forward index unavailable ({e}); forwards are only detected inside each row's window")
        finally:
            cache.save()
    results = []
    for i, row in enumerate(rows, 1):
        res = match_row(row, aliases, client, args, run_date, ctx)
        results.append(res)
        if not args.dry_run:
            best = res["candidates"][0] if res["candidates"] else None
            line = f"[{i}/{len(rows)}] {row['row_key']} {row['date']} {row['merchant'][:28]:28} -> {res['route']:7} {res['confidence']:8}"
            if best:
                line += f" {best['message_id']} {best['score']}"
            if res.get("error"):
                line += " ERROR"
            if args.verbose or i == len(rows) or i % 5 == 0 or best:
                print(line, file=sys.stderr)
    if args.dry_run:
        print(f"dry run: {len(results)} rows, {sum(len(r['queries']) for r in results)} queries, no API call made", file=sys.stderr)
        return 0
    failures = 0
    if checks:
        failures = run_check(results, checks, aliases, client.counters)
    routes = {}
    for r in results:
        routes[r["route"]] = routes.get(r["route"], 0) + 1
    purged = cache.purge()
    cache.save()
    fi = ctx.get("forward_index") or {}
    run = {"script": "gmail_match.py", "version": SCRIPT_VERSION, "run_date": run_date.isoformat(),
           "run_at": datetime.now(TZ).isoformat(timespec="seconds"), "input": input_path, "input_format": fmt,
           "entity": args.entity, "forward_from": FORWARD_FROM[args.entity],
           "rows_in": rows_in, "rows_searched": sum(1 for r in results if r.get("window")), "mailbox": mailbox,
           "reason": args.reason, "aliases": args.aliases, "aliases_sha1": aliases["_sha1"],
           "args": {"days_before": args.days_before, "days_after": args.days_after, "max_per_row": args.max_per_row,
                    "max_full": args.max_full, "all_tags": args.all_tags, "all_rows": args.all_rows, "check": args.check},
           "forward_index": {"query": fi.get("query"), "n": fi.get("n", 0)}, "cache_purged": purged,
           "api": dict(client.counters), "routes": routes}
    write_json(args.out, run, results)
    write_csv(args.csv, results)
    if args.short_csv:
        write_short_csv(args.short_csv, results)
    c = client.counters
    print(f"wrote {args.out} and {args.csv}{' and ' + args.short_csv if args.short_csv else ''}: routes {routes}; "
          f"API list {c['list']} metadata {c['metadata']} full {c['full']}"
          f" (cached {c['list_cached']}/{c['metadata_cached']}/{c['full_cached']}, retries {c['retries']}, errors {c['errors']});"
          f" cache entries purged {purged}", file=sys.stderr)
    errors = sum(1 for r in results if r.get("error"))
    return 1 if (failures or errors) else 0


if __name__ == "__main__":
    sys.exit(main())
