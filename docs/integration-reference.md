# Integration documentation reference

Receipt's local documentation corpus is in `out/documentation-2026-09-14/`.

Read its `README.md` for measured coverage and gaps. `manifest.json` distinguishes fetched content from URL-only discovery and records source URLs, collection dates, file hashes, and retrieval method. The corpus contains Pleo API and MCP documentation, Fortnox developer guidance and API reference, selected OpenAI, Claude and OpenClaw integration documentation, GBrain documentation, and initial Accounted and 1Password references.

Search the derived local full-text index:

```sh
python3 out/documentation-2026-09-14/search.py 'voucherfileconnections'
python3 out/documentation-2026-09-14/search.py 'refresh AND token'
```

Read the original page returned by the search before acting. Prefer current API documentation over deprecated endpoints. Fortnox's API reference and third-party Fortnox MCP implementations are separate sources. Verify the actual configured MCP tool schemas and granted scopes against the documentation.

The corpus is a dated snapshot. Before integration changes, verify the current official page and changelog. The retrieval scripts and raw snapshots remain local under `out/`; they are not backed up by the public code repository. No recurring refresh job is installed. Re-run Firecrawl maps to discover new URLs, then update the manifest and rebuild the index. A URL-only entry is not locally available full text.

The receipt workflow discussion and proposed operating model are in `out/documentation-2026-09-14/receipt-direction.md`. Durable user decisions remain in OBrain's existing `projects/pleo-api.md`.
