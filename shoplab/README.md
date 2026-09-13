# shoplab/ — the system under operation

ShopLab is the demo target for [Incident Judge](../README.md): a small but real online shop that actually breaks.
Incident Judge treats it like any production system it doesn't own, and changes it only through the control API.

## What runs

| Component | Port | What it is |
|---|---|---|
| Supervisor + control API | :8800 | Starts the services, holds runtime config (flags, pool size, versions), generates customer traffic, injects faults, records the change log |
| Storefront | http://127.0.0.1:8800 | What customers see: browse, search, pay |
| Control room | http://127.0.0.1:8800/ops | Live service health, fault injection, change log (who changed what, when) |
| `checkout`, `search`, `catalog`, `internal-batch` | own ports | Separate processes with a real connection pool, 1 s request timeout, Prometheus metrics and Sentry reporting |
| `checkout@staging` | | Same code as checkout, environment `staging`, never customer-facing |

Service behaviour, dependencies, failure modes and safe actions are documented for humans and for the agent in
[`knowledge/wiki/architecture.md`](../knowledge/wiki/architecture.md) and
[`knowledge/wiki/services/`](../knowledge/wiki/services/).

## Faults

| Fault | Service | What breaks |
|---|---|---|
| `bad_flag` | checkout | `payment_v2` flag flipped by `release-bot`, payments fail |
| `pool_starved` | checkout | Connection pool too small for the load, requests time out |
| `slow_query` | checkout | A slow query; looks like pool starvation in Sentry but isn't |
| `worker_hang` | search | Workers hang, latency climbs slowly |
| `bad_deploy` | checkout | A broken release |
| `staging_fire` | checkout@staging | Loud `CRITICAL` errors on staging only |
| `batch_fail` | internal-batch | Nightly report fails, no customer impact |
| `pii_leak`, `injection` | checkout | Error messages carrying PII or a prompt injection |
| `db_slow_shared` | checkout + search | Shared database slowdown: one incident, two services |
| `avatar_errors` | catalog | Many users, cosmetic feature |
| `pay_few_users` | checkout | Few users, but they cannot pay |

```bash
curl -X POST http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token" \
  -H "Content-Type: application/json" -d '{"fault":"bad_flag","service":"checkout"}'
curl -X DELETE http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token"
```

## Control API (used by the agent's catalog actions)

`GET /health` · `GET /services` · `GET /config/{service}` · `PUT /config/{service}/flags/{flag}` · `PUT /config/{service}/pool_size` ·
`POST /deploy/{service}` (deploy or roll back a version) · `POST /services/{service}/restart` · `GET /changes` ·
`GET /traffic` · `GET|POST|DELETE /faults`. Writes need `X-Control-Token`; the `X-Actor` header is recorded in the
change log.

## Run and test

```bash
uv run python -m shoplab.supervisor --port-base 8800 --sandbox-url http://127.0.0.1:8900   # usually started by `judge dev`
uv run pytest -q
```

When the Sentry DSN points at sentry.io, ShopLab applies an event budget (a burst of 5, then 1 event per 10 s per
error) so a demo never exhausts the free-plan quota.
