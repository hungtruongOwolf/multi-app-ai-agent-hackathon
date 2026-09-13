# internal-batch

Background jobs for internal reporting. **Tier 3, internal** — no public status page component, no customer impact.

## Job

`nightly-report` — runs every 10 s in ShopLab (compressed schedule): takes a DB connection, aggregates product
revenue, holds the connection ~100 ms. Metric: `batch_jobs{job="nightly-report", status="ok|error"}`.
There is no HTTP traffic, so request metrics (`error_rate`, `latency_p95`, `rps`) are not available for this service;
use Sentry errors and the batch job counter.

## Dependencies

`db` (connection pool, default size 10).

## Failure modes

| Symptom | Likely cause | How to tell apart | Safe action |
|---|---|---|---|
| `RuntimeError: nightly-report cron failed` every run | Report job failing | Error only in internal-batch; customer services healthy | **Escalate** to the owning team via an internal ticket. Never post publicly, never page as customer-facing |
| Job slow or `PoolTimeout` while customer services are also slow | Shared database slowdown | Several `db` services affected together | Handle as the shared incident |

## Notes for on-call

- Severity is SEV3/SEV4 at most: reports can be re-run later.
