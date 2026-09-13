# knowledge/ — what Incident Judge knows

This folder is the knowledge base Incident Judge reads before it judges an incident and improves after it resolves
one. It follows Karpathy's **LLM Wiki** pattern: a folder of Markdown that the model reads first and keeps up to date,
instead of re-deriving everything from raw data each time.

Because runbooks here drive real actions on production systems, the pattern is split by who may write what:
**the LLM writes understanding (prose), code owns the numbers (stats, autonomy) and humans own the service docs.**
The full schema and rules are in [`AGENTS.md`](AGENTS.md).

## Contents

| Path | What | Written by |
|---|---|---|
| [`wiki/architecture.md`](wiki/architecture.md) | ShopLab topology, customer journeys, what "normal" looks like | Humans |
| [`wiki/services/`](wiki/services/) | Per service: dependencies, failure modes, signals, safe actions | Humans |
| [`wiki/runbooks/`](wiki/runbooks/) | Known incident classes: signatures, machine-checked `match_conditions`, action, stats, autonomy | LLM prose via reviewed PRs; stats and autonomy by code |
| [`wiki/index.md`](wiki/index.md) | Catalog of runbooks | Tooling (regenerated on merge) |
| [`wiki/log.md`](wiki/log.md) | Append-only change log of the wiki | Code |
| [`raw/incidents/`](raw/) | Immutable, redacted incident timelines from the agent's audit log | Code |

## How the agent uses it

1. **Query.** On a new incident: match runbooks by fingerprint, then by symptoms, then let code check the
   runbook's `match_conditions` on live metrics. A look-alike whose conditions fail is rejected.
2. **Diagnose.** With no usable runbook, the relevant service docs and architecture are given to the diagnosis step,
   and the answer must cite them.
3. **Ingest.** After resolution, code writes the raw timeline and recomputes stats and autonomy. The LLM proposes a
   runbook update, which is validated (P12: no edits to code-owned fields, actions must exist in the catalog) and opened as a
   pull request against this folder.
4. **Lint.** `judge memory lint` finds stale runbooks, orphaned actions, duplicate signatures and index drift.

A runbook's autonomy level (L0 suggest, L1 confirm, L2 veto window, L3 automatic) is computed only from verified
outcomes. One failed fix drops it to L1 and flags it for review.
