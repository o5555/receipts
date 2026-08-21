#!/usr/bin/env python3
"""Profile a Pleo expenses export (Export page -> Download) and list rows without receipts.

Expects the unzipped folder with export_*.csv and a receipts/ directory.
Columns used: Date (DD-MM-YYYY), Receipt (Pleo receipt number), Expense Type, Amount (sv-SE, Unicode minus),
Orig. amount, Orig. currency, Source description, Category, Review Status, Reviewer, Review Note,
Receipt urls, Export Status, Expense ID, Reconciled Entries, Personal Expense.

Usage:
  scripts/pleo_export_profile.py data/pleo/expenses_2026-08-21 [--missing-csv out/pleo-missing.csv]

Known limits of this export type: card purchases plus payout rows only; out-of-pocket expenses
are not included (only referenced from payouts via Reconciled Entries); one entity per export.
"""
import argparse
import csv
import glob
import io
import os
import re
from collections import Counter
from datetime import datetime


def amt(s):
    s = (s or "").replace("−", "-").replace(" ", "").replace(".", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--missing-csv")
    a = ap.parse_args()
    csv_path = glob.glob(os.path.join(a.folder, "export_*.csv"))[0]
    rows = list(csv.DictReader(io.StringIO(open(csv_path, "rb").read().decode("utf-8-sig"))))
    files = os.listdir(os.path.join(a.folder, "receipts")) if os.path.isdir(os.path.join(a.folder, "receipts")) else []
    have_file = {re.sub(r"[a-z]?\.\w+$", "", f) for f in files}
    for r in rows:
        r["_d"] = datetime.strptime(r["Date"], "%d-%m-%Y").date().isoformat()
        r["_a"] = amt(r["Amount"])
        r["_has_receipt"] = bool(r["Receipt urls"].strip()) or r["Receipt"] in have_file
    rows.sort(key=lambda r: r["_d"])
    print(f"rows {len(rows)}  {rows[0]['_d']} -> {rows[-1]['_d']}")
    print("expense types:", dict(Counter(r["Expense Type"] for r in rows)))
    print("owners:", dict(Counter(r["Owner"] for r in rows)))
    print("export status:", dict(Counter(r["Export Status"] for r in rows)))
    print("review status:", dict(Counter(r["Review Status"] for r in rows)))
    missing = [r for r in rows if not r["_has_receipt"]]
    print(f"rows without receipt: {len(missing)} (card purchases {sum(r['Expense Type']=='Card Purchase' for r in missing)})")
    for r in missing:
        print(f"  {r['_d']} #{r['Receipt']} {r['_a']:>11,.2f} {r['Currency']} {r['Source description'][:30]:30} {r['Expense Type'][:14]:14} {r['Export Status']}")
    payouts = [r for r in rows if r["Expense Type"] != "Card Purchase"]
    ids = {r["Receipt"] for r in rows}
    for p in payouts:
        refs = [x.strip() for x in p["Reconciled Entries"].split(",") if x.strip()]
        absent = [x for x in refs if x not in ids]
        print(f"payout {p['_d']} {p['_a']:,.2f} {p['Currency']} {p['Export Status']}: {len(refs)} entries, {len(absent)} not in this export")
    if a.missing_csv:
        with open(a.missing_csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "pleo_receipt_no", "expense_id", "merchant", "amount", "currency", "orig_amount", "orig_currency", "type", "category", "review_status", "export_status"])
            for r in missing:
                w.writerow([r["_d"], r["Receipt"], r["Expense ID"], r["Source description"], r["_a"], r["Currency"], r["Orig. amount"], r["Orig. currency"], r["Expense Type"], r["Category"], r["Review Status"], r["Export Status"]])


if __name__ == "__main__":
    main()
