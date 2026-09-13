# Two-minute demo

Video: https://youtu.be/vH7voZyG5Fg

The demo runs on the **real apps**: Sentry, Linear, Instatus, Slack, PagerDuty and GitHub. ShopLab breaks for real on
the local machine.

## Preparation

```bash
cd incident-judge
uv run judge doctor                          # every app green
uv run judge dev --trial-id demo-final       # ShopLab + console + agent on the real apps (runbooks seeded from knowledge/)
```

Set `INSTATUS_SHOULD_PUBLISH=true` in `.env` for the recording only.

Arrange the screen:

| Window | URL |
|---|---|
| Slack | `#incident-judge-oncall` |
| ShopLab control room | http://127.0.0.1:8800/ops |
| Store | http://127.0.0.1:8800 |
| Console | http://127.0.0.1:8700 |
| Tabs | Status page (`INSTATUS_PAGE_URL`), Linear "My issues", the GitHub repository's Pull requests |

## Script

| Time | Show | Say |
|---|---|---|
| 0:00–0:12 | Store, then Slack (quiet) | "At 3 a.m. on-call has to answer four questions alone: how bad is it, can customers see it, is it one incident or several, and have we fixed this before? Incident Judge answers them, and acts only within limits enforced by code." |
| 0:12–0:30 | Control room → inject **bad_flag** on checkout. Store: payment fails. Slack: triage message appears | "Checkout breaks for real. The agent waits for the signal to settle, then judges: SEV1, customers affected. The cause: the `payment_v2` flag, flipped by release-bot 35 seconds before the first error. It opened one Linear ticket and assigned it to me." |
| 0:30–0:48 | Slack approval card: status-page post + fix, exact change, why, verify, rollback → click **Approve all** | "One card for everything that needs a human. The exact change, why it fixes the cause, how it will be verified and how it rolls back. This runbook has earned L1, so it needs one click." |
| 0:48–1:05 | Progress message updating: error rate 100% → 0%. Status page: Investigating → Monitoring | "It verifies on live traffic, not on Sentry going quiet. If the SLO doesn't recover, the change is rolled back, the runbook is demoted and PagerDuty pages on-call." |
| 1:05–1:20 | Slack report → Linear ticket Done → status page Resolved → console incident page (timeline, verification chart, audit) | "It resolves only after a quiet window with healthy SLOs. Every decision and the rule behind it is in the audit log." |
| 1:20–1:35 | GitHub pull request "Update runbook …" against `knowledge/` → click **Merge** on the Slack card | "Then it learns. Code writes the timeline and recomputes the runbook's track record, and the LLM can't touch those numbers. Its runbook update arrives as a pull request a human merges." |
| 1:35–1:50 | Inject **slow_query**. Slack: "looks like `db-pool-starved`, but its conditions do not hold (query p95 …)" | "This one looks identical in Sentry. A guessing agent would apply the same fix. The runbook's conditions are measured by code, so the agent refuses and diagnoses instead." |
| 1:50–2:00 | README results table: 30/30, 0 unsafe; ablation row | "Thirty scenarios, three trials each, graded on the real state of the apps: zero unsafe. The LLM proposes. Code decides." |

## Tips

- Clear faults between takes: `curl -X DELETE http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token"`.
- A new `--trial-id` gives a clean agent state; delete earlier Linear tickets and status-page incidents before the
  final take.
- To show escalation, leave an approval card unanswered: PagerDuty is paged when the approval window
  (10 minutes on the real apps) expires.
