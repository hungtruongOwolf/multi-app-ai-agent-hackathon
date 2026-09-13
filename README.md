# Multi-App AI Agent Hackathon — Incident Judge

**Incident Judge** is an on-call agent that *judges* production incidents — how severe, whether customers can see
it, what most likely caused it, whether we've fixed it before — and then acts across **Sentry, Linear, Instatus,
Slack, GitHub and PagerDuty**. Known failures are fixed with autonomy the runbook has *earned* from verified
outcomes; new failures are diagnosed from the service docs, the change log and live metrics; engineers can discuss
and co-fix in the incident thread.

> **The LLM proposes, code decides.** The LLM maintains understanding; code holds the numbers and the permissions.

## Repository layout

| Folder | What it is |
|---|---|
| [`incident-judge/`](incident-judge/) | The agent: triage judge, policy engine, remediation + verification, approvals, diagnosis & discussion, memory (LLM Wiki), connectors, console dashboard, local API sandbox, eval harness |
| [`shoplab/`](shoplab/) | The target system: a real shop (storefront, checkout, search, catalog, batch job), control room `/ops`, change log, fault injector |
| [`knowledge/`](knowledge/) | The knowledge base the agent reads and improves: architecture, per-service docs (failure modes, safe actions), runbooks, incident timelines |

## Results

30 scenarios × 3 independent trials, graded on final app state, audit-log invariants and seeded canaries:
**pass^3 30/30, 0 unsafe, 0 mixed**. Removing a component breaks exactly what it protects (no policy engine: 50%
unsafe; no verification: 75% unsafe). Details: [`incident-judge/docs/brief.md`](incident-judge/docs/brief.md) ·
reports in [`incident-judge/docs/results/`](incident-judge/docs/results/).

## Run it

```bash
uv sync --all-packages
cd incident-judge && uv run judge dev     # sandbox APIs, ShopLab, console and the agent — no accounts needed
```

- Console http://127.0.0.1:8700 · Store http://127.0.0.1:8800 · Control room http://127.0.0.1:8800/ops
- Real apps: [`incident-judge/docs/SETUP.md`](incident-judge/docs/SETUP.md), then `uv run judge bootstrap` and `uv run judge doctor`
- Tests: `cd incident-judge && uv run pytest` · `cd shoplab && uv run pytest`
- Evals: `cd incident-judge && uv run python -m evals.runner --scenarios all --k 3`

More: [`incident-judge/README.md`](incident-judge/README.md) · design [`incident-judge/SPEC.md`](incident-judge/SPEC.md).
