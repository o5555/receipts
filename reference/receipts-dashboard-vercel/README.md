# Receipts Dashboard

Local source of truth:

- `protected/data/receipts.json` contains seed receipt and card rows.
- `protected/data/state.json` contains mutable state: tag, status, operator note, and timestamps.
- `bin/receipts` is the agent and terminal interface for reading and updating state.

Common commands:

```bash
bin/receipts summary --source AMEX
bin/receipts list --source AMEX --tag business --status needs-receipt
bin/receipts next --source AMEX
bin/receipts show amex-2026-03-07-masinov
bin/receipts tag <receipt-id> business
bin/receipts status <receipt-id> done
bin/receipts note <receipt-id> "Invoice requested"
bin/receipts import-state ~/Downloads/receipts-dashboard-state-2026-06-24.json
```

The deployed dashboard reads the JSON files at startup. Browser edits are still local until exported or made through the CLI. For live browser persistence, move the same `receipts` and `receipt_state` shape to Supabase or another hosted SQL store, then point both the CLI and dashboard API at that store.
