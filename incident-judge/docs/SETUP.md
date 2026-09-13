# Connecting the real apps

Everything runs locally against the sandbox by default. This guide switches the agent to the real SaaS apps.
Budget ~30 minutes. All secrets go into `.env` (git-ignored); nothing is committed.

```
.env
IJ_BACKEND=real
ANTHROPIC_API_KEY=...        # already set
SENTRY_ORG=...  SENTRY_TOKEN=...
LINEAR_API_KEY=...
INSTATUS_API_KEY=...         # INSTATUS_PAGE_ID is discovered by bootstrap
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...          # Socket Mode: approval buttons without a public URL
ONCALL_SLACK_USER_IDS=U0123ABCD   # who may approve fixes / public posts
INSTATUS_SHOULD_PUBLISH=false     # true only when recording the demo
PAGERDUTY_ROUTING_KEY=...         # optional: escalation
GITHUB_TOKEN=...  GITHUB_REPO=owner/repo   # optional: runbook updates as pull requests
```

Then:

```bash
uv run judge bootstrap     # creates/discovers: Sentry projects + DSNs, Linear team + ij-eval label,
                           # Instatus page + components, Slack #incident-judge-oncall; writes ids to .env
uv run judge doctor        # per app: auth → one write → read back → clean up
uv run judge dev           # ShopLab sends real errors to Sentry; agent acts on Linear / Instatus / Slack / PagerDuty / GitHub
```

---

## 1. Sentry (errors)

1. Sign up (Developer plan is enough) and note your **organization slug** (Settings → Organization) → `SENTRY_ORG`.
2. Create a **Personal Token** (User settings → Personal Tokens), scopes: `org:read`, `project:read`, `project:write`,
   `event:read`, `event:write`, `team:read` → `SENTRY_TOKEN`.
   Use a personal token, not an org auth token (org tokens cannot read issues).
3. `judge bootstrap` creates projects `shoplab-prod` and `shoplab-staging` if missing (two projects because Sentry
   groups one error across environments into a single issue) and writes their DSNs.

Trap: ingestion has no SLA. `judge doctor` polls for its test event for up to 90 s.

## 2. Linear (internal record)

1. Settings → Security & access → **Personal API keys** → create → `LINEAR_API_KEY`.
   The header is `Authorization: <key>` with **no** `Bearer` (the connector already does this).
2. `judge bootstrap` picks your first team (or `--linear-team-key ENG`) and creates the `ij-eval` label.

Trap: the free plan caps active issues (250). Eval trials label their issues `ij-eval`; delete them after runs.

## 3. Instatus (public status page)

1. Create a status page in the dashboard (free plan).
2. User settings → **API key** → `INSTATUS_API_KEY`.
3. `judge bootstrap` finds the page and creates components named after the catalog capabilities
   ("Checkout & payments", "Search", "Product pages"), then writes `INSTATUS_PAGE_ID` and `INSTATUS_COMPONENTS`.

Trap: Instatus publishes incidents immediately. Keep `INSTATUS_SHOULD_PUBLISH=false` while developing.
UNKNOWN: whether the incidents API is available on the free plan — `judge doctor` will tell you.

## 4. Slack (incident thread + approvals)

1. Create a workspace you own (company workspaces may require app approval).
2. https://api.slack.com/apps → **Create New App** → From scratch.
3. OAuth & Permissions → **Bot Token Scopes**: `chat:write`, `channels:read`, `channels:history`,
   `channels:manage`, `channels:join`, `users:read`. Install to workspace → copy the **Bot User OAuth Token**
   (`xoxb-…`) → `SLACK_BOT_TOKEN`.
4. Your member ID (profile → ⋯ → Copy member ID) → `ONCALL_SLACK_USER_IDS` (comma-separated for several people).
5. **Socket Mode** (Settings → Socket Mode → enable) → create an app-level token with `connections:write` →
   `SLACK_APP_TOKEN`. Turn on Interactivity & Shortcuts. The approval buttons then work with no public URL.
6. `judge bootstrap` creates/joins `#incident-judge-oncall`, invites the on-call users and writes `SLACK_ONCALL_CHANNEL`.
7. Optional: Basic Information → Display Information → upload `assets/brand/png/app-icon-512.png` as the app icon.

Everything for one incident happens in one thread of that channel. Approvals are buttons on one card per incident;
typed `approve <hash8>` / `reject <hash8>` replies still work as a fallback.

## 5. PagerDuty (escalation, optional)

1. PagerDuty → Services → your service → **Integrations** → Add **Events API V2** → copy the **Integration Key**
   → `PAGERDUTY_ROUTING_KEY`.
2. The service's escalation policy needs an on-call user, otherwise events are accepted but no incident is created.

The agent pages when: the fix or public-post approval times out, a fix fails SLO verification, or a SEV1/SEV2 has no
safe catalog fix. All pages for one incident share `dedup_key = ij-<incident id>` and are resolved when it resolves.
`judge bootstrap` checks the key's format; `judge doctor` sends a SEV4 test event and resolves it immediately.
The console shows who was paged and why on each incident, and PagerDuty in the integrations list.

## 6. GitHub (knowledge base mirror, optional)

`GITHUB_TOKEN` (a token with `repo` scope, e.g. `gh auth token`, or a fine-grained token limited to this repository with
Contents and Pull requests: read and write) and `GITHUB_REPO=<owner>/<repo>` (the monorepo). `judge bootstrap` and
`judge doctor` check that the token can read the repository and push.
Runbook updates become pull requests on `incident-judge/<proposal>` branches touching `knowledge/`; merging on GitHub
or clicking **Merge** in Slack both work (P12 validation runs first). Raw timelines and recomputed stats are committed
to `main`. The first start only records a baseline, so the reviewed `knowledge/` is never overwritten by a fresh
runtime copy. Run `git pull` locally to see what the agent learned.

## 7. Claude

`ANTHROPIC_API_KEY` in `.env`. Models: `IJ_JUDGE_MODEL=claude-sonnet-5` (triage), `IJ_MEMORY_MODEL=claude-opus-5`
(runbook chooser/writer). `judge doctor` pings both.

## What stays local

- The agent always reads the runbook wiki from its local git repository (fast, already validated); GitHub is the
  human-facing copy when `GITHUB_TOKEN` is set.
- The eval harness grades against the sandbox (it needs admin read access to every app's state); real-app runs are for
  the live demo and `judge doctor`.
