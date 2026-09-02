#!/usr/bin/env python3
"""Offline regression checks for scripts/fortnox_lane.py.

Runs the plan builder against the synthetic ledger in
scripts/tests/fixtures/fortnox_classified.csv (every row invented for this
suite, no real spend data) and asserts the exact voucher rows per VAT regime,
the salary payload, manifest, plan.md and commands.sh. All output goes to a
fresh temp directory (receipt stubs and the receipts map are generated there
too) which is removed on success and kept for inspection on failure. The real
out/ directories are never touched; no network and no fortnox CLI call.

Run: python3 scripts/tests/test_fortnox_lane.py   (exit 0 = every check passed)
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from decimal import Decimal

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "fortnox_lane.py")
LEDGER_CSV = os.path.join(HERE, "fixtures", "fortnox_classified.csv")

EXPECT_ROWS = {  # ref -> list of (Account, Debit, Credit) exactly as they must land
    "ATFIX001": [(5410, "103.20", 0), (2641, "25.80", 0), (2890, 0, "129.00")],
    "ATFIX002": [(6072, "222.32", 0), (2641, "26.68", 0), (2890, 0, "249.00")],
    "ATFIX003": [(5810, "55.66", 0), (2641, "3.34", 0), (2890, 0, "59.00")],
    "ATFIX004": [(5615, "432.00", 0), (2641, "48.00", 0), (2890, 0, "480.00")],
    "ATFIX005": [(6540, "49.11", 0), (2890, 0, "49.11"), (2614, 0, "12.28"), (2645, "12.28", 0)],
    "ATFIX006": [(6540, "981.09", 0), (2890, 0, "981.09"), (2614, 0, "245.27"), (2645, "245.27", 0)],
    "ATFIX007": [(6540, "100.00", 0), (2890, 0, "100.00")],
    "ATFIX008": [(6990, "111.25", 0), (2890, 0, "111.25")],
    "ATFIX009": [(6993, "200.00", 0), (2890, 0, "200.00")],
    "ATFIX010": [(5615, "111.10", 0), (2641, "12.35", 0), (2890, 0, "123.45")],
    "ATFIX011": [(6540, "1599.99", 0), (2641, "400.00", 0), (2890, 0, "1999.99")],
    "ATFIX012": [(5410, "50.00", 0), (2641, "12.50", 0), (2890, 0, "62.50")],
}
TOTAL = "4544.39"
TOTAL_SEK = "4 544,39"
checks = []


def ok(name, cond, detail=""):
    checks.append((name, cond, detail))
    if not cond:
        print(f"FAIL {name} {detail}")


def dec(x):
    return Decimal(str(x))


def run(*args):
    return subprocess.run([sys.executable, SCRIPT, *args], capture_output=True, text=True)


def main():
    work = tempfile.mkdtemp(prefix="test_fortnox_lane_")
    rdir = os.path.join(work, "receipts")
    os.makedirs(rdir)
    for f in ("ATFIX001_testbutik.pdf", "receipt_ATFIX006_usmoln.pdf", "bilpool_faktura.pdf"):
        with open(os.path.join(rdir, f), "w") as fh:
            fh.write("pdf stub\n")
    rmap = os.path.join(work, "receipts-map.csv")
    with open(rmap, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ref", "file"])
        w.writerow(["ATFIX004", os.path.join(rdir, "bilpool_faktura.pdf")])
        w.writerow(["ATFIX011", os.path.join(rdir, "does_not_exist.pdf")])
    with open(LEDGER_CSV, encoding="utf-8", newline="") as fh:
        fixture_rows = list(csv.DictReader(fh))
    ledger_json = os.path.join(work, "classified.json")
    with open(ledger_json, "w", encoding="utf-8") as fh:
        json.dump([dict(r, amount=float(r["amount"])) for r in fixture_rows],
                  fh, ensure_ascii=False, indent=1)

    out1 = os.path.join(work, "run-csv")
    p = run(LEDGER_CSV, "--month", "2026-08", "--out-dir", out1,
            "--receipts-dir", rdir, "--receipts-map", rmap)
    ok("exit 0", p.returncode == 0, p.stderr)
    ok("stdout totals", "12 vouchers planned" in p.stdout and "14 rows flagged" in p.stdout
       and f"{TOTAL_SEK} SEK" in p.stdout, p.stdout)

    # --- vouchers: exact rows per regime, exact balance, 2-decimal rendering ---
    vdir = os.path.join(out1, "vouchers")
    vfiles = sorted(os.listdir(vdir))
    ok("12 voucher files", len(vfiles) == 12, vfiles)
    by_ref = {}
    for f in vfiles:
        with open(os.path.join(vdir, f), encoding="utf-8") as fh:
            raw = fh.read()
        v = json.loads(raw, parse_float=Decimal)["Voucher"]
        ref = f.split("_", 1)[1][:-5]
        by_ref[ref] = (v, raw, f)
        deb = sum(dec(r["Debit"]) for r in v["VoucherRows"])
        cre = sum(dec(r["Credit"]) for r in v["VoucherRows"])
        ok(f"{f} balances to the ore", deb == cre, f"{deb} != {cre}")
        ok(f"{f} series/date", v["VoucherSeries"] == "A" and v["TransactionDate"].startswith("2026-08"))
        ok(f"{f} description <100 and has ref", len(v["Description"]) < 100 and v["Description"].endswith(ref)
           and v["Description"].startswith("Utlagg Amex "), v["Description"])
    for ref, want in EXPECT_ROWS.items():
        got = [(r["Account"], r["Debit"] if isinstance(r["Debit"], int) else str(r["Debit"]),
                r["Credit"] if isinstance(r["Credit"], int) else str(r["Credit"]))
               for r in by_ref[ref][0]["VoucherRows"]]
        ok(f"{ref} exact rows", got == list(want), f"got {got} want {want}")
    ok("2-decimal JSON rendering", '"Debit": 103.20' in by_ref["ATFIX001"][1]
       and '"Credit": 129.00' in by_ref["ATFIX001"][1], by_ref["ATFIX001"][1])
    ok("zero sides render as int 0", '"Credit": 0\n' in by_ref["ATFIX001"][1]
       and '"Debit": 0,' in by_ref["ATFIX001"][1], by_ref["ATFIX001"][1])
    ok("numbering by date", vfiles[0].startswith("001_ATFIX001") and vfiles[11].startswith("012_ATFIX012"), vfiles)

    # --- salary ---
    with open(os.path.join(out1, "salary", "utlagg.json"), encoding="utf-8") as fh:
        s = json.loads(fh.read(), parse_float=Decimal)["SalaryTransaction"]
    ok("salary", s["EmployeeId"] == "FILL_IN" and s["SalaryCode"] == "UTL" and s["Date"] == "2026-08-31"
       and s["Number"] == 1 and dec(s["Amount"]) == Decimal(TOTAL) and s["TextRow"] == "Utlagg Amex 2026-08", s)

    # --- manifest ---
    with open(os.path.join(out1, "manifest.csv"), encoding="utf-8", newline="") as fh:
        man = {r["ref"]: r for r in csv.DictReader(fh)}
    ok("manifest row count", len(man) == 17, len(man))
    ok("ok status", man["ATFIX004"]["status"] == "ok" and man["ATFIX004"]["receipt_file"].endswith("bilpool_faktura.pdf"))
    ok("dir startswith match", man["ATFIX001"]["receipt_file"].endswith("ATFIX001_testbutik.pdf"))
    ok("dir contains match", man["ATFIX006"]["receipt_file"].endswith("receipt_ATFIX006_usmoln.pdf"))
    ok("kvitto saknas", man["ATFIX002"]["status"] == "kvitto saknas")
    ok("mapped-but-missing file treated missing", man["ATFIX011"]["status"] == "kvitto saknas" and man["ATFIX011"]["receipt_file"] == "")
    ok("oss flag combined", man["ATFIX007"]["status"] == "kvitto saknas; oss policy pending", man["ATFIX007"]["status"])
    ok("regime none flagged", "oss policy pending" in man["ATFIX008"]["status"])
    ok("empty regime flagged", "oss policy pending" in man["ATFIX009"]["status"])
    ok("5555media excluded", man["ATFIX013"]["status"] == "excluded entity 5555media")
    ok("needs-tag excluded", man["ATFIX014"]["status"] == "excluded tag needs-tag")
    ok("bas missing excluded", man["ATFIX015"]["status"] == "excluded bas_account saknas")
    ok("unknown entity excluded", man["ATFIX016"]["status"] == "excluded entity unknown")
    ok("unknown regime excluded", man["ATFIX021"]["status"] == "excluded vat_regime okand 'domestic20'")
    ok("swedish amount in manifest", man["ATFIX011"]["amount"] == "1 999,99", man["ATFIX011"]["amount"])
    for gone in ("ATFIX017", "ATFIX018", "ATFIX019", "ATFIX020", "ATFIX022", "ATFIX023"):
        ok(f"{gone} absent from august manifest", gone not in man)

    # --- plan.md ---
    with open(os.path.join(out1, "plan.md"), encoding="utf-8") as fh:
        plan = fh.read()
    ok("plan header", "Amex-utlagg 2026-08" in plan and "- Antal verifikat: 12" in plan)
    ok("plan sums", f"{TOTAL_SEK} SEK" in plan and "Kvitton funna: 3 av 12" in plan)
    ok("plan warns 5555media", "ATFIX013" in plan and "5555media" in plan)
    ok("plan warns needs-tag", "ATFIX014" in plan)
    ok("plan warns bas", "ATFIX015" in plan)
    ok("plan warns employee id", "FILL_IN" in plan)
    ok("plan warns oss", "Nicolina" in plan)
    ok("plan warns missing mapped file", "does_not_exist.pdf" in plan)

    # --- commands.sh: Steg 1 is dry-run line + APPROVED line + NOTE line per upload ---
    with open(os.path.join(out1, "commands.sh"), encoding="utf-8") as fh:
        cmds = fh.read()
    ok("commands not executable", not os.access(os.path.join(out1, "commands.sh"), os.X_OK))
    ok("3 inbox upload dry-runs", cmds.count("\nfortnox write inbox-upload ") == 3
       and cmds.count("--folder-id inbox_v") == 6, cmds.count("\nfortnox write inbox-upload "))
    ok("3 approved upload lines", cmds.count("# APPROVED: fortnox write inbox-upload ") == 3
       and cmds.count('--execute --confirm "UPLOAD INBOX FILE"') == 3)
    ok("NOTE per upload mentions fortnox_execute", cmds.count("voucher-file-connection") == 3
       and cmds.count("scripts/fortnox_execute.py") == 3)
    ok("12 voucher dry-runs", cmds.count("\nfortnox write voucher --file ") == 12)
    ok("12 approved voucher lines", cmds.count("# APPROVED: fortnox write voucher") == 12
       and cmds.count('--execute --confirm "CREATE VOUCHER"') == 12)
    ok("salary lines", cmds.count("fortnox write salary-transaction --file ") == 2
       and '--execute --confirm "CREATE SALARY TRANSACTION"' in cmds
       and cmds.count("# APPROVED: fortnox write salary-transaction") == 1)
    ok("claude.md rule in header", "dry-run forst" in cmds and "Oscar godkanner" in cmds and "parallellt" in cmds)

    # --- no em dashes in anything generated ---
    for root, _, files in os.walk(out1):
        for f in files:
            with open(os.path.join(root, f), encoding="utf-8") as fh:
                txt = fh.read()
            ok(f"no em dash in {f}", "—" not in txt and "–" not in txt)

    # --- JSON ledger input parity ---
    out2 = os.path.join(work, "run-json")
    p2 = run(ledger_json, "--month", "2026-08", "--out-dir", out2,
             "--receipts-dir", rdir, "--receipts-map", rmap)
    ok("json input exit 0", p2.returncode == 0, p2.stderr)
    same = True
    for f in vfiles:
        with open(os.path.join(out1, "vouchers", f), encoding="utf-8") as f1, \
                open(os.path.join(out2, "vouchers", f), encoding="utf-8") as f2:
            same = same and f1.read() == f2.read()
    ok("json/csv voucher parity", same and sorted(os.listdir(os.path.join(out2, "vouchers"))) == vfiles)

    # --- rerun cleans stale vouchers ---
    stale = os.path.join(out1, "vouchers", "099_STALE.json")
    with open(stale, "w") as fh:
        fh.write("{}\n")
    run(LEDGER_CSV, "--month", "2026-08", "--out-dir", out1,
        "--receipts-dir", rdir, "--receipts-map", rmap)
    ok("stale voucher removed on rerun", not os.path.exists(stale))

    # --- other months ---
    p_jul = run(LEDGER_CSV, "--month", "2026-07", "--out-dir", os.path.join(work, "run-jul"),
                "--receipts-dir", rdir)
    ok("july plans the single july row", p_jul.returncode == 0 and "1 vouchers planned" in p_jul.stdout, p_jul.stdout)
    p_sep = run(LEDGER_CSV, "--month", "2026-09", "--out-dir", os.path.join(work, "run-sep"))
    ok("only-excluded month exit 1", p_sep.returncode == 1
       and "no vouchers to plan for 2026-09" in p_sep.stderr
       and "entity 5555media" in p_sep.stderr and "tag needs-tag" in p_sep.stderr, p_sep.stderr)
    ok("only-excluded month writes nothing", not os.path.exists(os.path.join(work, "run-sep")))

    # --- failure modes ---
    p3 = run(LEDGER_CSV, "--month", "2026-01", "--out-dir", os.path.join(work, "run-empty"))
    ok("empty month exit 1", p3.returncode == 1 and "no vouchers to plan for 2026-01" in p3.stderr, p3.stderr)
    ok("empty month writes nothing", not os.path.exists(os.path.join(work, "run-empty")))
    p4 = run(os.path.join(work, "nope.csv"), "--month", "2026-08", "--out-dir", os.path.join(work, "x"))
    ok("missing input exit 2", p4.returncode == 2 and "error:" in p4.stderr, p4.stderr)
    bad = os.path.join(work, "bad.csv")
    with open(bad, "w") as fh:
        fh.write("a,b\n1,2\n")
    p5 = run(bad, "--month", "2026-08", "--out-dir", os.path.join(work, "x"))
    ok("bad columns exit 2", p5.returncode == 2 and "missing columns" in p5.stderr, p5.stderr)
    p6 = run(LEDGER_CSV, "--month", "2026-13", "--out-dir", os.path.join(work, "x"))
    ok("bad month exit 2", p6.returncode == 2 and "YYYY-MM" in p6.stderr, p6.stderr)

    # --- option overrides ---
    out3 = os.path.join(work, "run-opts")
    p8 = run(LEDGER_CSV, "--month", "2026-08", "--out-dir", out3, "--series", "B",
             "--liability-account", "2820", "--employee-id", "1", "--salary-code", "UTL2",
             "--vat-account-domestic", "2640", "--vat-out-rc", "2615", "--vat-in-rc", "2647")
    ok("overrides exit 0", p8.returncode == 0, p8.stderr)
    with open(os.path.join(out3, "vouchers", "005_ATFIX005.json"), encoding="utf-8") as fh:
        v = json.loads(fh.read(), parse_float=Decimal)["Voucher"]
    accs = [r["Account"] for r in v["VoucherRows"]]
    ok("override accounts eu_rc", accs == [6540, 2820, 2615, 2647] and v["VoucherSeries"] == "B", accs)
    with open(os.path.join(out3, "salary", "utlagg.json"), encoding="utf-8") as fh:
        s3 = json.load(fh)["SalaryTransaction"]
    ok("override salary", s3["EmployeeId"] == "1" and s3["SalaryCode"] == "UTL2", s3)
    with open(os.path.join(out3, "plan.md"), encoding="utf-8") as fh:
        plan3 = fh.read()
    ok("no FILL_IN warning with employee id", "FILL_IN" not in plan3)

    n_fail = sum(not c for _, c, _ in checks)
    if n_fail:
        print(f"\ntest_fortnox_lane: {len(checks)} checks, {n_fail} failed (work dir kept: {work})")
        sys.exit(1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"test_fortnox_lane: {len(checks)} checks, 0 failed")


if __name__ == "__main__":
    main()
