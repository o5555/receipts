#!/usr/bin/env python3
"""Fetch Gmail receipts for one month of classified Amex business rows (Viseo AB).

Automates Lane C step (d), replacing the manual per-message gmail_fetch calls.
Takes the classified ledger from scripts/classify.py, selects the month's
business charges for Viseo AB, and decides the receipt-bearing Gmail message
for each row: a known evidence id if the ledger notes one ('Gmail <16 hex>'),
otherwise scripts/gmail_match.py's top candidate when gmail_match itself calls
the match strong. Matching is never reimplemented here: live runs drive
gmail_match as a subprocess and read its JSON output; an uncertain match is
skipped with a note, never fetched blindly. Attachments are fetched with
scripts/gmail_fetch.py save (prefix = the Amex ref), then junk is removed:
files named like terms/conditions/tos/agreement, and anything that is neither
PDF nor image. HTML-only receipts are reported for manual handling
(gmail_fetch html); rendering to PDF is out of scope here. Refs that already
have a file in --out-dir are never fetched again. The result map CSV
(columns ref,file, both pre-existing and new files) is what
scripts/fortnox_lane.py --receipts-map consumes.

Usage:
  scripts/amex_receipts.py out/amex.classified2.csv --month 2026-07 --reason "..." --dry-run
  scripts/amex_receipts.py out/amex.classified2.csv --month 2026-07 --reason "..." \
      [--out-dir out/receipts-amex] [--map-csv out/receipts-amex/map-2026-07.csv] \
      [--min-confidence strong|possible]

Confidence: 'strong' is gmail_match's top tier (score >= 70, at least two
independent signals); 'possible' is its 'review amount first' tier. The default
fetches strong only; --min-confidence possible also accepts possible.

--dry-run makes no network call of any kind: gmail_match is not invoked as a
subprocess and gmail_fetch never runs. Instead gmail_match's own scoring is
replayed in-process against the existing out/gmail-cache.json, strictly
read-only; rows whose mail is not in the cache are reported as 'cache-miss'
(a live run will search them). Live mode runs gmail_match once for the rows
that still need a decision (its matches JSON and CSV land next to the map),
then gmail_fetch save per row; every API call carries --reason into Thor's
audit log. Nothing here sends mail, forwards to Fetch, or writes to Pleo or
Fortnox.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import zoneinfo
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = Path(__file__).resolve().parent
CACHE = ROOT / "out" / "gmail-cache.json"
TZ = zoneinfo.ZoneInfo("Europe/Stockholm")
KNOWN_ID_RE = re.compile(r"\bGmail\s+([0-9a-f]{16})\b")
LEDGER_COLS = {"date", "amount", "merchant", "tag", "ref", "entity"}
KEEP_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".bmp", ".heic"}
JUNK_NAME_RE = re.compile(r"terms|condition|agreement", re.I)
TOS_RE = re.compile(r"(?:^|[^a-z])tos(?:[^a-z]|$)", re.I)  # 'tos.pdf' yes, 'photos.pdf' no
CONF_RANK = {"strong": 2, "possible": 1}


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def sv_amount(x, cur="SEK"):
    """78429.63 -> '78 429,63 SEK' (ASCII space as thousands separator)."""
    s = f"{abs(x):,.2f}".replace(",", "\x00").replace(".", ",").replace("\x00", " ")
    return f"{s} {cur}".strip()


def keep_attachment(filename, mime=""):
    """(keep, reason_if_not): junk names out, then only PDF or image by mime or extension."""
    name = filename or ""
    if JUNK_NAME_RE.search(name) or TOS_RE.search(name):
        return False, "terms or conditions file"
    mime = (mime or "").lower()
    ext = os.path.splitext(name)[1].lower()
    if mime == "application/pdf" or mime.startswith("image/") or ext in KEEP_EXTS:
        return True, ""
    return False, f"neither pdf nor image ({mime or ext or 'no type'})"


# ----------------------------------------------------------------------------- ledger

def load_ledger(path):
    """classify.py output, .json list of objects or CSV; needs date, amount, merchant, tag, ref, entity."""
    if str(path).endswith(".json"):
        data = json.load(open(path, encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise SystemExit(f"{path}: JSON ledger must be a list of objects")
        raw, have = data, set(data[0]) if data else set()
    else:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            raw, have = list(rd), set(rd.fieldnames or [])
    missing = sorted(LEDGER_COLS - have)
    if missing:
        raise SystemExit(f"{path}: not a classify.py ledger, missing columns: {', '.join(missing)}")
    rows = []
    for i, r in enumerate(raw, 1):
        try:
            amount = float(str(r.get("amount", "")).replace(",", "."))
        except ValueError:
            raise SystemExit(f"{path} row {i} (ref {r.get('ref') or '?'}): bad amount {r.get('amount')!r}")
        row = {k: re.sub(r"\s+", " ", str(r.get(k) or "")).strip() for k in
               ("date", "merchant", "tag", "ref", "entity", "confirm", "why", "pack_status")}
        row["amount"] = amount
        rows.append(row)
    return rows


def select_month(rows, month):
    """Business rows of the month. Viseo rows are selected; business rows of another
    entity and refunds/payments are listed as skipped, never silently dropped."""
    selected, skipped = [], []
    for r in rows:
        if not r["date"].startswith(month) or r["tag"] != "business":
            continue
        if "BETALNING MOTTAGEN" in r["merchant"].upper():
            continue
        if (r["entity"] or "viseo") != "viseo":
            skipped.append((r, f"entity {r['entity']}, not Viseo AB; keep the forward path for it"))
        elif r["amount"] <= 0:
            skipped.append((r, "refund or payment, no receipt to fetch"))
        else:
            selected.append(r)
    selected.sort(key=lambda r: (r["date"], r["ref"]))
    return selected, skipped


def ledger_known_id(row):
    m = KNOWN_ID_RE.search(" ".join(row.get(k, "") for k in ("confirm", "why", "pack_status")))
    return m.group(1) if m else ""


def files_for(out_dir, ref, names):
    return sorted(n for n in names if n.startswith(ref + "_"))


# ----------------------------------------------------------------------------- matching

def match_live(ledger_path, refs, args):
    """Run gmail_match once for the refs that still need a decision; read its JSON output."""
    out_json = Path(args.out_dir) / f"matches-{args.month}.json"
    out_csv = Path(args.out_dir) / f"matches-{args.month}.csv"
    cmd = [sys.executable, str(args.gmail_match), str(ledger_path), "--reason", args.reason,
           "--entity", "viseo", "--rows", ",".join(refs),
           "--out", str(out_json), "--csv", str(out_csv)]
    p = subprocess.run(cmd, text=True)
    if p.returncode not in (0, 1):  # 1 = per-row errors, still usable; anything else is a hard stop
        raise SystemExit(f"gmail_match failed with exit code {p.returncode}: {' '.join(cmd)}")
    data = json.load(open(out_json, encoding="utf-8"))
    return {r["row_key"]: r for r in data["rows"]}


def match_offline(ledger_path, refs, reason, run_date):
    """Replay gmail_match's own scoring from out/gmail-cache.json, strictly read-only.
    Its GmailClient(dry_run=True) has no session and raises before any HTTP request,
    so a row whose lists or messages are not cached comes back with an error, not a search."""
    sys.path.insert(0, str(SCRIPTS))
    import gmail_match as gm
    ns = argparse.Namespace(all_tags=False, all_rows=False, days_before=10, days_after=5,
                            max_per_row=5, max_full=6, dry_run=False)
    fmt, work = gm.load_worklist(str(ledger_path), ns)
    aliases = gm.load_aliases(str(SCRIPTS / "vendor_aliases.json"))
    cache = gm.Cache(str(CACHE), enabled=True)
    cache.save = lambda: None  # belt and braces: a dry run never writes the cache
    client = gm.GmailClient(gm.MAILBOX, reason, cache, dry_run=True)
    ctx = {"entity": "viseo", "forward_index": {}}
    wanted = set(refs)
    results = {}
    for row in work:
        if row["row_key"] in wanted:
            results[row["row_key"]] = gm.match_row(row, aliases, client, ns, run_date, ctx)
    return results


def decide(res, min_conf):
    """(message ids to try in order, skip note). Reuses gmail_match's own routing: a business
    Amex row is fetchable only when it routed 'pack', at or above the required confidence."""
    if res is None:
        return [], "no result from gmail_match for this ref"
    if res.get("error"):
        return [], ""
    conf = res.get("confidence") or "none"
    if res.get("route") != "pack" or CONF_RANK.get(conf, 0) < CONF_RANK[min_conf]:
        note = res.get("route_reason") or "no confident match"
        if res.get("route") == "pack" and conf == "possible":
            return [], f"possible only, not fetched: {note} (accept with --min-confidence possible)"
        return [], f"{res.get('route') or 'none'}/{conf}: {note}"
    ids = [res["best"]]
    if res.get("pack_secondary"):
        ids.append(res["pack_secondary"])
    return ids, ""


def candidate_of(res, mid):
    return next((c for c in (res or {}).get("candidates") or [] if c.get("message_id") == mid), None)


def precheck(cand):
    """From gmail_match's attachment metadata (present when it full-fetched the message):
    'fetch' with the keepable names, 'html' (HTML-only receipt), 'none', or 'unknown'."""
    if not cand or cand.get("fetched") != "full":
        return "unknown", []
    keep = [a["filename"] for a in cand.get("attachments") or []
            if keep_attachment(a.get("filename"), a.get("mime_type"))[0]]
    if keep:
        return "fetch", keep
    if cand.get("has_html"):
        return "html", []
    return "none", []


# ----------------------------------------------------------------------------- fetch

def fetch_message(mid, ref, args):
    """gmail_fetch save, then remove junk among what it saved. Returns (kept, junk, error)."""
    cmd = [sys.executable, str(args.gmail_fetch), "save", mid, str(args.out_dir),
           "--prefix", ref, "--reason", args.reason]
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode:
        return [], [], (p.stderr or p.stdout).strip().splitlines()[-1:] or ["gmail_fetch failed"]
    try:
        saved = json.loads(p.stdout or "[]")
    except ValueError:
        return [], [], [f"gmail_fetch printed no JSON: {p.stdout[:120]!r}"]
    kept, junk = [], []
    for s in saved:
        name = os.path.basename(s["file"])
        ok, why = keep_attachment(name, s.get("mime"))
        if ok:
            kept.append(name)
        else:
            os.remove(s["file"])
            junk.append(f"{name}: {why}")
    return kept, junk, []


def html_hint(mid, ref, args):
    return (f"HTML-only receipt in {mid}; manual: scripts/gmail_fetch.py html {mid} "
            f"{os.path.join(str(args.out_dir), ref + '.html')} --reason \"...\"")


def resolve_row(row, mids, res, args):
    """Try the candidate messages in order; first one that yields a kept file wins.
    Returns (status, files, note)."""
    saw_html = ""
    for mid in mids:
        kind, names = precheck(candidate_of(res, mid))
        if kind == "html":
            saw_html = saw_html or html_hint(mid, row["ref"], args)
            continue
        if kind == "none":
            continue
        if args.dry_run:
            return "would-fetch", names, f"message {mid}" + ("" if names else " (attachment list not cached, fetch live)")
        kept, junk, err = fetch_message(mid, row["ref"], args)
        if err:
            return "error", [], f"message {mid}: {'; '.join(err)}"
        note = f"message {mid}" + (f"; junk removed: {'; '.join(junk)}" if junk else "")
        if kept:
            return "fetched", kept, note
        cand = candidate_of(res, mid)
        if cand is None or cand.get("has_html"):
            saw_html = saw_html or html_hint(mid, row["ref"], args)
        if junk:
            saw_html = saw_html or f"message {mid}: only junk attachments ({'; '.join(junk)})"
    if saw_html:
        return "html-only", [], saw_html
    return "no-attachment", [], f"no usable attachment and no HTML body in {', '.join(mids)}; check the mail's links"


# ----------------------------------------------------------------------------- outputs

def write_map(path, out_dir, refs):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    names = os.listdir(out_dir) if os.path.isdir(out_dir) else []
    n = 0
    with open(p, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ref", "file"])
        for ref in sorted(refs):
            for name in files_for(out_dir, ref, names):
                w.writerow([ref, os.path.join(str(out_dir), name)])
                n += 1
    return n


def report(records, month, n_sel, total, args, map_rows):
    extra = len(records) - n_sel
    tail = f", plus {extra} listed out of scope" if extra else ""
    print(f"Viseo AB business rows {month}: {n_sel} ({sv_amount(total)}){tail}")
    for rec in records:
        r = rec["row"]
        detail = "; ".join(rec["files"]) if rec["files"] else ""
        if rec["note"]:
            detail = f"{detail}  ({rec['note']})" if detail else rec["note"]
        print(f"  {rec['status']:<13} {r['date']}  {r['ref']:<23}  {r['merchant'][:34]:<34} "
              f"{sv_amount(r['amount']):>14}  {detail}")
    counts = {}
    for rec in records:
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    covered = sum(1 for rec in records if rec["files"])
    files_new = sum(len(rec["files"]) for rec in records if rec["status"] == "fetched")
    order = ["already", "fetched", "would-fetch", "html-only", "no-attachment", "skipped", "cache-miss", "error"]
    label = {"already": "already in out-dir",
             "fetched": f"fetched now ({files_new} file{'s' if files_new != 1 else ''})",
             "would-fetch": "would fetch (dry run)", "html-only": "html-only, manual",
             "no-attachment": "no usable attachment", "skipped": "skipped (uncertain, portal, other entity or refund)",
             "cache-miss": "not in cache, live run will search", "error": "errors"}
    for k in order:
        if counts.get(k):
            print(f"  {label[k]:<38}: {counts[k]}")
    print(f"  receipt file present after this run   : {covered} of {n_sel} rows")
    if args.dry_run:
        print(f"dry run: nothing fetched, map not written (would be {args.map_csv})")
    else:
        print(f"map written: {args.map_csv} ({map_rows} rows)")


# ----------------------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description="Fetch Gmail receipts for a month of classified Amex business rows (see docstring).")
    ap.add_argument("classified", help="classified ledger from scripts/classify.py (CSV or JSON)")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out-dir", default=str(ROOT / "out" / "receipts-amex"), help="receipt files land here, named <ref>_<attachment>")
    ap.add_argument("--map-csv", default=None, help="ref,file map for fortnox_lane --receipts-map (default <out-dir>/map-<month>.csv)")
    ap.add_argument("--reason", required=True, help="audit reason, passed to gmail_match and gmail_fetch")
    ap.add_argument("--min-confidence", choices=("strong", "possible"), default="strong",
                    help="gmail_match confidence required to fetch (default strong, its top tier)")
    ap.add_argument("--dry-run", action="store_true", help="no gmail_match subprocess, no fetch; replay from out/gmail-cache.json read-only")
    ap.add_argument("--run-date", default=None, help="YYYY-MM-DD for age/route computation, default today in Europe/Stockholm")
    ap.add_argument("--gmail-match", default=str(SCRIPTS / "gmail_match.py"), help="gmail_match.py to drive (tests point this at a stub)")
    ap.add_argument("--gmail-fetch", default=str(SCRIPTS / "gmail_fetch.py"), help="gmail_fetch.py to drive (tests point this at a stub)")
    args = ap.parse_args(argv)
    if not re.fullmatch(r"\d{4}-\d{2}", args.month):
        ap.error("--month must be YYYY-MM")
    if not args.reason.strip():
        ap.error("--reason must not be empty")
    args.map_csv = args.map_csv or os.path.join(str(args.out_dir), f"map-{args.month}.csv")
    try:
        run_date = (datetime.strptime(args.run_date, "%Y-%m-%d").date() if args.run_date
                    else datetime.now(TZ).date())
    except ValueError:
        ap.error("--run-date must be YYYY-MM-DD")

    rows = load_ledger(args.classified)
    selected, skipped_pre = select_month(rows, args.month)
    if not selected and not skipped_pre:
        print(f"{args.classified}: no business rows in {args.month}", file=sys.stderr)
        return 0
    existing_names = os.listdir(args.out_dir) if os.path.isdir(args.out_dir) else []

    records, to_match, known = [], [], {}
    for r in selected:
        have = files_for(args.out_dir, r["ref"], existing_names)
        if have:
            records.append({"row": r, "status": "already", "files": have, "note": ""})
        elif ledger_known_id(r):
            known[r["ref"]] = ledger_known_id(r)
            records.append({"row": r, "status": "", "files": [], "note": ""})
        else:
            to_match.append(r)
            records.append({"row": r, "status": "", "files": [], "note": ""})
    for r, why in skipped_pre:
        records.append({"row": r, "status": "skipped", "files": [], "note": why})
    records.sort(key=lambda rec: (rec["row"]["date"], rec["row"]["ref"]))

    results = {}
    if to_match:
        refs = [r["ref"] for r in to_match]
        if args.dry_run:
            print(f"dry run: replaying gmail_match scoring for {len(refs)} rows from {CACHE} (read-only)", file=sys.stderr)
            results = match_offline(args.classified, refs, args.reason, run_date)
        else:
            Path(args.out_dir).mkdir(parents=True, exist_ok=True)
            results = match_live(args.classified, refs, args)

    for rec in records:
        if rec["status"]:
            continue
        r = rec["row"]
        if r["ref"] in known:
            mid = known[r["ref"]]
            if args.dry_run:
                rec["status"], rec["note"] = "would-fetch", f"known evidence id {mid} in the ledger"
            else:
                Path(args.out_dir).mkdir(parents=True, exist_ok=True)
                kept, junk, err = fetch_message(mid, r["ref"], args)
                if err:
                    rec["status"], rec["note"] = "error", f"message {mid}: {'; '.join(err)}"
                elif kept:
                    rec["status"], rec["files"] = "fetched", kept
                    rec["note"] = f"known id {mid}" + (f"; junk removed: {'; '.join(junk)}" if junk else "")
                else:
                    rec["status"], rec["note"] = "html-only", html_hint(mid, r["ref"], args)
            continue
        res = results.get(r["ref"])
        if res is not None and res.get("error"):
            rec["status"] = "cache-miss" if args.dry_run else "error"
            rec["note"] = ("row not fully in the cache; a live run will search Gmail" if args.dry_run
                           else f"gmail_match error: {res['error']}")
            continue
        mids, skip_note = decide(res, args.min_confidence)
        if not mids:
            rec["status"], rec["note"] = "skipped", skip_note
            continue
        rec["status"], rec["files"], rec["note"] = resolve_row(r, mids, res, args)

    map_rows = 0
    if not args.dry_run:
        map_rows = write_map(args.map_csv, args.out_dir, [r["ref"] for r in selected])
    report(records, args.month, len(selected), sum(r["amount"] for r in selected), args, map_rows)
    return 1 if any(rec["status"] == "error" for rec in records) else 0


if __name__ == "__main__":
    sys.exit(main())
