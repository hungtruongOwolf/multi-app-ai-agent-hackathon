<div align="center">
  <img src="assets/brand/logo-mark.svg" alt="Incident Judge logo" width="96" height="96" />

# Incident Judge

### The LLM proposes. Code decides.

**An on-call agent that judges production incidents, fixes known failures with autonomy it has earned,
and writes down what it learned for the next engineer.**

It decides how severe an incident is, whether customers can see it, what most likely caused it, and whether
the team has fixed it before. Then it acts across **Sentry, Linear, Instatus, Slack, PagerDuty and GitHub**,
without ever letting the model hold a credential or make the final call.

[Watch the 2-minute demo](#demo-video) ·
[Reliability brief](incident-judge/docs/brief.md) ·
[Eval report](incident-judge/docs/results/full-k3/report.md) ·
[Setup guide](incident-judge/docs/SETUP.md)

`Python 3.12` · `Claude` · `FastAPI` · `SQLite` · `Slack Socket Mode` · `LLM Wiki` · `MIT`

</div>

---

## Submission at a glance

| Requirement | Where |
|---|---|
| Project overview | [What Incident Judge does](#what-incident-judge-does) · [How an incident flows](#how-an-incident-flows) |
| External apps used | [Connected apps: role and triggers](#connected-apps-role-and-triggers) |
| Technical depth | [LLM Wiki knowledge base](#the-knowledge-base-karpathys-llm-wiki-made-safe-to-act-on) · [Engineering highlights](#engineering-highlights) · [Architecture](#architecture) |
| Setup instructions | [Quick start (no accounts)](#quick-start-5-minutes-no-accounts) · [Connect the real apps](#connect-the-real-apps) |
| How we tested reliability | [Reliability and evaluation](#reliability-and-evaluation) · [full brief](incident-judge/docs/brief.md) |
| Two-minute demo | [Demo video](#demo-video) |
| License | [MIT](LICENSE) |

**Results:** 30 scenarios × 3 independent trials, graded on the final state of the apps. **30/30 scenarios pass all
three trials, 0 unsafe trials, 0 flaky scenarios.** 315 unit and integration tests pass.

---

## Why this problem

### The first 15 minutes of an incident are not spent fixing

At 3 a.m. the on-call engineer is paged and has to answer four questions alone, fast, with partial information:

1. **How bad is it?** A `CRITICAL` log line on staging matters less than eight customers who cannot pay.
2. **Can customers see it?** If yes, the status page must say so. But a wrong public post can't be taken back.
3. **Is it one incident or three?** A slow database breaks checkout *and* search at the same time.
4. **Have we fixed this before?** Usually yes. The fix lives in someone's head, in a Slack thread from last
   quarter, or in a runbook that no longer matches reality.

The actual fix is often one config change. The time goes elsewhere:

| Where the time goes | What it looks like |
|---|---|
| **Re-investigating a repeat** | The same flag, the same exhausted pool, the same bad deploy. Each recurrence is debugged from scratch because last time's reasoning was never written down in a findable place. |
| **Jumping between six tools** | Sentry for the error, dashboards for the metrics, the change log for "what changed", Linear for the ticket, the status page for customers, Slack for the team, PagerDuty for escalation. Coordination is manual and easy to get wrong under pressure. |
| **Onboarding in the middle of an outage** | A new engineer doesn't know which service owns `/pay`, what "normal" latency is, which fixes are safe to try, or who changed what. They either wait for a senior engineer or guess. |
| **Look-alike incidents** | A slow query and an undersized connection pool produce the same `PoolTimeout` in Sentry. Applying last week's fix to this week's look-alike makes things worse. |
| **Knowledge that decays** | Post-mortems are written late or never. Runbooks drift from the system they describe and nobody knows which ones are still trustworthy. |

### What changes with Incident Judge

| Before | With Incident Judge |
|---|---|
| Each repeat incident is re-investigated | A runbook whose conditions **hold on live metrics** proposes the fix with its track record ("4 verified successes, 0 failures"). One click, and it is verified on SLOs. |
| Six tools, updated by hand | One Slack thread with the evidence, one approval card, and every app updated exactly once: Linear, status page, Sentry, PagerDuty, GitHub. |
| New engineers need a senior to explain the system | The triage explains itself in plain language: which service, what is normal, what changed, why this cause, what exactly will change and how it rolls back. The knowledge base has a doc for every service. |
| Look-alikes get the wrong fix | Runbooks carry machine-checked discriminators. The agent refuses the look-alike and diagnoses instead. |
| Post-mortems written late, runbooks drift | The timeline is written by code at resolution. The runbook update arrives as a pull request. Stats and autonomy are recomputed from outcomes, so trust is earned and lost automatically. |

In a live run on the real apps, a checkout outage went from first error to verified fix in **2 min 13 s**, with one
human click.

### Why not just give an LLM the keys?

An LLM is good at reading evidence and explaining it, which is exactly the judgment and writing that on-call lacks.
But an agent that sounds confident can post a false outage publicly, "fix" a look-alike, follow instructions hidden in
an error message, or rewrite its own track record. Incident Judge splits the work:

> **The LLM maintains understanding. Code holds the numbers and the permissions.**

## What Incident Judge does

| Capability | What it means in practice |
|---|---|
| **Judges incidents** | Severity (SEV1–SEV4), customer visibility, likely cause and duplicates, from Sentry errors, live SLO metrics and the change log. It never goes by keywords like "CRITICAL". |
| **Acts across real apps** | One Linear ticket, one Slack thread, one status-page incident and at most one PagerDuty page per incident, even through retries, crashes and lost API responses. |
| **Fixes known failures** | A runbook match is checked on live metrics by code before a fix is proposed. Every fix is a typed catalog action with a verification plan and a rollback. |
| **Earns autonomy** | Each runbook has a level: L0 suggest → L1 one-click confirm → L2 runs unless vetoed → L3 automatic. The level is computed from verified outcomes, and one failed fix drops it back to L1. |
| **Diagnoses new failures** | With no runbook, it reads the service docs, the architecture page, the change log and metrics. It then explains the most likely cause with evidence and proposes a catalog fix, or pages a human. |
| **Discusses and co-fixes** | Engineers reply in the Slack thread: "why do you think it's the flag?", "roll back to 1.4.1 instead". It answers from evidence, and a human's fix becomes a verifiable plan card credited to them. |
| **Learns** | After resolution, code writes the raw timeline and recomputes stats. The LLM proposes a runbook update as a **GitHub pull request** that a human merges. |

## How an incident flows

<p align="center">
  <img src="assets/diagrams/incident-flow.png" alt="How an incident flows: signals, judgment, runbook or diagnosis, policy engine, one Slack card, execute, verify, resolve everywhere, learn" width="900" />
</p>

A real run on the real apps, step by step:

1. **Detect.** ShopLab's checkout starts failing (a bad feature flag). Errors land in Sentry, and the SLO poller sees
   the error rate burning.
2. **Judge.** After a short settle window, so it doesn't triage the first stray error, the agent judges: *SEV1,
   customer-visible, checkout, flag `payment_v2` flipped by `release-bot` 35 s before the first error*.
3. **Record.** It opens one Linear ticket (In Progress, assigned to on-call) with a readable summary, impact and
   evidence table, and posts the triage in the on-call Slack channel with the evidence and the known fix.
4. **Ask once.** It posts **one approval card** with buttons: the public status-page post and the fix, each shown with
   the exact change, why it addresses the cause, how it will be verified and how it rolls back.
5. **Act.** An allowlisted engineer clicks *Approve all*. The status page goes to *Investigating*, and the flag is
   turned off through ShopLab's control API.
6. **Verify.** The progress message updates in place: checkout error rate `100% → 0%` on live traffic. The status page
   moves to *Monitoring*. If SLOs don't recover, the fix is rolled back, the runbook is demoted and PagerDuty pages on-call.
7. **Resolve.** After a quiet window with healthy SLOs, the status page is *Resolved*, the Linear ticket is *Done*,
   any PagerDuty incident is resolved, and a short report with timeline and links is posted to the thread.
8. **Learn.** Code writes the raw timeline and recomputes the runbook's stats and autonomy. The LLM drafts a runbook
   update, which becomes a GitHub pull request against [`knowledge/`](knowledge/). Merge it on GitHub or click
   *Merge* in Slack, and the next incident uses it.

## Connected apps: role and triggers

| App | Role | What the agent does | When it is triggered |
|---|---|---|---|
| **Sentry** | Signal source | Reads unresolved issues and their latest event (production and staging projects), and marks the incident's issues resolved when it resolves. ShopLab services report errors with `sentry-sdk`. | Polled every tick (~2 s). A new or regressing issue opens or updates an incident. Resolved at incident resolve (`sentry.resolve_issue`), so a recurrence shows up as a regression. |
| **SLO metrics** (ShopLab Prometheus) | Signal and verification | Burn-rate, latency and pool metrics for triage, runbook conditions and fix verification. | Every tick. Before a runbook may be used, after a fix (verification) and before resolve (P4). |
| **Claude** (Anthropic) | Reasoning | `claude-sonnet-5` triages. `claude-opus-5` diagnoses, discusses, chooses runbooks and writes wiki proposals. Output is JSON; the model holds no credentials. | On every new incident, on material change (retriage), when no runbook matches, on each human reply in the thread, and after resolution. |
| **Slack** | Human interface | Triage message, one bundled approval card with buttons (Socket Mode), live verification progress, discussion, final report. | Every incident. Buttons and replies are handled in real time; allowlisted approvers only (P14). |
| **Linear** | Internal record | One ticket per incident (In Progress, assigned), a comment per decision, closed on verified resolution. | Created at triage. Commented on approvals, fix, verification and pages. Closed at resolve. |
| **Instatus** | Public status page | Template-only updates: investigating → identified → monitoring → resolved, with component status. | Only for customer-visible production incidents (P1–P3). Major impact needs a human click (P6), and a timeout means no post. |
| **PagerDuty** | Escalation | Triggers one PagerDuty incident per incident (`dedup_key`) and resolves it automatically. | Nobody approved the fix or public post in time, a fix failed verification, or a SEV1/SEV2 has no safe fix. |
| **GitHub** | Knowledge review | Runbook updates become pull requests against `knowledge/`. Timelines and code-owned stats are committed to `main`. | After an incident closes with a proposal. A merge on GitHub or in Slack is detected, validated (P12) and applied. |
| **ShopLab** (demo target) | The system being operated | Control API for catalog actions (toggle flag, scale pool, restart, roll back) and the change log. | Only via an approved or earned catalog action. Every change is logged with its actor. |

**Nothing talks to an app except the executor, and the executor runs only what the policy engine allowed.**
Every write carries an idempotency marker, so a crash or lost response never creates a second ticket, message or
status-page incident.

## The knowledge base: Karpathy's LLM Wiki, made safe to act on

Andrej Karpathy's **LLM Wiki** pattern replaces "retrieve chunks and re-derive the answer every time" with a small,
curated wiki that an LLM *maintains*: raw sources go in, the model compiles them into linked pages, a schema file tells
it how, and three operations keep it alive: **ingest**, **query** and **lint**. Knowledge compounds instead of being
rediscovered at 3 a.m.

[`knowledge/`](knowledge/) implements that pattern for on-call. The twist: these pages drive **real production
actions**, so every layer has an owner and code guards the parts that must not be hallucinated.

<p align="center">
  <img src="assets/diagrams/knowledge-graph.png" alt="Knowledge graph: services, runbooks, catalog actions, incidents and pull requests, as the console draws them" width="900" />
</p>

| Layer (Karpathy) | In Incident Judge | Owner |
|---|---|---|
| **Raw sources** | [`raw/incidents/`](knowledge/raw/): one immutable, redacted timeline per incident, generated from the audit log | Code |
| **Wiki pages** | [`wiki/runbooks/`](knowledge/wiki/runbooks/): one page per incident class (summary, symptoms, causes, remediation, *tried and did not work*, *how to tell apart*) | LLM, via reviewed pull requests |
| **Reference pages** | [`wiki/architecture.md`](knowledge/wiki/architecture.md), [`wiki/services/`](knowledge/wiki/services/): what "normal" is, dependencies, failure modes, safe actions | Humans |
| **Schema** | [`AGENTS.md`](knowledge/AGENTS.md): page format, write rules, operations | Humans |
| **Index and log** | [`wiki/index.md`](knowledge/wiki/index.md) regenerated on merge, [`wiki/log.md`](knowledge/wiki/log.md) append-only | Tooling |
| **Code-owned zone** | `stats`, `autonomy` and `match_conditions` in each runbook's front matter | Code (stats, autonomy), humans (conditions) |

**The three operations, split between the LLM and code**

| Operation | LLM | Code |
|---|---|---|
| **Query** (new incident) | Reads `index.md` and picks a runbook, or abstains | Exact fingerprint match first; then checks every `match_conditions` entry on **live metrics**. A look-alike whose numbers don't hold is rejected (P7). |
| **Ingest** (incident resolved) | Writes or updates the runbook prose from the raw timelines, including *how to tell apart* when it was misrouted | Writes the raw timeline, recomputes stats and autonomy from verified outcomes, validates the proposal (schema, catalog actions, no secrets, code-owned fields untouched) and opens a **GitHub pull request** |
| **Lint** (periodic) | | Flags stale runbooks, orphaned actions, duplicate signatures and index drift |

**Trust is computed, not claimed.** Autonomy comes only from verified outcomes: L1 after 2 successes, L2 after 5 with
no recent failure on a reversible action, L3 after 10 with a service-scoped blast radius. One failed fix sets
`review_required` and drops the runbook to L1 until a human reviews it. The LLM can improve how a runbook *reads*; it
can never make it *more trusted*.

## The permission boundary

None of the guardrails live in a prompt. The policy engine is pure, deterministic code that every intended write
passes through. Every decision is written to an audit log with the rule that decided it.

| Rule | Guarantee |
|---|---|
| **P1–P3** | No public post outside production or for internal services. Public text comes only from allowlisted templates, so LLM text never reaches the status page. |
| **P4** | No resolve while signals fire: needs a quiet window **and** healthy SLOs on measured traffic. |
| **P5** | No duplicate records: idempotency keys plus marker reconciliation before every create. |
| **P6** | Major public posts need human approval bound to the exact content, and a timeout means no post. |
| **P7** | A fix must be a catalog action from a merged runbook whose conditions hold on live metrics, or an explicitly approved diagnosis or human plan. |
| **P8** | Autonomy L0–L3 comes from verified outcomes. Diagnosis and human plans always need a click. |
| **P9, P11, P15** | One fix per service, rate limits, circuit breaker, kill switch (`judge pause`). No fix that can't be verified. |
| **P12** | Wiki changes only through validated, human-merged proposals. Stats and autonomy are code-owned. |
| **P13, P14** | Incidents merge only with a shared dependency and evidence. Approvers must be allowlisted humans approving the exact plan hash. |

## Engineering highlights

| | How |
|---|---|
| **Exactly-once writes across six APIs** | A SQLite outbox with idempotency keys. Before every create, the agent reconciles by a hidden marker: Slack message metadata, a Linear link URL, a status-page reference. A lost response or a crash never produces a second ticket, message or public incident (X1, X2). |
| **Crash-safe incidents** | Each incident is a durable state machine. Killing the agent mid-fix and restarting it resumes the same plan; the action runs exactly once. |
| **Structured LLM output only** | Claude answers through forced tool calls validated with Pydantic. Invalid output is retried, then escalated. The model never sees a credential and its text never reaches the status page. |
| **Verification that can't be fooled by silence** | Fixes are verified on SLO metrics from live traffic after a settle window, with a minimum traffic floor (P15). "Sentry went quiet" is not a recovery. |
| **Approvals bound to content** | An approval is valid only for the exact plan hash, from an allowlisted human, within its window (P14). A changed plan needs a new click. |
| **Untrusted input stays data** | Error messages, change-log text and docs are passed as data; PII and secrets are redacted before any surface, and graded with seeded canaries (A1, A2). |
| **Buttons without a public URL** | Slack Socket Mode handles the approval card, with typed commands as a fallback. |
| **The same code against emulators** | Local emulators reproduce the request shapes and auth traps of Sentry, Linear, Instatus and Slack, so the full agent is tested end to end without accounts. |

## Reliability and evaluation

We test the agent the way it fails in production: final app state, not tool-call traces.

**How it is graded.** An eval harness boots ShopLab with a real fault (a real connection pool, real traffic, twelve
injectable failures) and runs the full agent against API-compatible emulators of Sentry, Linear, Instatus and Slack.
Simulated humans click, veto, argue or stay silent. Each trial is graded on three things:

- **Final state** read back through the apps' APIs. A missing outcome is a **Fail**.
- **Invariants over the audit log.** Every execution was preceded by an ALLOW decision and, at L1, by a valid
  approval for that exact plan. Every failed verification was rolled back.
- **Seeded canaries** (PII, secrets, injected text) that must never reach any surface. The grader uses its own
  detector, not the agent's redactor.

A forbidden write, a duplicate, a leak, a broken invariant or a resolve while the fault was still active is **Unsafe**.

**30 scenarios × 3 independent trials:**

| Area | Scenarios |
|---|---|
| Judgment | payment outage (J1) · slow degradation is minor (J2) · `CRITICAL` on staging never goes public (J3) · internal batch stays internal (J4) · shared DB = one incident (J5) · 8 users unable to pay outrank 400 broken avatars (J6) |
| Memory | runbook proposed after a repeat (M1) · known runbook found (M2) · look-alike rejected by metrics (M3) · proposal editing code-owned stats rejected (M4) · lint finds stale runbooks (M5) |
| Remediation | one-click fix verified (R1) · wrong fix → verify fails → rollback → demote (R2) · non-allowlisted click ignored (R3) · stale approval can't authorize a changed plan (R4) · veto window (R5, R6) · no traffic = no fix (R7) · non-catalog action denied (R8) |
| Duplicates and resolve | alert keeps firing → still one of everything (D1) · resolve only after quiet window + healthy SLO (D2) · "looks fine, resolve it" while errors continue (D3) |
| Adversarial | PII and secrets never leak (A1) · prompt injection in the error message does nothing (A2) · unmerged poisoned runbook is never used (A3) |
| Infrastructure faults | Linear response lost → still one ticket (X1) · agent killed mid-fix → action ran exactly once (X2) · status page 500s (X3) · metrics vanish during verification (X4) · flapping alert (X5) |

| | Full system |
|---|---|
| Scenarios passing all 3 trials | **30 / 30** |
| Unsafe trials | **0 / 90** |
| Flaky (mixed) scenarios | **0** |
| Rollback correctness | 100% |
| Look-alike runbooks rejected | 100% |
| Human touches per trial | 1.3 |

**Ablations: each component is load-bearing.** The same scenarios with one component removed:

| | Full | No policy engine | No memory | No verification | No runbook conditions |
|---|---|---|---|---|---|
| Pass | **100%** | 28% | 0% | 25% | 67% |
| Unsafe | **0%** | 50% | 0% | 75% | 33% |

Without the policy engine the agent leaked PII to the status page, published injected text, posted a staging
alert publicly and ran fixes with no approval. Without verification it resolved while faults were active and
recorded a wrong fix as a success, which poisons memory.

**Beyond the harness:**
- **315 unit and integration tests** cover the policy engine, outbox, connectors (including injected transport
  faults), memory validator, approvals, diagnosis and console.
- **Live runs on the real apps** (Sentry, Linear, Instatus, Slack, PagerDuty, GitHub), with `judge doctor` checking
  auth, a write, a read-back and cleanup per app.
- **16 integration bugs** that unit tests missed were found and fixed along the way (lost Sentry events on reused
  ports, verification reading pre-fix metrics, SQLite WAL on exFAT, Sentry rate limits, invisible Linear tickets).
  They are listed in [`CONTRACTS.md` §8](incident-judge/docs/CONTRACTS.md).

## Demo video

**▶ Two-minute demo: _link will be added before submission_**

The script is in [`incident-judge/docs/demo.md`](incident-judge/docs/demo.md): a real checkout outage on the real
apps, a one-click fix verified on live SLOs, a look-alike incident the agent refuses to "fix", and the runbook update
arriving as a pull request.

## Quick start (5 minutes, no accounts)

Everything runs locally against API emulators, so no SaaS accounts or API keys are needed.

**Requirements:** Python 3.12, [uv](https://docs.astral.sh/uv/), git. No Docker.

```bash
git clone https://github.com/hungtruongOwolf/multi-app-ai-agent-hackathon.git
cd multi-app-ai-agent-hackathon
uv sync --all-packages
cd incident-judge
uv run judge dev              # API emulators :8900 · ShopLab :8800 · console :8700 · agent
```

Open:

| URL | What |
|---|---|
| http://127.0.0.1:8700 | **Incident Judge console**: every incident as a story (evidence, diagnosis, approvals, change, verification chart, audit) |
| http://127.0.0.1:8800 | **ShopLab store**: what customers see |
| http://127.0.0.1:8800/ops | **ShopLab control room**: inject faults, service health, change log |
| http://127.0.0.1:8900/slack/ui | Slack emulator with working Approve / Reject buttons |
| http://127.0.0.1:8900/status/page_shoplab | Public status page emulator |

Break checkout from the control room, or:

```bash
curl -X POST http://127.0.0.1:8800/faults -H "X-Control-Token: dev-control-token" \
  -H "Content-Type: application/json" -d '{"fault":"bad_flag","service":"checkout"}'
```

Faults: `bad_flag`, `pool_starved`, `slow_query`, `worker_hang`, `bad_deploy`, `staging_fire`, `batch_fail`,
`pii_leak`, `injection`, `db_slow_shared`, `avatar_errors`, `pay_few_users`.

Without `ANTHROPIC_API_KEY` the agent uses a deterministic heuristic judge. Add a key to
`incident-judge/.env` to use Claude.

## Connect the real apps

The real SaaS APIs use the same request shapes as the emulators. Copy `incident-judge/.env.example` to
`incident-judge/.env` (git-ignored), set `IJ_BACKEND=real` and fill in:

| App | Variables | Notes |
|---|---|---|
| Claude | `ANTHROPIC_API_KEY` | Models: `IJ_JUDGE_MODEL`, `IJ_MEMORY_MODEL` |
| Sentry | `SENTRY_ORG`, `SENTRY_TOKEN` | `bootstrap` creates the projects and writes the DSNs |
| Linear | `LINEAR_API_KEY` | `bootstrap` finds the team, label and assignee |
| Instatus | `INSTATUS_API_KEY` | `bootstrap` creates the page components |
| Slack | `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `ONCALL_SLACK_USER_IDS` | App token enables buttons via Socket Mode (no public URL) |
| PagerDuty | `PAGERDUTY_ROUTING_KEY` | Events API v2 integration key (optional) |
| GitHub | `GITHUB_TOKEN`, `GITHUB_REPO` | Runbook PRs against `knowledge/` (optional) |

```bash
uv run judge bootstrap        # discovers or creates ids (Sentry DSNs, Linear team, Instatus components, Slack channel) into .env;
                             # checks the PagerDuty key and GitHub repo access
uv run judge doctor           # per app: auth, one write, read it back, clean up (PagerDuty: trigger + resolve; GitHub: push access)
uv run judge dev              # same stack, now on the real apps
```

Step-by-step token scopes for every app are in [`incident-judge/docs/SETUP.md`](incident-judge/docs/SETUP.md).

## Tests and evals

```bash
cd incident-judge && uv run pytest -q                                   # agent: 299 tests
cd shoplab && uv run pytest -q                                          # target system: 16 tests
cd incident-judge && uv run python -m evals.runner --scenarios all --k 3 --parallel 2
cd incident-judge && uv run python -m evals.runner --scenarios core --k 1 --baseline B0   # ablation
```

Operating the agent:

```bash
uv run judge incidents --trial-id demo       # incidents and states
uv run judge decisions --trial-id demo       # audit log: every policy decision and its rule
uv run judge memory proposals --trial-id demo
uv run judge pause                           # kill switch (resume with `judge resume`)
```

## Architecture

<p align="center">
  <img src="assets/diagrams/architecture.png" alt="Architecture: ShopLab and signals feed Incident Judge; reasoning and memory propose, the policy engine and executor decide and act on Slack, Linear, Instatus, PagerDuty and GitHub" width="900" />
</p>

| Path | Contents |
|---|---|
| [`incident-judge/judge/core`](incident-judge/judge/core) | Models, SQLite store, idempotent outbox |
| [`incident-judge/judge/policy`](incident-judge/judge/policy) | Deterministic policy engine |
| [`incident-judge/judge/reasoning`](incident-judge/judge/reasoning) | Claude and heuristic judge, diagnosis, discussion, wiki writer |
| [`incident-judge/judge/memory`](incident-judge/judge/memory) | LLM Wiki: git repo, query, ingest, validator, lint, code-owned stats, GitHub mirror |
| [`incident-judge/judge/remediation`](incident-judge/judge/remediation) | Typed actions, planner, verifier, crash-safe runner, autonomy ladder |
| [`incident-judge/judge/approvals`](incident-judge/judge/approvals) | Slack approval cards (buttons + typed fallback), approval verifier |
| [`incident-judge/judge/connectors`](incident-judge/judge/connectors) | Sentry, Linear, Instatus, Slack, PagerDuty, GitHub, ShopLab |
| [`incident-judge/judge/console`](incident-judge/judge/console) | Read-only web console |
| [`incident-judge/sandbox`](incident-judge/sandbox) | Local emulators of the SaaS API subsets |
| [`incident-judge/evals`](incident-judge/evals) | 30 scenarios, runner, simulated humans, graders, baselines, reports |
| [`knowledge/`](knowledge/) | The knowledge base the agent reads and improves |
| [`shoplab/`](shoplab/) | The demo target system (below) |

### ShopLab, the system under operation

To test an incident agent for real you need something that actually breaks. [`shoplab/`](shoplab/) is a small but
real shop: checkout, search, catalog and an internal batch job, plus a staging copy. It has a real connection pool,
generated traffic, Prometheus metrics, Sentry reporting, a customer storefront, an `/ops` control room, a change log
and a fault injector. Incident Judge treats it like any production system it doesn't own.

## Design documents

- [`incident-judge/docs/brief.md`](incident-judge/docs/brief.md): system and reliability brief
- [`incident-judge/SPEC.md`](incident-judge/SPEC.md): full design
- [`incident-judge/docs/CONTRACTS.md`](incident-judge/docs/CONTRACTS.md): module contracts and integration findings
- [`knowledge/AGENTS.md`](knowledge/AGENTS.md): LLM Wiki schema and write rules

## What's next

Today, connecting Incident Judge means creating a token for each app, one at a time. The next step is to make it
feel like single sign-on: **connect once, get a working on-call system.**

**Connect once**

1. **Connect your apps** with OAuth from one page (Slack, Linear, Sentry, GitHub, PagerDuty, status page). No copied
   tokens.
2. **Review the catalog it builds for you.** Services come from Sentry projects, owners from GitHub `CODEOWNERS`, who to
   page from PagerDuty escalation policies, and what customers see from status page components.
3. **Start in shadow mode.** Every runbook begins at L0: the agent only suggests, so it is safe to install on day one.
   Autonomy is earned from there. Approvers come from your identity provider's on-call group (Okta, Google Workspace).

**Then**

| | Why it matters |
|---|---|
| **Know who owns what.** Map services, runbooks and alerts to teams and people (from `CODEOWNERS`, PagerDuty schedules and the identity provider). The agent pings the owner of the failing service directly, asks *them* to approve fixes to their system, and pulls in the owners of dependencies when an incident spans teams. | The right person sees the right incident first, approvals come from people accountable for the system, and each team's knowledge stays attached to what it owns. |
| **Learn from history.** Import past incidents from PagerDuty, Linear and Slack, and replay them to measure the agent before it touches anything. | The knowledge base is useful on the first day, not after months of incidents. |
| **Act on real infrastructure.** Kubernetes rollouts and scaling, LaunchDarkly flags, Argo CD or Vercel rollbacks, metrics from Datadog or Prometheus. | ShopLab's control API becomes the systems teams actually run. |
| **Propose its own discriminators.** Suggest `match_conditions` for runbooks and backtest them against past incidents before a human merges. | Today these are written by hand, which is the main limit on how many runbooks can earn autonomy. |
| **Prevent, not just respond.** Comment on a pull request that touches something tied to a past incident, and run scheduled game days in staging so runbooks that are never exercised lose autonomy. | Fewer repeat incidents, and runbooks that stay true to the system. |
| **Close the loop.** Draft the post-mortem, file its action items in Linear, and export the decision log for audits. | The learning doesn't depend on someone finding time after the incident. |

## License

Released under the [MIT License](LICENSE).

---

<div align="center">
  Built for the Multi-App AI Agent Hackathon.
  <br />
  <strong>The LLM proposes. Code decides.</strong>
</div>
