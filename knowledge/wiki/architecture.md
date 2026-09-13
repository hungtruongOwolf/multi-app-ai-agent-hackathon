# ShopLab architecture

ShopLab is a small online shop. Four production services and one staging copy of checkout run as separate
processes managed by a supervisor, which also holds runtime configuration and generates synthetic customer traffic.

## Customer journeys

| Journey | Service | Endpoint | Business criticality |
|---|---|---|---|
| Pay for an order | `checkout` | `POST /pay` | **Critical** — failed payments are lost revenue |
| Search the catalog | `search` | `GET /search?q=` | Critical for discovery, degraded search still lets customers buy |
| View a product | `catalog` | `GET /products/{id}` | Critical |
| Upload a profile avatar | `catalog` | `POST /profile/avatar` | Secondary — cosmetic, customers can still buy |
| Nightly revenue report | `internal-batch` | background job `nightly-report` | Internal only, no customer impact |

## Topology and dependencies

```
customers ──► checkout (tier 1, public) ──► db (per-service SQLite behind a connection pool)
          │                              └─► payment provider (v1 default; v2 behind flag payment_v2)
          ├─► search   (tier 2, public)  ──► db
          ├─► catalog  (tier 2, public)  ──► db, cache
          └   internal-batch (tier 3, internal) ──► db
checkout@staging: same code as checkout, environment=staging, never customer-facing
```

- Every service talks to the database through an **application connection pool** (default size **10**, acquire
  timeout **0.5 s**). Each request holds a connection for its query plus a short transaction hold
  (checkout 250 ms, search 50 ms, catalog 30 ms, internal-batch 100 ms).
- Every HTTP request has a hard **1.0 s timeout**; a request that runs longer returns **504** and counts as an error.
- `db`, `cache` and `payment-provider` are shared dependencies. Incidents on services that share a dependency and
  start together are candidates for one root cause (see each service's "Failure modes").

## Runtime configuration (held by the supervisor, polled by services every 1 s)

| Setting | Default | Who changes it | Recorded in the change log |
|---|---|---|---|
| `flags.payment_v2` (checkout only) | `false` | release tooling, on-call via `toggle_flag` | yes |
| `pool_size` | `10` | capacity changes, on-call via `scale_pool` | yes |
| `app_version` | `1.4.1` (known: 1.4.1, 1.4.2) | deploys, on-call via `rollback_deploy` | yes |
| process restart | — | on-call via `restart_service` | yes |

The **change log** (`GET /changes` on the supervisor) is the first place to look when errors start suddenly:
a flag flip, deploy or pool change on the same service shortly before the first error is the most likely cause.
Infrastructure degradation (slow database, leaking worker) does **not** appear in the change log — diagnose it from
metrics.

## Metrics (per service, scraped from `/metrics`)

| Metric | Meaning | Normal |
|---|---|---|
| `error_rate` | share of requests answered with 5xx (including 504 timeouts) | < 0.5 % |
| `latency_p95` | 95th percentile request duration | checkout 300–500 ms (includes the 250 ms hold), search/catalog < 150 ms |
| `rps` | requests per second (synthetic traffic ~30 rps in production: checkout ~40 %, search ~25 %, product pages ~25 %, avatars ~10 %) | checkout ~12, search ~7, catalog ~10 |
| `pool_utilization` | connections in use ÷ pool size | checkout 20–40 %, others < 20 % |
| `pool_wait_p95` | time to acquire a connection | < 10 ms |
| `db_query_p95` | database query time | < 10 ms |
| `memory_mb` | resident memory of the process | stable, 60–90 MB |

Per-route variants exist for critical journeys, e.g. `error_rate:/pay`.

## Safe actions (judge/config/actions.yaml)

| Action | What it does | Reversible | Use when | Never use when |
|---|---|---|---|---|
| `toggle_flag` | set a feature flag on one service | yes | a recent flag change correlates with the errors | the flag has not changed recently |
| `scale_pool` (size 5–50) | change the connection pool size | yes | the pool is saturated **and** queries are fast | queries are slow — a bigger pool only adds load |
| `restart_service` | restart one service process | no (safe to repeat) | in-process state is corrupt: latency and memory grow steadily | errors come from config, deploys or dependencies |
| `rollback_deploy` | deploy a known-good version | no (needs a new deploy) | errors started right after a deploy | the version has not changed |

When no action fits the evidence: **escalate** to a human and say what is unknown.
