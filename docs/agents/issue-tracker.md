# Issue tracker: Local Markdown

Receipt uses a private local Markdown tracker under `.scratch/`, excluded from the public repository. This is the Wayfinder fallback; no external issue tracker has been configured. Run `/setup-matt-pocock-skills` if a different tracker is wanted.

## Wayfinding operations

- Map: `.scratch/<effort>/map.md`, labelled `wayfinder:map`.
- Children: `.scratch/<effort>/issues/NN-<slug>.md`, with `Type`, `Label`, `Status`, `Assignee`, `Parent`, and `Blocked by` fields.
- Claim: set `Status: claimed` and `Assignee` before work.
- Dependencies: `Blocked by: NN, NN`. All blockers must have `Status: resolved` before a ticket is available.
- Frontier: open, unclaimed children with all blockers resolved, ordered by number.
- Resolution: append `## Answer`, set `Status: resolved`, and link the named decision from the map's Decisions so far. Keep decisions in the ticket, not duplicated in the map.
- Link research assets from tickets. Private finance and access findings stay under ignored paths.
