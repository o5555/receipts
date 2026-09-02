#!/usr/bin/env python3
"""Build the month-end Fortnox dry-run plan for Viseo AB's Amex expense lane.

Oscar pays the Amex bill privately, so every business charge on the card is an
employee expense (utlagg). This script reads the classified ledger produced by
scripts/classify.py, selects the month's business charges for Viseo AB and writes
a complete plan directory: one voucher payload per charge (cost plus VAT, credit
to the liability account), one salary transaction that reimburses Oscar, a
manifest, a Swedish summary for Oscar (plan.md) and a documented fortnox CLI
command sequence (commands.sh, deliberately not executable). It calls NO API;
the fortnox CLI executes the plan only after Oscar approves the exact batch
(dry-run first, see CLAUDE.md).

Usage:
  scripts/fortnox_lane.py out/amex.classified.csv --month 2026-08 --out-dir out/fortnox-run-2026-08 \
      [--receipts-dir out/receipts] [--receipts-map out/receipts-map.csv] [--series A] \
      [--liability-account 2890] [--employee-id ID] [--salary-code UTL] \
      [--vat-account-domestic 2641] [--vat-out-rc 2614] [--vat-in-rc 2645]

Selection: tag == business, entity == viseo, date in --month, amount > 0.
Rows excluded for entity 5555media, tag needs-tag, unknown entity, missing
bas_account or unknown vat_regime are listed in plan.md warnings and in the
manifest, never silently dropped. Personal, skip, refund and payment rows are
out of scope by design and are dropped without a warning.

VAT per row on the gross amount G (Decimal, ROUND_HALF_UP to the ore):
  domestic25/12/6   VAT = G*r/(100+r); debit bas G-VAT, debit 2641 VAT, credit liability G
  domestic25_half   KINTO passenger-car rule: half of G*25/125 deductible on 2641
  eu_rc, noneu_rc   debit bas G, credit liability G, credit 2614 and debit 2645 G*0.25
  oss_pending/none/empty  gross to bas, credit liability G, flagged for Nicolina
The bas_account row absorbs any rounding residual so every voucher balances exactly.
"""
import argparse
import calendar
import csv
import json
import os
import re
import sys
from collections import Counter
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP

CENT = Decimal("0.01")
LEDGER_COLS = ["date", "card", "account", "amount", "merchant", "tag", "why", "in_pleo",
               "confirm", "dash_id", "pack_status", "ext", "ref", "period", "entity",
               "bas_account", "vat_regime", "konto_name"]
DOMESTIC = {"domestic25": 25, "domestic12": 12, "domestic6": 6}
GROSS_ONLY = {"oss_pending", "none", ""}
REGIMES = set(DOMESTIC) | {"domestic25_half", "eu_rc", "noneu_rc"} | GROSS_ONLY


def r2(x: Decimal) -> Decimal:
    return x.quantize(CENT, rounding=ROUND_HALF_UP)


def sek(x: Decimal) -> str:
    """1234.56 -> '1 234,56' (Swedish format, no currency suffix)."""
    return f"{x:,.2f}".replace(",", " ").replace(".", ",")


def norm(s) -> str:
    return re.sub(r"\s+", " ", str(s if s is not None else "")).strip()


def to_json(o, ind=0) -> str:
    """json.dumps look-alike that renders Decimals as plain 2-decimal numbers."""
    pad, pad1 = " " * ind, " " * (ind + 1)
    if isinstance(o, Decimal):
        return f"{o:.2f}"
    if isinstance(o, dict):
        if not o:
            return "{}"
        body = ",\n".join(f"{pad1}{json.dumps(k, ensure_ascii=False)}: {to_json(v, ind + 1)}"
                          for k, v in o.items())
        return "{\n" + body + "\n" + pad + "}"
    if isinstance(o, list):
        if not o:
            return "[]"
        body = ",\n".join(pad1 + to_json(v, ind + 1) for v in o)
        return "[\n" + body + "\n" + pad + "]"
    return json.dumps(o, ensure_ascii=False)


def load_ledger(path):
    """Read the classified ledger (.json list of objects, or CSV) and validate columns."""
    if path.endswith(".json"):
        data = json.load(open(path, encoding="utf-8"))
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise ValueError(f"{path}: JSON ledger must be a list of objects")
        raw, have = data, set(data[0]) if data else set(LEDGER_COLS)
    else:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            rd = csv.DictReader(fh)
            raw, have = list(rd), set(rd.fieldnames or [])
    missing = [c for c in LEDGER_COLS if c not in have]
    if missing:
        raise ValueError(f"{path}: not a classify.py ledger, missing columns: {', '.join(missing)}")
    rows = []
    for i, r in enumerate(raw, 1):
        row = {k: norm(r.get(k, "")) for k in LEDGER_COLS}
        try:
            row["amount"] = Decimal(row["amount"])
        except InvalidOperation:
            raise ValueError(f"{path} row {i} (ref {row['ref'] or '?'}): bad amount {r.get('amount')!r}")
        rows.append(row)
    return rows


def load_receipts_map(path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rd = csv.DictReader(fh)
        if not rd.fieldnames or not {"ref", "file"} <= set(rd.fieldnames):
            raise ValueError(f"{path}: receipts map needs columns ref,file")
        return {r["ref"].strip(): (r.get("file") or "").strip() for r in rd if (r.get("ref") or "").strip()}


def find_receipt(ref, mapped, rdir, rdir_files):
    """Map first; then a file in --receipts-dir starting with ref; then one containing ref."""
    if mapped.get(ref):
        return mapped[ref]
    for f in rdir_files:
        if f.startswith(ref):
            return os.path.join(rdir, f)
    for f in rdir_files:
        if ref in f:
            return os.path.join(rdir, f)
    return ""


def voucher_rows(g, bas, regime, acc):
    """Return [[account, debit, credit], ...], balanced to the ore. Zero sides are int 0."""
    if regime in DOMESTIC:
        rate = DOMESTIC[regime]
        vat = r2(g * rate / (100 + rate))
        rows = [[bas, g - vat, 0], [acc["vat_dom"], vat, 0], [acc["liab"], 0, g]]
    elif regime == "domestic25_half":
        deductible = r2(r2(g * 25 / 125) / 2)
        rows = [[bas, g - deductible, 0], [acc["vat_dom"], deductible, 0], [acc["liab"], 0, g]]
    elif regime in ("eu_rc", "noneu_rc"):
        rc = r2(g * Decimal("0.25"))
        rows = [[bas, g, 0], [acc["liab"], 0, g], [acc["vat_out_rc"], 0, rc], [acc["vat_in_rc"], rc, 0]]
    else:  # oss_pending, none, empty: gross, no VAT lift, flagged for Nicolina by the caller
        rows = [[bas, g, 0], [acc["liab"], 0, g]]
    residual = sum(r[2] for r in rows) - sum(r[1] for r in rows)
    if residual:
        rows[0][1] += residual  # force exact balance on the bas_account row
    return [r for r in rows if r[1] or r[2]]


def description(merchant, ref):
    """'Utlagg Amex <merchant> <ref>', merchant truncated so the whole stays under 100 chars."""
    budget = 99 - len("Utlagg Amex ") - 1 - len(ref)
    m = norm(merchant)[:max(budget, 0)].rstrip()
    d = f"Utlagg Amex {m} {ref}" if m else f"Utlagg Amex {ref}"
    return d[:99]


def md_cell(s, width=34):
    s = norm(s).replace("|", "/")
    return s[:width - 2] + ".." if len(s) > width else s


def main():
    ap = argparse.ArgumentParser(description="Build the Fortnox dry-run plan for one month of Amex utlagg (no API calls).")
    ap.add_argument("ledger", help="classified ledger from scripts/classify.py (.csv or .json)")
    ap.add_argument("--month", required=True, help="YYYY-MM")
    ap.add_argument("--out-dir", required=True, help="plan directory, e.g. out/fortnox-run-2026-08")
    ap.add_argument("--receipts-dir", help="directory scanned for files named by the row's ref")
    ap.add_argument("--receipts-map", help="CSV with columns ref,file (paths used as written)")
    ap.add_argument("--series", default="A", help="voucher series (default A)")
    ap.add_argument("--liability-account", type=int, default=2890)
    ap.add_argument("--employee-id", help="Fortnox employee id for the salary transaction")
    ap.add_argument("--salary-code", default="UTL")
    ap.add_argument("--vat-account-domestic", type=int, default=2641)
    ap.add_argument("--vat-out-rc", type=int, default=2614, help="output VAT, reverse charge")
    ap.add_argument("--vat-in-rc", type=int, default=2645, help="input VAT, reverse charge")
    a = ap.parse_args()

    m = re.fullmatch(r"(\d{4})-(0[1-9]|1[0-2])", a.month)
    if not m:
        print(f"error: --month must be YYYY-MM, got {a.month!r}", file=sys.stderr)
        sys.exit(2)
    last_day = f"{a.month}-{calendar.monthrange(int(m.group(1)), int(m.group(2)))[1]:02d}"

    try:
        ledger = load_ledger(a.ledger)
        mapped = load_receipts_map(a.receipts_map) if a.receipts_map else {}
        rdir_files = sorted(os.listdir(a.receipts_dir)) if a.receipts_dir else []
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(2)

    in_month = [r for r in ledger if r["date"].startswith(a.month + "-") and r["amount"] > 0]
    planned, excluded = [], []  # excluded: (row, short ascii reason for the manifest)
    for r in in_month:
        if r["tag"] == "needs-tag":
            excluded.append((r, "tag needs-tag"))
        elif r["tag"] != "business":
            continue  # personal and skip rows are out of scope by design
        elif r["entity"] == "viseo":
            if not r["bas_account"] or not r["bas_account"].isdigit():
                excluded.append((r, "bas_account saknas"))
            elif r["vat_regime"] not in REGIMES:
                excluded.append((r, f"vat_regime okand '{r['vat_regime']}'"))
            else:
                planned.append(r)
        elif r["entity"] == "5555media":
            excluded.append((r, "entity 5555media"))
        else:
            excluded.append((r, f"entity {r['entity'] or 'saknas'}"))

    if not planned:
        reasons = "; ".join(f"{n} x {why}" for why, n in Counter(w for _, w in excluded).items())
        print(f"error: no vouchers to plan for {a.month}: {len(in_month)} ledger rows in the month, "
              f"0 selected (tag=business, entity=viseo, amount>0)"
              + (f"; excluded: {reasons}" if reasons else ""), file=sys.stderr)
        sys.exit(1)

    vdir = os.path.join(a.out_dir, "vouchers")
    sdir = os.path.join(a.out_dir, "salary")
    os.makedirs(vdir, exist_ok=True)
    os.makedirs(sdir, exist_ok=True)
    for stale in os.listdir(vdir):  # a rerun with fewer rows must not leave stale payloads
        if stale.endswith(".json"):
            os.unlink(os.path.join(vdir, stale))

    acc = {"liab": a.liability_account, "vat_dom": a.vat_account_domestic,
           "vat_out_rc": a.vat_out_rc, "vat_in_rc": a.vat_in_rc}
    planned.sort(key=lambda r: (r["date"], r["ref"]))
    warnings, manifest, uploads = [], [], []
    total, receipts_found = Decimal("0"), 0

    for i, r in enumerate(planned, 1):
        g = r2(r["amount"])
        rows = voucher_rows(g, int(r["bas_account"]), r["vat_regime"], acc)
        payload = {"Voucher": {
            "VoucherSeries": a.series,
            "TransactionDate": r["date"],
            "Description": description(r["merchant"], r["ref"]),
            "VoucherRows": [{"Account": acct, "Debit": d, "Credit": c} for acct, d, c in rows],
        }}
        safe_ref = re.sub(r"[^A-Za-z0-9_.-]", "_", r["ref"]) or f"rad{i}"
        vpath = os.path.join(vdir, f"{i:03d}_{safe_ref}.json")
        with open(vpath, "w", encoding="utf-8") as fh:
            fh.write(to_json(payload) + "\n")

        receipt = find_receipt(r["ref"], mapped, a.receipts_dir or "", rdir_files)
        if receipt and not os.path.isfile(receipt):
            warnings.append(f"Kvittofilen {receipt} for {r['ref']} ({md_cell(r['merchant'])}) finns inte pa disk, raden behandlas som utan kvitto")
            receipt = ""
        status = []
        if receipt:
            receipts_found += 1
            uploads.append((receipt, i, vpath))
        else:
            status.append("kvitto saknas")
            warnings.append(f"Kvitto saknas: {r['ref']} {r['date']} {md_cell(r['merchant'])} ({sek(g)} SEK), verifikatet planeras anda")
        if r["vat_regime"] in GROSS_ONLY:
            status.append("oss policy pending")
            warnings.append(f"OSS-policy vantar: {r['ref']} {r['date']} {md_cell(r['merchant'])} ({sek(g)} SEK) "
                            f"bokfors brutto utan momslyft (vat_regime={r['vat_regime'] or 'tom'}), flagga for Nicolina")
        manifest.append({"ref": r["ref"], "date": r["date"], "merchant": r["merchant"], "amount": sek(g),
                         "bas_account": r["bas_account"], "vat_regime": r["vat_regime"],
                         "receipt_file": receipt, "voucher_payload": vpath,
                         "status": "; ".join(status) or "ok", "_nr": f"{i:03d}", "_konto_name": r["konto_name"]})
        total += g

    for r, why in excluded:
        warnings.append(f"Exkluderad ({why}): {r['ref']} {r['date']} {md_cell(r['merchant'])} ({sek(r2(r['amount']))} SEK)")
        manifest.append({"ref": r["ref"], "date": r["date"], "merchant": r["merchant"], "amount": sek(r2(r["amount"])),
                         "bas_account": r["bas_account"], "vat_regime": r["vat_regime"],
                         "receipt_file": "", "voucher_payload": "", "status": f"excluded {why}",
                         "_nr": "-", "_konto_name": r["konto_name"]})

    total = r2(total)
    employee = a.employee_id or "FILL_IN"
    if employee == "FILL_IN":
        warnings.append("EmployeeId ar FILL_IN i salary/utlagg.json: satt Oscars anstallnings-id med --employee-id fore korning")
    spath = os.path.join(sdir, "utlagg.json")
    with open(spath, "w", encoding="utf-8") as fh:
        fh.write(to_json({"SalaryTransaction": {
            "EmployeeId": employee, "SalaryCode": a.salary_code, "Date": last_day,
            "Number": 1, "Amount": total, "TextRow": f"Utlagg Amex {a.month}"}}) + "\n")

    with open(os.path.join(a.out_dir, "manifest.csv"), "w", newline="", encoding="utf-8") as fh:
        cols = ["ref", "date", "merchant", "amount", "bas_account", "vat_regime",
                "receipt_file", "voucher_payload", "status"]
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(manifest)

    plan = [f"# Fortnox-korning: Amex-utlagg {a.month}", "",
            "Genererad av scripts/fortnox_lane.py. Detta ar en plan, inga API-anrop har gjorts.",
            "fortnox-CLI:t kor den forst efter Oscars godkannande, dry-run forst (se commands.sh).", "",
            f"- Antal verifikat: {len(planned)}",
            f"- Summa utlagg (kredit {a.liability_account}): {sek(total)} SEK",
            f"- Ersattning till Oscar: en lonetransaktion ({a.salary_code}) {last_day} pa {sek(total)} SEK",
            f"- Kvitton funna: {receipts_found} av {len(planned)}",
            "", "## Varningar", ""]
    plan += [f"- {w}" for w in warnings] or ["- Inga varningar."]
    plan += ["", "## Rader", "",
             "| Nr | Datum | Handlare | Belopp | Konto | Moms | Kvitto | Status |",
             "|---|---|---|---|---|---|---|---|"]
    for e in manifest:
        konto = f"{e['bas_account']} {md_cell(e['_konto_name'], 24)}".strip()
        plan.append(f"| {e['_nr']} | {e['date']} | {md_cell(e['merchant'])} | {e['amount']} SEK "
                    f"| {konto} | {e['vat_regime']} | {'ja' if e['receipt_file'] else 'nej'} | {e['status']} |")
    with open(os.path.join(a.out_dir, "plan.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(plan) + "\n")

    sh = ["#!/bin/sh",
          f"# Fortnox-korning Amex utlagg {a.month}. Genererad av scripts/fortnox_lane.py.",
          "# Regel enligt CLAUDE.md: dry-run forst, Oscar godkanner exakt denna batch, forst",
          "# darefter kors APPROVED-raderna, en i taget i ordning (fortnox-CLI:ts token-refresh",
          "# far inte kora parallellt). Filen ar avsiktligt inte korbar (ingen chmod +x).",
          "set -e", "",
          "# --- Steg 1: kvitton till Fortnox Inbox (dry-run) ---"]
    for receipt, nr, vpath in uploads:
        sh += [f"fortnox write inbox-upload {receipt} --folder-id inbox_v",
               f"# APPROVED: fortnox write inbox-upload {receipt} --folder-id inbox_v "
               f"--execute --confirm \"UPLOAD INBOX FILE\"",
               f"# NOTE: File Id fran svaret kopplas till verifikat {nr:03d} ({vpath}) via "
               f"fortnox write voucher-file-connection nar verifikatet finns; "
               f"scripts/fortnox_execute.py gor hela kedjan automatiskt."]
    if not uploads:
        sh.append("# (inga kvittofiler funna i denna korning)")
    sh += ["", "# --- Steg 2: verifikat (dry-run, sedan APPROVED-raden efter Oscars ja) ---"]
    for e in manifest:
        if e["voucher_payload"]:
            sh += [f"fortnox write voucher --file {e['voucher_payload']}",
                   f"# APPROVED: fortnox write voucher --file {e['voucher_payload']} --execute --confirm \"CREATE VOUCHER\""]
    sh += ["", "# --- Steg 3: lonetransaktionen (utlagg, en per manad) ---",
           f"fortnox write salary-transaction --file {spath}",
           f"# APPROVED: fortnox write salary-transaction --file {spath} --execute --confirm \"CREATE SALARY TRANSACTION\""]
    with open(os.path.join(a.out_dir, "commands.sh"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(sh) + "\n")  # deliberately no chmod +x

    flagged = sum(e["status"] != "ok" for e in manifest)
    print(f"{a.month}: {len(planned)} vouchers planned, {flagged} rows flagged, "
          f"{sek(total)} SEK to reimburse -> {a.out_dir} (dry-run plan, no API calls)")


if __name__ == "__main__":
    main()
