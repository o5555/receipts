#!/bin/sh
# Offline check runner for the receipts repo. Compiles every script, runs the
# test suites in scripts/tests/, then optional real-data regressions that skip
# with a notice when their inputs are missing. Makes no network calls, never
# runs the fortnox CLI or Gmail, never writes to out/. Exit 0 only when
# everything that ran passed.

set -u
REPO=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$REPO" || exit 2

fail=0

echo "== py_compile scripts/*.py =="
compiled=0
for f in scripts/*.py scripts/tests/*.py; do
    [ -f "$f" ] || continue
    if python3 -m py_compile "$f"; then
        compiled=$((compiled + 1))
    else
        echo "COMPILE FAIL: $f"
        fail=1
    fi
done
echo "$compiled files compiled"

echo ""
echo "== offline test suites =="
suites=0
suites_ok=0
for t in scripts/tests/test_*.py; do
    [ -f "$t" ] || continue
    suites=$((suites + 1))
    if python3 "$t"; then
        suites_ok=$((suites_ok + 1))
    else
        echo "SUITE FAIL: $t"
        fail=1
    fi
done

echo ""
echo "== classify regression (real data, optional) =="
RAW="out/amex-raw-2026-05-06_2026-08-02.json"
PRIOR="out/amex-2026-05-06_2026-08-02.classified.json"
regression="skipped"
if [ -f "$RAW" ] && [ -f "$PRIOR" ]; then
    TMP=$(mktemp -d "${TMPDIR:-/tmp}/receipts-checks.XXXXXX") || exit 2
    if python3 scripts/classify.py "$RAW" --prior "$PRIOR" \
        --out-json "$TMP/reclassified.json" >"$TMP/stdout.txt" 2>"$TMP/stderr.txt"; then
        if python3 - "$PRIOR" "$TMP/reclassified.json" <<'PYEOF'
import json, sys
def rows(path):
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    return data.get("rows") if isinstance(data, dict) else data
prior = {r["ref"]: r["tag"] for r in rows(sys.argv[1])}
new = {r["ref"]: r["tag"] for r in rows(sys.argv[2])}
missing = sorted(set(prior) - set(new))
extra = sorted(set(new) - set(prior))
diff = sorted(r for r in set(prior) & set(new) if prior[r] != new[r])
if missing or extra or diff:
    for r in missing[:5]:
        print(f"  ref {r}: in prior only")
    for r in extra[:5]:
        print(f"  ref {r}: in re-run only")
    for r in diff[:10]:
        print(f"  ref {r}: prior {prior[r]} != re-run {new[r]}")
    print(f"classify regression FAILED: {len(missing)} missing, {len(extra)} extra, {len(diff)} tag diffs")
    sys.exit(1)
print(f"classify regression ok: {len(new)} rows, tags identical to prior")
PYEOF
        then
            regression="ok"
        else
            regression="FAILED"
            fail=1
        fi
    else
        echo "classify re-run FAILED:"
        cat "$TMP/stderr.txt"
        regression="FAILED"
        fail=1
    fi
    rm -rf "$TMP"
else
    echo "SKIP classify regression: needs $RAW and $PRIOR"
fi

echo ""
echo "run_checks: $compiled files compiled, $suites_ok/$suites test suites passed, classify regression $regression"
exit "$fail"
