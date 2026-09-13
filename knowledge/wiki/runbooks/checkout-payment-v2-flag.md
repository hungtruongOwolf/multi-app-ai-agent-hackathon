---
id: checkout-payment-v2-flag
title: Payment provider v2 flag breaks checkout
signatures:
  fingerprints: [6d269003861e]
  error_types: [ConnectionError]
  services: [checkout]
match_conditions:
- {metric: error_rate, service: '{service}', op: '>', value: 0.05, window_s: 60}
action:
  name: toggle_flag
  params: {flag: payment_v2, value: false}
# ---- code-owned zone — an LLM proposal that touches this is rejected ----
# Seed values only; eval runner recomputes these from seeded outcomes via stats.sync_code_owned.
stats: {success: 0, failure: 0, inconclusive: 0, failure_since_review: 0, last_verified: null, recent: []}
autonomy: {level: L0, cap: L3, review_required: false}
---

# Payment provider v2 flag breaks checkout

## Summary

Turning on the `payment_v2` feature flag makes `/pay` call the payment provider v2, which is not ready; turning the flag off stops the errors.

## Symptoms

- `ConnectionError: payment provider v2 unreachable` on `checkout` `/pay`, firing continuously.
- Checkout `error_rate` jumps (usually > 30%) right after the flag is turned on.
- Latency does not rise noticeably — errors return fast, they are not timeouts.
- `app_flag{service="checkout",flag="payment_v2"} = 1`.

## Known root causes

- The `payment_v2` flag routes payments to the provider v2 endpoint, which is not yet open for production.
- Unrelated to the database or the connection pool.

## Remediation

- `toggle_flag payment_v2=false` on `checkout` — verified: `error_rate` back below 2% within 30–60 seconds.
- After turning the flag off, tell the payments team before turning it back on.

## Tried and did not work

- `restart_service checkout`: the flag is re-read from config, so the errors return as soon as the process is up.
- `scale_pool`: unrelated, the pool is not saturated.

## How to tell apart

- **Pool starvation (`db-pool-starved`)**: errors are `PoolTimeout`, `pool_utilization` > 0.9. Here the pool is normal.
- **Bad deploy (`app_version=1.4.2`)**: errors are `KeyError('currency')`, appearing after a deploy rather than a flag change.
- If the flag is off and `ConnectionError` still occurs ⇒ not this runbook (the provider is actually down).

## Notes

- The flag only affects `checkout`; it does not exist on staging.
