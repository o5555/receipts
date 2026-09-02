#!/usr/bin/env python3
"""Classify a normalised Amex ledger into business/personal/needs-tag/skip and attach kontering.

Input is the RAW output of scripts/amex_import.py (JSON list with date, merchant, account,
card, member, amount, kind, foreign, ext, statement_text, city, country, ref, source_file).
Output is the amex_classified format that scripts/gmail_match.py ingests directly, extended
with entity/bas_account/vat_regime/konto_name for scripts/fortnox_lane.py.

Rule order per row (first hit wins), encoding Oscar's rulings (OBrain projects/pleo-api.md):
  1. payment rows (BETALNING MOTTAGEN)                          -> skip
  2. negative amounts (refunds)                                 -> skip
  3. per-ref override in --overrides (Oscar's answers)          -> that tag
  4. per-ref tag in --prior (earlier classified ledger)         -> that tag, needs-tag included:
     rows already queued for Oscar stay queued until his answer lands in --overrides
     (never infer tags for rows a human judged ambiguous)
  5. KINTO on -13003 within 2026-04-20..2026-06-04              -> business (bounded exception)
  6. -13003                                                     -> personal (personal-only card)
  7. -61006 on/after 2026-06-04                                 -> personal; business-looking
     merchants (rule tag business) -> needs-tag (review/process-failure)
  8. merchant rule with a tag in --rules                        -> that tag
  9. before the card split (2026-06-15, "mid-June")             -> needs-tag (merchant decides
     pre-split and only Oscar's word is final)
 10. card defaults post-split: -61022 business, -62004 personal; anything else -> needs-tag

Kontering fields come from the matching merchant rule regardless of how the tag was decided
(rules with tag null contribute kontering only). Entity defaults to viseo.

Usage:
  scripts/classify.py out/amex-raw-<window>.json \
      --prior out/amex-2026-05-06_2026-08-02.classified.json \
      --out-json out/amex-<window>.classified.json --out-csv out/amex-<window>.classified.csv \
      [--overrides data/tags/overrides.csv] [--needs-tag-csv out/amex-needs-tag.csv]

Overrides CSV columns: ref,tag,note. Add a row per answer from Oscar; overrides beat everything
except payment/refund detection.
"""

import argparse
import csv
import datetime
import json
import os
import sys

SPLIT_DATE = datetime.date(2026, 6, 15)  # "since mid-June" (Oscar, 2026-06-24); approximate on purpose
KINTO_EXC_START = datetime.date(2026, 4, 20)
KINTO_EXC_END = datetime.date(2026, 6, 4)
R61006_FROM = datetime.date(2026, 6, 4)

CSV_COLUMNS = ["date", "card", "account", "amount", "merchant", "tag", "why", "in_pleo",
               "confirm", "dash_id", "pack_status", "ext", "ref", "period", "entity",
               "bas_account", "vat_regime", "konto_name"]

VALID_TAGS = {"business", "personal", "needs-tag", "skip"}


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def warn(msg):
    print(f"warning: {msg}", file=sys.stderr)


def load_rules(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rules = data.get("rules") if isinstance(data, dict) else data
    if not isinstance(rules, list):
        die(f"{path}: expected a rules list")
    for r in rules:
        r["_patterns"] = [p.upper() for p in r.get("patterns", []) if p]
        tag = r.get("tag")
        if tag is not None and tag not in VALID_TAGS:
            die(f"{path}: rule {r.get('name')!r} has invalid tag {tag!r}")
    return rules


def match_rule(rules, merchant):
    m = (merchant or "").upper()
    for r in rules:
        if any(p in m for p in r["_patterns"]):
            return r
    return None


def load_overrides(path):
    out = {}
    if not path:
        return out
    if not os.path.exists(path):
        warn(f"overrides file {path} not found, continuing without")
        return out
    with open(path, encoding="utf-8", newline="") as f:
        for i, row in enumerate(csv.DictReader(f), 2):
            ref = (row.get("ref") or "").strip()
            tag = (row.get("tag") or "").strip()
            if not ref or tag not in VALID_TAGS:
                warn(f"{path} line {i}: skipped (need ref and a valid tag)")
                continue
            out[ref] = {"tag": tag, "note": (row.get("note") or "").strip()}
    return out


def load_prior(path):
    out = {}
    if not path:
        return out
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows") if isinstance(data, dict) else data
    for r in rows or []:
        ref = str(r.get("ref") or "").strip()
        if ref:
            out[ref] = r
    return out


def parse_date(s):
    return datetime.date.fromisoformat(str(s).strip())


def classify_row(r, rules, overrides, prior):
    merchant = (r.get("merchant") or "").strip()
    ref = str(r.get("ref") or "").strip()
    account = str(r.get("account") or "").strip()
    amount = float(r.get("amount") or 0.0)
    d = parse_date(r.get("date"))
    rule = match_rule(rules, merchant)
    prev = prior.get(ref, {})

    out = {
        "date": r.get("date"),
        "card": str(r.get("card") or account[-4:]),
        "account": account,
        "amount": amount,
        "merchant": merchant,
        "in_pleo": "yes" if prev.get("in_pleo") else "",
        "confirm": prev.get("confirm") or "",
        "dash_id": prev.get("dash_id") or "",
        "pack_status": prev.get("pack_status") or prev.get("dash_status") or "",
        "ext": r.get("ext") or "",
        "ref": ref,
        "period": r.get("source_file") or "",
        "entity": (rule.get("entity") if rule else "") or "viseo",
        "bas_account": (rule.get("bas_account") if rule else "") or "",
        "vat_regime": (rule.get("vat_regime") if rule else "") or "",
        "konto_name": (rule.get("konto_name") if rule else "") or "",
        "foreign": r.get("foreign"),
    }

    def done(tag, why):
        out["tag"] = tag
        out["why"] = why
        return out

    if r.get("kind") == "payment" or "BETALNING MOTTAGEN" in merchant.upper():
        return done("skip", "payment row")
    if amount < 0:
        return done("skip", "refund")
    if ref in overrides:
        o = overrides[ref]
        note = f" ({o['note']})" if o["note"] else ""
        return done(o["tag"], f"Oscar override{note}")
    prev_tag = (prev.get("tag") or "").strip()
    if prev_tag in VALID_TAGS:
        return done(prev_tag, prev.get("why") or "carried from prior classified ledger")
    if account == "-13003":
        if "KINTO" in merchant.upper() and KINTO_EXC_START <= d <= KINTO_EXC_END:
            return done("business", "KINTO exception 2026-04-20..2026-06-04 (Oscar 2026-06-04)")
        return done("personal", "-13003 personal-only (Oscar 2026-06-04)")
    if account == "-61006" and d >= R61006_FROM:
        if rule and rule.get("tag") == "business":
            return done("needs-tag", "business-looking on -61006: review/process-failure (Oscar 2026-06-04)")
        return done("personal", "-61006 personal from 2026-06-04 (Oscar 2026-06-04)")
    if rule and rule.get("tag"):
        return done(rule["tag"], f"merchant rule {rule.get('name')}")
    if d < SPLIT_DATE:
        return done("needs-tag", "pre-split charge, unknown merchant (merchant decides pre-split)")
    if account == "-61022":
        return done("business", "business card 1022 default (post-split)")
    if account == "-62004":
        return done("personal", "personal card 2004 default (post-split)")
    return done("needs-tag", f"unknown account {account}")


def sv_amount(x):
    s = f"{abs(x):,.2f}".replace(",", " ").replace(".", ",")
    sign = "-" if x < 0 else ""
    return f"{sign}{s} SEK"


def main():
    ap = argparse.ArgumentParser(description="Classify a normalised Amex ledger (see module docstring).")
    ap.add_argument("ledger", help="raw amex_import.py JSON output")
    default_rules = os.path.join(os.path.dirname(os.path.abspath(__file__)), "merchant_rules.json")
    ap.add_argument("--rules", default=default_rules)
    ap.add_argument("--overrides", default=None, help="CSV ref,tag,note with Oscar's per-row answers")
    ap.add_argument("--prior", default=None, help="earlier classified JSON whose tags seed this run")
    ap.add_argument("--out-json", default=None)
    ap.add_argument("--out-csv", default=None)
    ap.add_argument("--needs-tag-csv", default=None, help="Oscar-facing list of rows awaiting his tag")
    args = ap.parse_args()

    rules = load_rules(args.rules)
    overrides = load_overrides(args.overrides)
    prior = load_prior(args.prior)

    with open(args.ledger, encoding="utf-8") as f:
        data = json.load(f)
    rows = data.get("rows") if isinstance(data, dict) else data
    if not rows:
        die(f"{args.ledger}: no rows")
    if "source_file" not in rows[0] or "kind" not in rows[0]:
        warn("input does not look like raw amex_import.py output (missing kind/source_file); "
             "make sure you are not re-classifying an already classified or enriched file")

    out_rows, missing_ref = [], 0
    for r in rows:
        if not str(r.get("ref") or "").strip():
            missing_ref += 1
            continue
        out_rows.append(classify_row(r, rules, overrides, prior))
    if missing_ref:
        warn(f"{missing_ref} rows without ref were dropped")

    counts = {}
    for r in out_rows:
        counts[r["tag"]] = counts.get(r["tag"], 0) + 1
    business_sum = sum(r["amount"] for r in out_rows if r["tag"] == "business")
    needs = [r for r in out_rows if r["tag"] == "needs-tag"]

    if args.out_json:
        with open(args.out_json, "w", encoding="utf-8") as f:
            json.dump(out_rows, f, ensure_ascii=False, indent=1)
    if args.out_csv:
        with open(args.out_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            w.writeheader()
            for r in out_rows:
                row = dict(r)
                row["amount"] = f"{row['amount']:.2f}"
                w.writerow(row)
    if args.needs_tag_csv:
        with open(args.needs_tag_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ref", "date", "merchant", "amount", "card", "why"])
            for r in needs:
                w.writerow([r["ref"], r["date"], r["merchant"], sv_amount(r["amount"]),
                            r["card"], r["why"]])

    parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
    print(f"{len(out_rows)} rows: {parts}; business total {sv_amount(business_sum)}")
    for r in needs:
        print(f"  needs-tag: {r['date']} {r['merchant']} {sv_amount(r['amount'])} (card {r['card']})")


if __name__ == "__main__":
    main()
