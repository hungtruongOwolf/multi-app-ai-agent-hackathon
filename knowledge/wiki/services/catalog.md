# catalog

Product pages and customer profiles. **Tier 2, public**, status page component "Product pages".

## Endpoints

| Endpoint | Journey | Criticality |
|---|---|---|
| `GET /products/{id}` | view a product (one query, ~30 ms connection hold) | **Critical** |
| `POST /profile/avatar` | upload a profile picture (image transcoder, no DB) | Secondary — cosmetic |

Request timeout 1.0 s (HTTP 504).

## Dependencies

`db` (connection pool, default size 10), `cache`, image transcoder (avatars).

## Normal baselines (production, ~10 rps, of which ~30 % avatar uploads)

| Metric | Normal |
|---|---|
| `error_rate` | < 0.5 % |
| `error_rate:/products/{id}` | < 0.5 % |
| `latency_p95` | < 150 ms |
| `pool_utilization` | < 20 % |
| `db_query_p95` | < 10 ms |

## Failure modes

| Symptom | Likely cause | How to tell apart | Safe action |
|---|---|---|---|
| `OSError: avatar storage bucket rejected upload: image transcoder unavailable`, service error rate ~25–30 %, **`error_rate:/products/{id}` normal**, many users affected | Image transcoder down | Only `/profile/avatar` fails; product pages are healthy | **Escalate** (no catalog action fixes a third-party transcoder). Impact is secondary: at most SEV3, customers can still browse and buy |
| `PoolTimeout`, product pages failing, `pool_utilization` ≈ 100 %, queries fast | Pool too small | Change log shows `pool_size` lowered | `scale_pool` |
| Product pages slow with high `db_query_p95`, other services slow too | Shared database slowdown | Same start on several `db` services | **Escalate** |

## Notes for on-call

- Judge impact by the critical route: many failing avatar uploads matter less than a few failing product pages.
