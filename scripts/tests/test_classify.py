#!/usr/bin/env python3
"""Offline regression checks for scripts/classify.py rule order.

A synthetic mini-ledger (every row invented, no real spend data) exercises the
documented rule order: payment and refund skips, --overrides beating --prior,
--prior seeding every tag including needs-tag (never auto-resolved), the
bounded KINTO exception on -13003, the -61006 personal rule with its
business-looking needs-tag escape, merchant rule tags, the pre-split needs-tag
default, the post-split card defaults, and the kontering fields attached from
the real scripts/merchant_rules.json (read-only), including entity 5555media
for Oderland. Real vendor names appear only where a rule pattern must match;
all amounts and dates are synthetic. Everything is written to a fresh temp
directory, removed on success and kept on failure. No network.

Run: python3 scripts/tests/test_classify.py   (exit 0 = every check passed)
"""
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(os.path.dirname(HERE), "classify.py")

CSV_COLUMNS = ["date", "card", "account", "amount", "merchant", "tag", "why", "in_pleo",
               "confirm", "dash_id", "pack_status", "ext", "ref", "period", "entity",
               "bas_account", "vat_regime", "konto_name"]
checks = []


def ok(name, cond, detail=""):
    checks.append((name, cond, detail))
    if not cond:
        print(f"FAIL {name} {detail}")


def raw(ref, date, merchant, account, amount=100.0, kind="charge"):
    return {"date": date, "merchant": merchant, "account": account, "card": account[-4:],
            "member": "TESTPERSON EFTERNAMN", "amount": amount, "kind": kind, "foreign": None,
            "ext": "", "statement_text": merchant, "city": "STOCKHOLM", "country": "SVERIGE",
            "ref": ref, "source_file": "activity_fixture.csv"}


ROWS = [
    # rule 1: payment rows skip, even with an override on the ref
    raw("CTFIX001", "2026-07-01", "BETALNING MOTTAGEN, TACK", "-61022", -5000.0, "payment"),
    # rule 2: negative amount skips
    raw("CTFIX002", "2026-07-02", "TESTBUTIK RETUR", "-61022", -50.0, "refund"),
    # rule 3: override beats prior (prior says personal) and the card default
    raw("CTFIX003", "2026-07-03", "OKAND HANDLARE A", "-62004"),
    # rule 4: prior seeds ALL tags including needs-tag (would default business on -61022)
    raw("CTFIX004", "2026-07-04", "OKAND HANDLARE B", "-61022"),
    raw("CTFIX005", "2026-07-05", "RANDOM KAFE A", "-62004"),
    # rules 5 and 6: KINTO exception window on -13003 (end date inclusive), else personal-only
    raw("CTFIX006", "2026-06-04", "KINTO SHARE TEST", "-13003"),
    raw("CTFIX007", "2026-06-05", "KINTO SHARE TEST", "-13003"),
    raw("CTFIX008", "2026-05-01", "STADHJALP HEMMA AB", "-13003"),
    # rule 7: -61006 on/after 2026-06-04; business-rule merchant escapes to needs-tag
    raw("CTFIX009", "2026-07-06", "ANTHROPIC TESTKOP", "-61006"),
    raw("CTFIX010", "2026-06-04", "RANDOM KAFE B", "-61006"),
    raw("CTFIX011", "2026-06-03", "RANDOM KAFE C", "-61006"),  # before the -61006 rule: falls to pre-split
    # rule 8: merchant rule tags (Anthropic business, Oderland business/5555media, Spotify needs-tag)
    raw("CTFIX012", "2026-07-07", "ANTHROPIC TESTKOP", "-62004"),
    raw("CTFIX013", "2026-07-08", "ODERLAND TESTKOP", "-61022"),
    raw("CTFIX014", "2026-07-09", "SPOTIFY TESTKOP", "-61022"),
    # rule 9: pre-split unknown merchant (day before the 2026-06-15 split)
    raw("CTFIX015", "2026-06-14", "HELT OKAND AB", "-61022"),
    # rule 10: post-split card defaults and unknown account
    raw("CTFIX016", "2026-07-10", "HELT OKAND AB", "-61022"),
    raw("CTFIX017", "2026-07-11", "HELT OKAND AB", "-62004"),
    raw("CTFIX018", "2026-07-12", "HELT OKAND AB", "-99999"),
    # kontering-only rule (tag null): Webhallen rule attaches konto, card default decides tag
    raw("CTFIX019", "2026-07-13", "WEBHALLEN TESTKOP", "-61022"),
    # KINTO on the business card goes through the merchant rule, half VAT kontering
    raw("CTFIX020", "2026-07-14", "KINTO SHARE TEST", "-61022"),
    # split day itself is post-split
    raw("CTFIX021", "2026-06-15", "HELT OKAND AB", "-61022"),
    # dropped: no ref
    raw("", "2026-07-15", "REFLOS RAD", "-61022"),
]

PRIOR = [
    {"ref": "CTFIX003", "tag": "personal", "why": "gammal bedomning"},
    {"ref": "CTFIX004", "tag": "needs-tag", "why": "queued for Oscar", "in_pleo": True, "confirm": "fx123"},
    {"ref": "CTFIX005", "tag": "business", "why": ""},
]

EXPECT = {  # ref -> (tag, why prefix)
    "CTFIX001": ("skip", "payment row"),
    "CTFIX002": ("skip", "refund"),
    "CTFIX003": ("business", "Oscar override (Oscars svar)"),
    "CTFIX004": ("needs-tag", "queued for Oscar"),
    "CTFIX005": ("business", "carried from prior classified ledger"),
    "CTFIX006": ("business", "KINTO exception"),
    "CTFIX007": ("personal", "-13003 personal-only"),
    "CTFIX008": ("personal", "-13003 personal-only"),
    "CTFIX009": ("needs-tag", "business-looking on -61006"),
    "CTFIX010": ("personal", "-61006 personal from 2026-06-04"),
    "CTFIX011": ("needs-tag", "pre-split charge"),
    "CTFIX012": ("business", "merchant rule Anthropic"),
    "CTFIX013": ("business", "merchant rule Oderland"),
    "CTFIX014": ("needs-tag", "merchant rule Spotify"),
    "CTFIX015": ("needs-tag", "pre-split charge"),
    "CTFIX016": ("business", "business card 1022 default (post-split)"),
    "CTFIX017": ("personal", "personal card 2004 default (post-split)"),
    "CTFIX018": ("needs-tag", "unknown account -99999"),
    "CTFIX019": ("business", "business card 1022 default (post-split)"),
    "CTFIX020": ("business", "merchant rule KINTO"),
    "CTFIX021": ("business", "business card 1022 default (post-split)"),
}


def main():
    work = tempfile.mkdtemp(prefix="test_classify_")
    raw_path = os.path.join(work, "raw.json")
    with open(raw_path, "w", encoding="utf-8") as fh:
        json.dump(ROWS, fh, ensure_ascii=False, indent=1)
    prior_path = os.path.join(work, "prior.json")
    with open(prior_path, "w", encoding="utf-8") as fh:
        json.dump(PRIOR, fh, ensure_ascii=False, indent=1)
    overrides_path = os.path.join(work, "overrides.csv")
    with open(overrides_path, "w", encoding="utf-8", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ref", "tag", "note"])
        w.writerow(["CTFIX001", "business", "skall inte galla"])  # payment detection must win
        w.writerow(["CTFIX003", "business", "Oscars svar"])
        w.writerow(["CTFIX999", "notatag", "ogiltig tagg"])       # warned and skipped
        w.writerow(["", "business", "tom ref"])                   # warned and skipped
    out_json = os.path.join(work, "classified.json")
    out_csv = os.path.join(work, "classified.csv")
    needs_csv = os.path.join(work, "needs-tag.csv")

    p = subprocess.run([sys.executable, SCRIPT, raw_path, "--prior", prior_path,
                        "--overrides", overrides_path, "--out-json", out_json,
                        "--out-csv", out_csv, "--needs-tag-csv", needs_csv],
                       capture_output=True, text=True)
    ok("exit 0", p.returncode == 0, p.stderr)
    ok("stdout counts", "21 rows: business 9, needs-tag 6, personal 4, skip 2; "
       "business total 900,00 SEK" in p.stdout, p.stdout)
    ok("stdout lists 6 needs-tag rows", p.stdout.count("\n  needs-tag: ") == 6, p.stdout)
    ok("stderr warns twice about bad override lines",
       p.stderr.count("skipped (need ref and a valid tag)") == 2, p.stderr)
    ok("stderr warns about the ref-less row", "1 rows without ref were dropped" in p.stderr, p.stderr)

    with open(out_json, encoding="utf-8") as fh:
        rows = {r["ref"]: r for r in json.load(fh)}
    ok("21 classified rows", len(rows) == 21, len(rows))
    for ref, (tag, why) in EXPECT.items():
        r = rows.get(ref)
        ok(f"{ref} tag {tag}", r is not None and r["tag"] == tag, r and (r["tag"], r["why"]))
        ok(f"{ref} why", r is not None and r["why"].startswith(why), r and r["why"])

    # kontering comes from the merchant rule regardless of how the tag was decided
    ok("Oderland entity 5555media", rows["CTFIX013"]["entity"] == "5555media"
       and rows["CTFIX013"]["bas_account"] == "6540"
       and rows["CTFIX013"]["vat_regime"] == "domestic25"
       and rows["CTFIX013"]["konto_name"] == "IT-tjanster", rows["CTFIX013"])
    ok("Anthropic kontering on needs-tag row", rows["CTFIX009"]["bas_account"] == "6540"
       and rows["CTFIX009"]["vat_regime"] == "noneu_rc" and rows["CTFIX009"]["entity"] == "viseo",
       rows["CTFIX009"])
    ok("KINTO kontering on personal -13003 row", rows["CTFIX007"]["bas_account"] == "5820"
       and rows["CTFIX007"]["vat_regime"] == "domestic25_half", rows["CTFIX007"])
    ok("KINTO half VAT on business card", rows["CTFIX020"]["bas_account"] == "5820"
       and rows["CTFIX020"]["vat_regime"] == "domestic25_half"
       and rows["CTFIX020"]["konto_name"] == "Hyrbilskostnader", rows["CTFIX020"])
    ok("null-tag rule kontering, card default tag", rows["CTFIX019"]["bas_account"] == "5410"
       and rows["CTFIX019"]["vat_regime"] == "domestic25"
       and rows["CTFIX019"]["konto_name"] == "Forbrukningsinventarier", rows["CTFIX019"])
    ok("unknown merchant entity defaults viseo", rows["CTFIX016"]["entity"] == "viseo"
       and rows["CTFIX016"]["bas_account"] == "", rows["CTFIX016"])
    ok("prior seeds in_pleo and confirm", rows["CTFIX004"]["in_pleo"] == "yes"
       and rows["CTFIX004"]["confirm"] == "fx123", rows["CTFIX004"])

    with open(out_csv, encoding="utf-8", newline="") as fh:
        rd = csv.DictReader(fh)
        ok("csv columns", rd.fieldnames == CSV_COLUMNS, rd.fieldnames)
        crows = list(rd)
    ok("csv row count", len(crows) == 21, len(crows))
    ok("csv amount format", next(r for r in crows if r["ref"] == "CTFIX016")["amount"] == "100.00")

    with open(needs_csv, encoding="utf-8", newline="") as fh:
        nrows = list(csv.DictReader(fh))
    ok("needs-tag csv has 6 rows", len(nrows) == 6, len(nrows))
    ok("needs-tag csv swedish amounts", all(r["amount"] == "100,00 SEK" for r in nrows),
       [r["amount"] for r in nrows])

    n_fail = sum(not c for _, c, _ in checks)
    if n_fail:
        print(f"\ntest_classify: {len(checks)} checks, {n_fail} failed (work dir kept: {work})")
        sys.exit(1)
    shutil.rmtree(work, ignore_errors=True)
    print(f"test_classify: {len(checks)} checks, 0 failed")


if __name__ == "__main__":
    main()
