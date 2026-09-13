# Two-minute demo — running for real on a local machine

Preparation (before recording):

```bash
uv run judge dev --time-scale 0.2          # sandbox + ShopLab + agent, runbooks seeded at L1
```

Keep three windows open: **Slack UI** http://127.0.0.1:8900/slack/ui · **Status page**
http://127.0.0.1:8900/status/page_shoplab · a terminal for injecting faults and running
`uv run judge decisions --trial-id demo`.

Handy variables:

```bash
F='curl -s -X POST http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token" -H "Content-Type: application/json" -d'
CLEAR='curl -s -X DELETE http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token"'
```

| Time | Do | Say |
|---|---|---|
| 0:00–0:15 | Empty Slack UI | "On-call at 3 a.m. has to answer four questions alone: how severe, can customers see it, is it a duplicate, how did we fix it last time. Existing tools do the plumbing; we do the judgment and the learning." |
| 0:15–0:45 | `$F '{"fault":"pool_starved","service":"checkout"}'` → Slack: SEV1 triage, war room, **[IJ-PUBLIC]** and **[IJ-FIX]** cards (runbook `db-pool-starved`, L1, 3/3 successes) → click **Approve as U_ONCALL_1** on both | "A real failure on ShopLab. The agent finds the runbook, but this runbook's self-fix right is only L1 — a human has to click." |
| 0:45–1:05 | ":wrench: Executing scale_pool" → "Verify PASS" → status page moves to MONITORING → after the quiet window: RESOLVED; `judge memory proposals` shows a proposal updating the runbook | "Verified on SLOs, not on 'Sentry went quiet'. If it fails, it rolls back on its own. The experience goes back into the wiki through a human-reviewed proposal — success counts are computed by code, the LLM cannot edit them." |
| 1:05–1:25 | `$CLEAR`; `$F '{"fault":"slow_query","service":"checkout"}'` → Slack: "Runbook db-pool-starved looks similar but its machine-checked conditions do NOT match (db_query_p95 …)" | "It looks exactly like last time. A guessing agent would press the wrong fix. The runbook carries discriminators; code measures them and refuses — P7." |
| 1:25–1:40 | `$F '{"fault":"staging_fire"}'` → Slack ":no_entry: Blocked public status page post — P1: environment=staging". Then `$F '{"fault":"injection","service":"checkout"}'` → nothing is resolved or restarted | "The word CRITICAL doesn't lead it. Neither does a prompt injection inside the error message — because none of the guardrails live in the prompt." |
| 1:40–1:55 | Open `var/reports/<run>/report.html`: 30 scenarios × 3, pass^3, unsafe, mixed; baseline B0 next to it | "Graded on the real state of the apps, invariants over the audit log, and canaries." |
| 1:55–2:00 | — | "The LLM proposes, code decides. Automation rights are earned through a track record — and lost the moment a fix fails." |

Record the real screen with voice-over. If a step is slow, a smaller `TIME_SCALE` shortens the windows.
