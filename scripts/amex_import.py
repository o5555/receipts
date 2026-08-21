#!/usr/bin/env python3
"""Normalise American Express portal exports (activity*.csv) into one ledger.

Input format (Amex SE portal, "Ladda ner aktivitet" CSV):
  Datum (MM/DD/YYYY), Beskrivning, Kortmedlem, Konto #, Belopp (sv-SE, Unicode minus on credits),
  Utokade specifikationer (foreign amount, flight legs), Visas pa ditt kontoutdrag som,
  Adress, Ort, Postnummer, Land, Referens (quoted with a leading apostrophe).

Usage:
  scripts/amex_import.py data/amex/*.csv --out out/amex.normalized.json
  scripts/amex_import.py data/amex/*.csv --csv out/amex.normalized.csv

Card accounts seen so far: -61022 (business Amex, plastic ends 1022), -62004 (personal, ends 2004).
-13003 (Amex Platinum) and -61006 appeared in earlier exports; rules for them are on
obrain/projects/pleo-api.md. Classification is deliberately NOT done here; see classify.py.
"""
import argparse
import csv
import io
import json
import re
import sys
from datetime import datetime


def parse_amount(s: str) -> float:
    s = (s or "").replace("−", "-").replace("\xa0", "").replace(" ", "").strip()
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    return float(s)


def norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def load(path: str):
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            txt = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    rows = []
    for r in csv.DictReader(io.StringIO(txt)):
        date = datetime.strptime(r["Datum"], "%m/%d/%Y").date().isoformat()
        merchant = norm(r["Beskrivning"])
        amount = parse_amount(r["Belopp"])
        kind = "payment" if "BETALNING MOTTAGEN" in merchant.upper() else ("refund" if amount < 0 else "charge")
        ext = norm(r.get("Utökade specifikationer") or r.get("Utokade specifikationer") or "")
        foreign = None
        m = re.search(r"Foreign Spend Amount:\s*([\d.,]+)\s*([A-Z ]+?)\s+Commission", ext)
        if m:
            foreign = {"amount": m.group(1), "currency": m.group(2).strip()}
        rows.append({
            "date": date,
            "merchant": merchant,
            "account": r["Konto #"].strip(),
            "card": r["Konto #"].strip()[-4:],
            "member": norm(r["Kortmedlem"]),
            "amount": amount,
            "kind": kind,
            "foreign": foreign,
            "ext": ext,
            "statement_text": norm(r.get("Visas på ditt kontoutdrag som") or ""),
            "city": norm(r.get("Ort") or ""),
            "country": norm(r.get("Land") or ""),
            "ref": (r.get("Referens") or "").strip().strip("'"),
            "source_file": path.rsplit("/", 1)[-1],
        })
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--out", help="JSON output path")
    ap.add_argument("--csv", help="CSV output path")
    a = ap.parse_args()
    rows, seen = [], set()
    for f in a.files:
        for r in load(f):
            if r["ref"] in seen:
                continue  # same transaction exported twice (overlapping statement windows)
            seen.add(r["ref"])
            rows.append(r)
    rows.sort(key=lambda x: (x["date"], x["merchant"]))
    charges = [r for r in rows if r["kind"] == "charge"]
    print(f"{len(rows)} rows ({len(charges)} charges, {sum(r['amount'] for r in charges):,.2f} SEK), "
          f"accounts {sorted({r['account'] for r in rows})}, {rows[0]['date']} -> {rows[-1]['date']}", file=sys.stderr)
    if a.out:
        json.dump(rows, open(a.out, "w"), ensure_ascii=False, indent=1)
    if a.csv:
        cols = ["date", "card", "account", "amount", "kind", "merchant", "foreign", "ext", "city", "country", "ref", "source_file"]
        with open(a.csv, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=cols)
            w.writeheader()
            for r in rows:
                w.writerow({k: (json.dumps(r[k], ensure_ascii=False) if k == "foreign" and r[k] else r.get(k, "")) for k in cols})
    if not a.out and not a.csv:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
