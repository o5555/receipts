# Supabase Setup

Approved schema for the receipts dashboard:

- `public.receipts`: one row per transaction/receipt, with the original row stored as JSONB in `data`.
- `public.receipt_state`: mutable dashboard state keyed by `receipt_id`.

Apply when a Supabase PAT or Postgres connection string is available:

```bash
supabase db push --db-url "$DATABASE_URL"
```

Then import the current local data:

```bash
bin/receipts-db sync
bin/receipts-db summary --source AMEX
```

The Vercel API reads/writes through `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`. The service-role key stays server-side only.
