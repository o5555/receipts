#!/usr/bin/env python3
"""Execute a fortnox_lane.py plan against Fortnox via the fortnox CLI.

Approval model (per CLAUDE.md: no Fortnox write without explicit approval of the
specific batch): the default mode runs CLI dry-runs only and prints the batch
summary together with an APPROVAL CODE, a sha256 over every payload byte in the
plan. Nothing executes without `--execute --approve <code>`; Oscar hands the code
back after reviewing the batch, and any change to any payload invalidates it.
The script then supplies the fortnox CLI confirm phrases on his behalf, for
exactly the approved content.

Chain per manifest row, strictly sequential (the fortnox CLI's token refresh must
never run in parallel): upload receipt to the inbox (folderid inbox_v) unless
uploads.json already has it -> create the voucher -> connect the uploaded file to
the voucher. Then one salary transaction (utlagg) for the month. Progress is
checkpointed to execution.json after every step; reruns resume idempotently and a
completed salary transaction is never re-run without --force-salary.

Usage:
  scripts/fortnox_execute.py out/fortnox-run-2026-07                # dry-run + approval code
  scripts/fortnox_execute.py out/fortnox-run-2026-07 --execute --approve <code>
"""

import argparse
import csv
import datetime
import hashlib
import json
import os
import subprocess
import sys

CONFIRM = {
    "upload": "UPLOAD INBOX FILE",
    "voucher": "CREATE VOUCHER",
    "connect": "CONNECT VOUCHER FILE",
    "salary": "CREATE SALARY TRANSACTION",
}


def die(msg, code=1):
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def sv(x):
    s = f"{abs(x):,.2f}".replace(",", " ").replace(".", ",")
    return ("-" if x < 0 else "") + s + " SEK"


def parse_sv(s):
    """Parse a manifest amount: Swedish '78 429,63' or plain '78429.63'."""
    return float(str(s).replace(" ", "").replace(" ", "").replace(",", "."))


def load_plan(plan_dir):
    manifest_path = os.path.join(plan_dir, "manifest.csv")
    if not os.path.exists(manifest_path):
        die(f"{manifest_path} not found; run scripts/fortnox_lane.py first")
    with open(manifest_path, encoding="utf-8", newline="") as f:
        rows = [r for r in csv.DictReader(f) if r.get("voucher_payload")]
    salary_path = os.path.join(plan_dir, "salary", "utlagg.json")
    if not os.path.exists(salary_path):
        die(f"{salary_path} not found")
    return rows, salary_path


def load_uploads(plan_dir):
    path = os.path.join(plan_dir, "uploads.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return path, data if isinstance(data, dict) else {"uploads": []}
    return path, {"uploads": []}


def save_json_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def batch_code(rows, salary_path):
    h = hashlib.sha256()
    for r in sorted(rows, key=lambda r: r["voucher_payload"]):
        with open(r["voucher_payload"], "rb") as f:
            h.update(f.read())
        h.update(f"{r['ref']}:{r.get('receipt_file', '')}\n".encode())
    with open(salary_path, "rb") as f:
        h.update(f.read())
    return h.hexdigest()[:16]


def run_cli(args_list, fortnox_bin):
    proc = subprocess.run([fortnox_bin] + args_list, capture_output=True, text=True)
    return proc


def parse_json_out(stdout):
    i = stdout.find("{")
    if i < 0:
        return None
    try:
        return json.loads(stdout[i:])
    except json.JSONDecodeError:
        return None


def cli_step(state, plan_dir, fortnox_bin, args_list, what):
    proc = run_cli(args_list, fortnox_bin)
    if proc.returncode != 0:
        save_state(plan_dir, state)
        tail = (proc.stderr or proc.stdout).strip().splitlines()[-4:]
        die(f"{what} failed; progress saved to execution.json, rerun to resume.\n  "
            + "\n  ".join(tail))
    return parse_json_out(proc.stdout)


def state_path(plan_dir):
    return os.path.join(plan_dir, "execution.json")


def load_state(plan_dir, code):
    path = state_path(plan_dir)
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        if state.get("batch_code") != code:
            die("execution.json belongs to a different batch (payloads changed since "
                "execution started); resolve manually before executing")
        return state
    return {"batch_code": code, "rows": {}, "salary_done": False, "log": []}


def save_state(plan_dir, state):
    save_json_atomic(state_path(plan_dir), state)


def log(state, msg):
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    state["log"].append(f"{stamp} {msg}")
    print(msg)


def main():
    ap = argparse.ArgumentParser(description="Execute a fortnox_lane.py plan (see module docstring).")
    ap.add_argument("plan_dir")
    ap.add_argument("--execute", action="store_true")
    ap.add_argument("--approve", default=None, help="approval code from the dry-run, binds this exact batch")
    ap.add_argument("--fortnox-bin", default="fortnox")
    ap.add_argument("--skip-salary", action="store_true")
    ap.add_argument("--force-salary", action="store_true")
    ap.add_argument("--only-ref", default=None)
    a = ap.parse_args()

    rows, salary_path = load_plan(a.plan_dir)
    if a.only_ref:
        rows = [r for r in rows if r["ref"] == a.only_ref]
    if not rows:
        die("no executable rows in the plan")
    code = batch_code(rows, salary_path)
    uploads_path, uploads = load_uploads(a.plan_dir)
    uploaded = {u["ref"]: u for u in uploads["uploads"]}
    with open(salary_path, encoding="utf-8") as f:
        salary = json.load(f)
    salary_amount = float(salary.get("SalaryTransaction", {}).get("Amount") or 0)

    if not a.execute:
        failures = 0
        print(f"Dry-run av {len(rows)} verifikat, plan {a.plan_dir}:")
        for r in rows:
            checks = []
            if r.get("receipt_file") and r["ref"] not in uploaded:
                p = run_cli(["write", "inbox-upload", r["receipt_file"], "--folder-id", "inbox_v"], a.fortnox_bin)
                checks.append(("kvitto", p))
            p = run_cli(["write", "voucher", "--file", r["voucher_payload"]], a.fortnox_bin)
            checks.append(("verifikat", p))
            marks = []
            for name, proc in checks:
                out = parse_json_out(proc.stdout)
                ok = proc.returncode == 0 and out and out.get("dryRun")
                failures += 0 if ok else 1
                marks.append(f"{name} {'ok' if ok else 'FEL'}")
            receipt = "kvitto uppladdat" if r["ref"] in uploaded else ("kvitto klart" if r.get("receipt_file") else "kvitto saknas")
            print(f"  {r['date']} {r['merchant'][:34]:36} {sv(parse_sv(r['amount'])):>14}  {', '.join(marks)} ({receipt})")
        p = run_cli(["write", "salary-transaction", "--file", salary_path], a.fortnox_bin)
        out = parse_json_out(p.stdout)
        ok = p.returncode == 0 and out and out.get("dryRun")
        failures += 0 if ok else 1
        emp = salary.get("SalaryTransaction", {}).get("EmployeeId")
        print(f"  Lonetransaktion (utlagg) {sv(salary_amount)} till EmployeeId {emp}: {'ok' if ok else 'FEL'}")
        if emp == "FILL_IN" and not a.skip_salary:
            print("  VARNING: EmployeeId ar FILL_IN; regenerera planen med --employee-id")
        if failures:
            die(f"{failures} dry-run steg misslyckades; atgarda innan godkannande")
        print(f"\nGodkannandekod for exakt denna batch: {code}")
        print(f"Kor: scripts/fortnox_execute.py {a.plan_dir} --execute --approve {code}")
        return

    if a.approve != code:
        die("approval code missing or stale: run without --execute to review the batch "
            "and get the current code (payload changes invalidate old codes)")
    state = load_state(a.plan_dir, code)
    if state.get("salary_done") and not a.skip_salary and not a.force_salary:
        die("this batch's salary transaction is already booked (execution.json); "
            "use --skip-salary, or --force-salary only if you are certain")

    os.makedirs(os.path.join(a.plan_dir, "connections"), exist_ok=True)
    for r in rows:
        ref = r["ref"]
        st = state["rows"].setdefault(ref, {})
        if r.get("receipt_file") and ref not in uploaded and "file_id" not in st:
            out = cli_step(state, a.plan_dir, a.fortnox_bin,
                           ["write", "inbox-upload", r["receipt_file"], "--folder-id", "inbox_v",
                            "--execute", "--confirm", CONFIRM["upload"]], f"upload {ref}")
            file_info = (out or {}).get("File") or {}
            if not file_info.get("Id"):
                save_state(a.plan_dir, state)
                die(f"upload {ref}: no File.Id in response")
            st["file_id"] = file_info["Id"]
            uploads["uploads"].append({
                "ref": ref, "file": r["receipt_file"], "inbox_file_id": file_info["Id"],
                "archive_file_id": file_info.get("ArchiveFileId"),
                "uploaded_at": datetime.date.today().isoformat(),
                "voucher_payload": r["voucher_payload"],
            })
            save_json_atomic(uploads_path, uploads)
            uploaded[ref] = uploads["uploads"][-1]
            log(state, f"kvitto uppladdat: {ref} File.Id {file_info['Id']}")
        elif ref in uploaded:
            st.setdefault("file_id", uploaded[ref]["inbox_file_id"])

        if "voucher_number" not in st:
            out = cli_step(state, a.plan_dir, a.fortnox_bin,
                           ["write", "voucher", "--file", r["voucher_payload"],
                            "--execute", "--confirm", CONFIRM["voucher"]], f"voucher {ref}")
            v = (out or {}).get("Voucher") or {}
            if not v.get("VoucherNumber"):
                save_state(a.plan_dir, state)
                die(f"voucher {ref}: no VoucherNumber in response")
            st["voucher_number"] = v["VoucherNumber"]
            st["voucher_series"] = v.get("VoucherSeries")
            st["voucher_year"] = v.get("Year")
            log(state, f"verifikat skapat: {ref} -> {st['voucher_series']}{st['voucher_number']} "
                       f"({r['merchant'][:30]}, {sv(parse_sv(r['amount']))})")
            save_state(a.plan_dir, state)

        if st.get("file_id") and not st.get("connected"):
            conn_path = os.path.join(a.plan_dir, "connections", f"{ref}.json")
            save_json_atomic(conn_path, {"VoucherFileConnection": {
                "FileId": st["file_id"],
                "VoucherSeries": st["voucher_series"],
                "VoucherNumber": st["voucher_number"],
            }})
            cli_step(state, a.plan_dir, a.fortnox_bin,
                     ["write", "voucher-file-connection", "--file", conn_path,
                      "--execute", "--confirm", CONFIRM["connect"]], f"connection {ref}")
            st["connected"] = True
            log(state, f"kvitto kopplat: {ref} -> verifikat {st['voucher_series']}{st['voucher_number']}")
            save_state(a.plan_dir, state)

    if not a.skip_salary and not state.get("salary_done"):
        if salary.get("SalaryTransaction", {}).get("EmployeeId") == "FILL_IN":
            save_state(a.plan_dir, state)
            die("salary payload has EmployeeId FILL_IN; regenerate the plan with --employee-id")
        cli_step(state, a.plan_dir, a.fortnox_bin,
                 ["write", "salary-transaction", "--file", salary_path,
                  "--execute", "--confirm", CONFIRM["salary"]], "salary transaction")
        state["salary_done"] = True
        log(state, f"lonetransaktion bokford: utlagg {sv(salary_amount)}")
    save_state(a.plan_dir, state)
    done = sum(1 for st in state["rows"].values() if st.get("voucher_number"))
    print(f"\nKlart: {done} verifikat, kvitton kopplade dar de finns, "
          f"{'lonetransaktion bokford' if state.get('salary_done') else 'lonetransaktion EJ bokford'}. "
          f"Detaljer i execution.json.")


if __name__ == "__main__":
    main()
