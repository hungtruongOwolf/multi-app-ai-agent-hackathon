# Incident log

Append-only. One event per line: `time | incident | fingerprint | service | severity | outcome | runbook`.
Lines labelled `candidate` = an incident class seen for the first time, no runbook yet.
- 2026-09-13T20:21Z | inc_5a5dfbd505 | fp:6d269003861e | checkout | SEV1 | success | runbook:checkout-payment-v2-flag | occurrence:1
- 2026-09-13T20:30Z | inc_e49eaae312 | fp:1664c5cb4162 | checkout | SEV1 | no-action | runbook:db-pool-starved | occurrence:2
