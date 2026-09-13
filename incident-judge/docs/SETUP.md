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
ONCALL_SLACK_USER_IDS=U0123ABCD   # who may approve fixes / public posts
INSTATUS_SHOULD_PUBLISH=false     # true only when recording the demo
```

Then:

```bash
uv run judge bootstrap     # creates/discovers: Sentry projects + DSNs, Linear team + ij-eval label,
                           # Instatus page + components, Slack #incident-judge-oncall; writes ids to .env
uv run judge doctor        # per app: auth → one write → read back → clean up
uv run judge dev           # ShopLab sends real errors to Sentry; agent acts on Linear / Instatus / Slack
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

## 4. Slack (war room + approvals)

1. Create a workspace you own (company workspaces may require app approval).
2. https://api.slack.com/apps → **Create New App** → From scratch.
3. OAuth & Permissions → **Bot Token Scopes**: `chat:write`, `channels:read`, `channels:history`,
   `channels:manage`, `channels:join`, `users:read`. Install to workspace → copy the **Bot User OAuth Token**
   (`xoxb-…`) → `SLACK_BOT_TOKEN`.
4. Your member ID (profile → ⋯ → Copy member ID) → `ONCALL_SLACK_USER_IDS` (comma-separated for several people).
5. `judge bootstrap` creates/joins `#incident-judge-oncall` and writes `SLACK_ONCALL_CHANNEL`.

Approvals are text replies in the incident thread: `approve <hash8>` / `reject <hash8>` — no public URL needed.

## 5. Claude

`ANTHROPIC_API_KEY` in `.env`. Models: `IJ_JUDGE_MODEL=claude-sonnet-5` (triage), `IJ_MEMORY_MODEL=claude-opus-5`
(runbook chooser/writer). `judge doctor` pings both.

## What stays local

- The runbook wiki is a local git repository (no remote). A GitHub backend is intentionally not enabled.
- The eval harness grades against the sandbox (it needs admin read access to every app's state); real-app runs are for
  the live demo and `judge doctor`.
