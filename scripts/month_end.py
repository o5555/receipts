#!/usr/bin/env python3
"""One-command month-end run for the Amex-to-Fortnox lane.

Chains the pipeline: classify (card rules + merchant rules + Oscar's overrides)
-> amex_receipts (Gmail evidence fetch, read-only, audited) -> fortnox_lane
(dry-run plan with vouchers, salary utlagg, manifest, plan.md). It never executes
anything against Fortnox: the last step prints the fortnox_execute.py dry-run
command, whose output ends with the approval code Oscar hands back to authorise
exactly that batch (CLAUDE.md: no Fortnox write without approval of the specific
batch).

Usage:
  scripts/month_end.py --month 2026-07 --reason "month-end run July"
  scripts/month_end.py --month 2026-08 --ledger out/amex-raw-<window>.json --no-fetch
"""

import argparse
import datetime
import glob
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def die(msg):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(1)


def newest_raw_ledger():
    cands = sorted(glob.glob(os.path.join(REPO, "out", "amex-raw-*.json")), key=os.path.getmtime)
    if not cands:
        die("no out/amex-raw-*.json found; run scripts/amex_import.py on data/amex/*.csv first")
    return cands[-1]


def prev_month(today=None):
    d = today or datetime.date.today()
    first = d.replace(day=1)
    last_prev = first - datetime.timedelta(days=1)
    return last_prev.strftime("%Y-%m")


def run(cmd, what):
    print(f"\n== {what} ==")
    print("$ " + " ".join(cmd))
    proc = subprocess.run(cmd)
    if proc.returncode != 0:
        die(f"{what} failed (exit {proc.returncode}); fix and rerun, steps are idempotent")


def main():
    ap = argparse.ArgumentParser(description="Month-end Amex-to-Fortnox run (see module docstring).")
    ap.add_argument("--month", default=None, help="YYYY-MM, default previous calendar month")
    ap.add_argument("--ledger", default=None, help="raw amex_import.py JSON, default newest out/amex-raw-*.json")
    ap.add_argument("--prior", default=os.path.join(REPO, "out", "amex-2026-05-06_2026-08-02.classified.json"))
    ap.add_argument("--overrides", default=os.path.join(REPO, "data", "tags", "overrides.csv"))
    ap.add_argument("--employee-id", default="02", help="Fortnox EmployeeId for the utlagg (Oscar is 02)")
    ap.add_argument("--reason", default=None, help="audit reason for the Gmail fetch")
    ap.add_argument("--no-fetch", action="store_true", help="skip the Gmail receipt fetch step")
    ap.add_argument("--min-confidence", default="strong", choices=["strong", "possible"])
    a = ap.parse_args()

    month = a.month or prev_month()
    if not re.fullmatch(r"\d{4}-\d{2}", month):
        die(f"bad month {month!r}, expected YYYY-MM")
    ledger = a.ledger or newest_raw_ledger()
    stem = re.sub(r"\.json$", "", os.path.basename(ledger)).replace("amex-raw-", "amex-")
    classified_csv = os.path.join(REPO, "out", f"{stem}.classified2.csv")
    classified_json = os.path.join(REPO, "out", f"{stem}.classified2.json")
    needs_tag_csv = os.path.join(REPO, "out", f"amex-needs-tag-{month}.csv")
    receipts_dir = os.path.join(REPO, "out", "receipts-amex")
    map_csv = os.path.join(receipts_dir, f"map-{month}.csv")
    plan_dir = os.path.join(REPO, "out", f"fortnox-run-{month}")

    classify_cmd = [os.path.join(HERE, "classify.py"), ledger,
                    "--out-json", classified_json, "--out-csv", classified_csv,
                    "--needs-tag-csv", needs_tag_csv]
    if os.path.exists(a.prior):
        classify_cmd += ["--prior", a.prior]
    if os.path.exists(a.overrides):
        classify_cmd += ["--overrides", a.overrides]
    run(classify_cmd, "steg 1: klassificering")

    if a.no_fetch:
        print("\n== steg 2: kvittohamtning HOPPAS OVER (--no-fetch) ==")
    else:
        if not a.reason:
            die("--reason is required for the Gmail fetch (or pass --no-fetch)")
        run([os.path.join(HERE, "amex_receipts.py"), classified_csv, "--month", month,
             "--out-dir", receipts_dir, "--map-csv", map_csv,
             "--min-confidence", a.min_confidence, "--reason", a.reason],
            "steg 2: kvitton fran Gmail")

    lane_cmd = [os.path.join(HERE, "fortnox_lane.py"), classified_csv, "--month", month,
                "--out-dir", plan_dir, "--receipts-dir", receipts_dir,
                "--employee-id", a.employee_id]
    if os.path.exists(map_csv):
        lane_cmd += ["--receipts-map", map_csv]
    run(lane_cmd, "steg 3: Fortnox-plan")

    print(f"\nKlart. Granska {os.path.relpath(plan_dir, REPO)}/plan.md och kor sedan:")
    print(f"  scripts/fortnox_execute.py {os.path.relpath(plan_dir, REPO)}")
    print("Dry-runnen skriver godkannandekoden; Oscar godkanner batchen genom att ge koden till")
    print(f"  scripts/fortnox_execute.py {os.path.relpath(plan_dir, REPO)} --execute --approve <kod>")


if __name__ == "__main__":
    main()
