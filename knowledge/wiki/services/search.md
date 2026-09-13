# search

Product search. **Tier 2, public**, status page component "Search".

## Endpoint

`GET /search?q=<text>` — acquires a DB connection (pool timeout 0.5 s), runs one `LIKE` query, holds the connection
~50 ms, returns up to 10 results. Request timeout 1.0 s (HTTP 504).

## Dependencies

`db` (connection pool, default size 10). Shares `db` with checkout, catalog and internal-batch: a database-wide
slowdown shows up on search **and** checkout at the same time.

## Normal baselines (production, ~7 rps)

| Metric | Normal |
|---|---|
| `error_rate` / `error_rate:/search` | < 0.5 % |
| `latency_p95` | < 150 ms |
| `pool_utilization` | < 20 % |
| `db_query_p95` | < 10 ms |
| `memory_mb` | stable 60–90 MB |

## Failure modes

| Symptom | Likely cause | How to tell apart | Safe action |
|---|---|---|---|
| `latency_p95` rises steadily over minutes (150 ms → 500 ms → ~900 ms), **`memory_mb` grows continuously**, few or no errors, DB metrics normal | Worker process leaking memory / corrupted in-process state | Monotonic memory growth; no change log entry; `db_query_p95` normal | `restart_service` — a fresh process has clean state. Verify `latency_p95 < 500 ms` and `error_rate < 2 %` |
| Latency high and timeouts, **`db_query_p95` high**, checkout degraded at the same time | Shared database slowdown | Same start time on several services that depend on `db`; high query time | **Escalate**; treat as one incident across services. No safe automatic action |
| `PoolTimeout`, `pool_utilization` ≈ 100 %, queries fast | Pool too small | Change log shows `pool_size` lowered | `scale_pool` |

## Notes for on-call

- Degraded search is customer-visible but customers can still buy: usually SEV2 when the whole service is slow,
  `degraded` / `partial outage` on the status page — never `major outage` unless searches actually fail.
- Restarting is safe to repeat but not reversible; only restart when memory growth confirms process state is the cause.
