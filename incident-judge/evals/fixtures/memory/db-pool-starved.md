---
id: db-pool-starved
title: DB connection pool starved
signatures:
  fingerprints: [1664c5cb4162, b610cf7dfe66]
  error_types: [PoolTimeout]
  services: [checkout, catalog]
match_conditions:
- {metric: pool_utilization, service: '{service}', op: '>', value: 0.9, window_s: 60}
- {metric: db_query_p95, service: '{service}', op: '<', value: 0.1, window_s: 60}
action:
  name: scale_pool
  params: {size: 20}
# ---- code-owned zone — an LLM proposal that touches this is rejected ----
# Seed values only; eval runner recomputes these from seeded outcomes via stats.sync_code_owned.
stats: {success: 0, failure: 0, inconclusive: 0, failure_since_review: 0, last_verified: null, recent: []}
autonomy: {level: L0, cap: L2, review_required: false}
---

# DB connection pool starved

## Summary

The connection pool is too small for the traffic, so requests wait for a slot and time out; increasing the pool size fixes it.

## Symptoms

- `PoolTimeout` on `checkout` (`/pay`) or `catalog`, returning 503.
- `pool_utilization` ≈ 1.0 and `pool_wait_p95` rises sharply.
- `db_query_p95` stays **low** (< 100ms): each query runs fast, there are just not enough slots.
- `db_pool_size` is abnormally small (for example 2) compared with the default 10.

## Known root causes

- The `pool_size` configuration was lowered (a bad config change) and cannot keep up with ~30 rps of traffic.

## Remediation

- `scale_pool size=20` on the affected service — verified: `error_rate` < 2% and `pool_wait_p95` < 50ms within 90 seconds.
- Afterwards, find the config change that lowered the pool size.

## Tried and did not work

- `restart_service`: the pool size is re-read from config, so it stays small and the errors return.

## How to tell apart

- **Slow query**: pool symptoms look almost identical (`PoolTimeout`, high utilization) **but `db_query_p95` is high** (hundreds of ms).
  A bigger pool does not help — every request is still slow and times out. The `match_conditions` entry `db_query_p95 < 0.1`
  exists precisely to rule this case out. Slow query ⇒ escalate, do not scale the pool.
- **Database down**: connection errors on every service that depends on `db`, not just slot timeouts.

## Notes

- Applies to production services with a pool (`checkout`, `catalog`). Never applied automatically on staging.
