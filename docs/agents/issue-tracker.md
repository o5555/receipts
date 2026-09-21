# Issue tracker: GitHub

Receipt uses GitHub Issues in `o5555/receipts`. The [Receipt Wayfinder map](https://github.com/o5555/receipts/issues/1) is the canonical map, published at Oscar's request on 2026-09-21. The previous `.scratch/receipt/` files are private historical snapshots, not the active tracker.

## Wayfinding operations

- Read the map and use its native sub-issues, in GitHub order, as its children. Use `gh api repos/o5555/receipts/issues/1/sub_issues --paginate` to list them.
- Map label: `wayfinder:map`. Ticket labels: `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling`, or `wayfinder:task`.
- Read a ticket and its resolution comments with `gh issue view <number> --comments`.
- Claim an open ticket before working: `gh issue edit <number> --add-assignee @me`. Do not claim a ticket already assigned to another worker.
- Use native blocking dependencies. Add an edge with `gh api --method POST repos/o5555/receipts/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-database-id>`. Obtain the database ID with `gh api repos/o5555/receipts/issues/<number> --jq .id`.
- Frontier: open, unassigned children whose `issue_dependencies_summary.blocked_by` is zero. This counts open blockers. Choose the first in map order unless the user names a ticket.
- Create a child issue, then attach it with `gh api --method POST repos/o5555/receipts/issues/1/sub_issues -F sub_issue_id=<child-database-id>`. Create all relevant tickets before wiring their dependencies.
- Resolve by posting the answer as a resolution comment, closing the ticket as completed, and adding a named link and short gist under the map's Decisions so far. Closing research means the investigation is complete, not that implementation is complete.
- Use `--body-file` for multiline issue bodies and comments. Refer to tickets by their linked titles when communicating with the user.

## Public and private material

This repository and its issues are public. Publish product questions, decisions, and sanitized research summaries. Keep receipt originals, transaction-level evidence, mailbox identifiers, credentials, credential locations, and detailed access inventories in ignored local storage. Research assets remain under `out/wayfinder-2026-09-21/`; public summaries must not claim those files are accessible on GitHub.
