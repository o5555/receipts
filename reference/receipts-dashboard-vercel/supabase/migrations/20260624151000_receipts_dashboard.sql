create table if not exists public.receipts (
  id text primary key,
  data jsonb not null,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.receipt_state (
  receipt_id text primary key references public.receipts(id) on delete cascade,
  tag text check (tag in ('business', 'personal', 'needs-tag', 'ignore')),
  status text check (status in ('ready', 'needs-receipt', 'pending-payout', 'done', 'needs-review', 'blocked', 'ignore')),
  operator_note text,
  updated_at timestamptz not null default now(),
  updated_by text not null default 'agent'
);

create index if not exists receipts_data_source_idx on public.receipts ((data->>'source'));
create index if not exists receipts_data_date_idx on public.receipts ((data->>'date'));
create index if not exists receipt_state_tag_idx on public.receipt_state (tag);
create index if not exists receipt_state_status_idx on public.receipt_state (status);

alter table public.receipts enable row level security;
alter table public.receipt_state enable row level security;

comment on table public.receipts is 'Receipt dashboard immutable source rows. App server reads via service-role key.';
comment on table public.receipt_state is 'Receipt dashboard mutable status/tag/operator state. App server writes via service-role key.';
