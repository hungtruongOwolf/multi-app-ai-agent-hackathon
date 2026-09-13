<img src="../assets/brand/logo-mark.svg" alt="Incident Judge logo" width="56" height="56" />

# incident-judge/ — the agent

This package is the on-call agent. For the project overview, connected apps, reliability results and the demo, see
the [main README](../README.md).

| Document | For |
|---|---|
| [`docs/brief.md`](docs/brief.md) | System and reliability brief (judges) |
| [`docs/SETUP.md`](docs/SETUP.md) | Connecting Sentry, Linear, Instatus, Slack, PagerDuty, GitHub and Claude |
| [`docs/demo.md`](docs/demo.md) | Two-minute demo script |
| [`SPEC.md`](SPEC.md) | Full design |
| [`docs/CONTRACTS.md`](docs/CONTRACTS.md) | Module contracts, integration findings (§8), v2 contracts (§9) |
| [`docs/results/`](docs/results/) | Eval reports: `full-k3`, baselines `base-B0`…`base-B3`, `v2-core` |

## Layout

| Path | Contents |
|---|---|
| `judge/agent.py` | Per-incident state machine: triage, announce, approvals, remediation, verification, resolve, learn |
| `judge/core/` | Pydantic models, SQLite (WAL) store, idempotent outbox with markers |
| `judge/policy/` | Pure policy engine, rules P1–P15 |
| `judge/reasoning/` | Claude and heuristic judge, diagnosis, discussion, wiki chooser and writer |
| `judge/memory/` | LLM Wiki: git repo, docs lookup, query, ingest, proposal validator, lint, code-owned stats, GitHub mirror |
| `judge/remediation/` | Action catalog, planner, verifier with settle window, crash-safe runner, autonomy ladder |
| `judge/approvals/` | Bundled Slack approval card, Socket Mode button handler, approval verifier |
| `judge/connectors/` | Sentry, Linear, Instatus, Slack, PagerDuty, GitHub, ShopLab, plus a fault-injecting HTTP transport |
| `judge/narration.py` | Plain-language Slack and Linear text: evidence, exact change, verification progress, report |
| `judge/console/` | Read-only web console on :8700 |
| `judge/config/` | `catalog.yaml` (services), `actions.yaml` (typed actions), `policy.yaml`, `slos.yaml` |
| `judge/realapps.py` | `judge bootstrap` and `judge doctor` for the real apps |
| `sandbox/` | Local emulators of the Sentry, Linear, Instatus and Slack API subsets (same request shapes) |
| `evals/` | 30 scenarios, runner, simulated humans, graders (state, invariants, canaries), baselines, reports |
| `tests/` | 294 unit and integration tests |

## Commands

```bash
uv run judge dev [--time-scale 0.2] [--trial-id demo]   # full local stack (or real apps with IJ_BACKEND=real)
uv run judge run                                        # agent only
uv run judge console [--trial-id demo]                  # console only
uv run judge bootstrap | doctor                         # real apps: discover ids, verify each app
uv run judge incidents | decisions [INCIDENT_ID]        # state and audit log
uv run judge pause | resume                             # kill switch
uv run judge review-runbook RUNBOOK_ID                  # clear review_required after a failed fix
uv run judge memory proposals | merge ID | reject ID | lint [--apply]

uv run pytest -q
uv run python -m evals.runner --scenarios core|extended|all|ID,ID --k 3 [--baseline B0|B1|B2|B3]
```

## Configuration

All settings come from `.env` (git-ignored; template in [`.env.example`](.env.example)).

| Variable | Meaning |
|---|---|
| `IJ_BACKEND` | `sandbox` (local emulators, default) or `real` |
| `TIME_SCALE` | Compresses every window (settle, quiet, veto, approval) for local runs and evals |
| `ANTHROPIC_API_KEY`, `IJ_JUDGE_IMPL`, `IJ_JUDGE_MODEL`, `IJ_MEMORY_MODEL` | Claude; without a key the heuristic judge runs |
| `IJ_VAR_DIR` | Runtime state (SQLite, memory git repos) |
| App credentials | See [`docs/SETUP.md`](docs/SETUP.md) |

In sandbox mode real app identities are ignored, so tests and evals can never write to the real apps even with a
filled `.env`.

## Environment notes

- If the project sits on an exFAT/FAT volume, runtime state moves to `%LOCALAPPDATA%\incident-judge\var`
  automatically, because SQLite WAL fails there with "disk I/O error".
- The agent reads its knowledge from a local git repository created from [`../knowledge`](../knowledge). With
  `GITHUB_TOKEN` set, proposals are mirrored to this repository as pull requests.
