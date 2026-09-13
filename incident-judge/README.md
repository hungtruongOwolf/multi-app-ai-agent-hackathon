# Incident Judge

An on-call agent that **judges** incidents — how severe, whether customers can see it, whether it shares a root cause
with something already open, and whether we have fixed it before — then acts across Sentry, Linear, Instatus, Slack
and a git-backed runbook wiki. Known failures can be fixed automatically, but only with autonomy the runbook has
**earned** through verified outcomes, and that autonomy is lost on the first failed fix.

> **The LLM proposes, code decides. The LLM maintains understanding; code holds the numbers and the permissions.**

- Design: [`SPEC.md`](SPEC.md)
- Module contracts, local-first deviations and integration findings: [`docs/CONTRACTS.md`](docs/CONTRACTS.md)
- Reliability brief (for judges): [`docs/brief.md`](docs/brief.md) · Demo script: [`docs/demo.md`](docs/demo.md)
- Connecting the real apps: [`docs/SETUP.md`](docs/SETUP.md)

## What you see

| Where | What it is |
|---|---|
| **Slack thread** (one per incident) | Triage with evidence, one approval card with buttons, live verification, discussion with the agent, final report |
| **Console** http://127.0.0.1:8700 | Every incident as a story: timeline, evidence, diagnosis, who approved what, exact change, verification chart, policy audit, links to Slack · Linear · Sentry · status page |
| **ShopLab store** http://127.0.0.1:8800 | A real shop; when checkout breaks you see what customers see |
| **ShopLab control room** http://127.0.0.1:8800/ops | Inject faults, live service health, change log (who changed what, when) |
| **Linear** | One ticket per incident: summary, impact, evidence table, diagnosis, plan; a comment per decision |
| **Status page** (Instatus) | Template-only public updates: investigating → identified → monitoring → resolved |

## How it reasons

1. **Known failure** — a runbook matches on fingerprint or symptoms, *and* its machine-checked conditions hold on live
   metrics → propose the runbook's fix with its track record. Autonomy (L0–L3) is earned from verified outcomes.
2. **New or look-alike failure** — the agent reads the service docs and architecture in the wiki (LLM Wiki pattern),
   the ShopLab change log and the metrics, then explains the most likely causes with evidence, *why* it happens, and a
   catalog fix with *why it addresses the cause* — or says it needs a human. Diagnosis fixes never run without a click.
3. **Discussion** — reply in the thread: ask "why?" and get an answer grounded in evidence and docs; propose your own
   fix ("roll back to 1.4.1") and it becomes a concrete, verifiable plan card credited to you.
4. **Learning** — after resolution the raw timeline is written by code, stats and autonomy are recomputed by code, and
   the LLM proposes a runbook update that a human merges from Slack.

## Results

30 scenarios × 3 independent trials (90 trials), graded on final app state + audit-log invariants + seeded canaries:

| pass^3 | unsafe | mixed | rollback correctness | look-alike runbooks rejected |
|---|---|---|---|---|
| **30/30** | **0** | **0** | 100% | 100% |

Baselines on the same scenarios fail exactly where the removed component matters: no policy engine (B0) leaks PII to
the status page, posts staging incidents publicly, resolves under social pressure and executes fixes without approval;
no memory (B1) never finds or writes runbooks; no verification (B2) resolves early and records wrong fixes as
successes; no machine-checked runbook conditions (B3) applies the wrong fix to a look-alike incident.
Reports: `var/reports/full-k3/report.html` and `var/reports/base-*`.

## Quick start (local, no external accounts)

Requirements: Python 3.12 via [uv](https://docs.astral.sh/uv/), git. No Docker.

```bash
uv sync
uv run pytest -q          # unit + integration tests
uv run judge dev          # sandbox APIs :8900 + ShopLab :8800 + console :8700 + agent
```

Open:
- Console: http://127.0.0.1:8700 · Store: http://127.0.0.1:8800 · Control room: http://127.0.0.1:8800/ops
- Slack emulator with Approve/Reject buttons: http://127.0.0.1:8900/slack/ui
- Public status page emulator: http://127.0.0.1:8900/status/page_shoplab

Inject a real fault into ShopLab:

```bash
curl -X POST http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token" \
  -H "Content-Type: application/json" -d '{"fault":"bad_flag","service":"checkout"}'
```

Available faults: `bad_flag`, `pool_starved`, `slow_query`, `worker_hang`, `bad_deploy`, `staging_fire`, `batch_fail`,
`pii_leak`, `injection`, `db_slow_shared`, `avatar_errors`, `pay_few_users` (see CONTRACTS §2.2).
Clear them: `curl -X DELETE http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token"`.

Inspect the agent:

```bash
uv run judge incidents --trial-id demo
uv run judge decisions --trial-id demo       # audit log of every policy decision
uv run judge memory proposals --trial-id demo
uv run judge pause | resume                   # kill switch
```

## LLM

Set `ANTHROPIC_API_KEY` in `.env` (never committed). The triage judge uses `claude-sonnet-5`, the wiki
chooser/writer uses `claude-opus-5` (`IJ_JUDGE_MODEL`, `IJ_MEMORY_MODEL`). Without a key the agent falls back to a
deterministic heuristic judge; every report states which judge ran.

## Real apps

`IJ_BACKEND=real` switches every connector to the real SaaS APIs (same request shapes the sandbox emulates).
Follow [`docs/SETUP.md`](docs/SETUP.md), then:

```bash
uv run judge bootstrap     # discovers IDs (Linear team/label, Instatus components, Slack channel, Sentry DSNs) into .env
uv run judge doctor        # per app: auth, one write, read it back, clean up
```

## Evaluation

```bash
uv run python -m evals.runner --scenarios all --k 3 --parallel 2
uv run python -m evals.runner --scenarios core --k 1 --baseline B0
uv run python -m evals.report var/reports/<run> --compare var/reports/<baseline-run>
```

| Baseline | Removes |
|---|---|
| B0 | policy engine + public templates |
| B1 | memory (runbooks) |
| B2 | post-fix verification |
| B3 | machine-checked runbook `match_conditions` |

## Architecture

```
ShopLab (real faults, real metrics) ─► Sentry + SLO poller ─► per-incident state machine (SQLite outbox)
   ─► gather ─► runbook lookup (LLM Wiki) ─► LLM judge (JSON proposal, no credentials)
   ─► policy engine (P1–P15, pure code) ─► executor (only holder of credentials)
   ─► Linear · Instatus · Slack · ShopLab control ─► SLO verification ─► rollback ─► audit + wiki proposal
```

| Path | Contents |
|---|---|
| `judge/core` | models, SQLite store, idempotent outbox |
| `judge/policy` | deterministic policy engine |
| `judge/reasoning` | Claude / heuristic judge, LLM wiki chooser + writer |
| `judge/memory` | LLM Wiki: local git repo, query, ingest (proposals), validator, lint, code-owned stats |
| `judge/remediation` | typed actions, planner, verifier, crash-safe runner, autonomy ladder |
| `judge/approvals` | Slack approvals (text commands / buttons), verifier |
| `judge/connectors` | Sentry, Linear, Instatus, Slack, ShopLab + fault-injecting transport |
| `shoplab/` | target system: 4 services + staging, real pool, traffic, fault injector |
| `sandbox/` | local emulator of the Sentry/Linear/Instatus/Slack API subsets |
| `evals/` | 30 scenarios, runner, simulated humans, graders, reports, baselines |

## Environment notes

- Runtime state (SQLite, memory git repos) lives in `IJ_VAR_DIR`. If the project sits on an exFAT/FAT volume it moves to
  `%LOCALAPPDATA%\incident-judge\var` automatically, because SQLite WAL fails there with "disk I/O error".
- The runbook wiki is a **local** git repository with no remote. Nothing is pushed anywhere.
