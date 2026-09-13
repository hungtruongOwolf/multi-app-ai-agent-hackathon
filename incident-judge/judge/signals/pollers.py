"""Signal sources. Polling only (no inbound webhooks, no public URL)."""

from __future__ import annotations

import logging
from datetime import datetime

from judge.core.models import Env, Signal, now
from judge.core.store import Store
from judge.signals.fingerprint import slo_fingerprint
from judge.signals.metrics import MetricsBackend

log = logging.getLogger("judge.signals")


class SentryPoller:
    def __init__(self, sentry, store: Store, settings):
        self.sentry = sentry
        self.store = store
        self.settings = settings

    def _query(self) -> str:
        q = "is:unresolved lastSeen:-10m"
        if self.settings.trial_id:
            q += f" ij_trial:{self.settings.trial_id}"
        return q

    async def poll(self) -> list[Signal]:
        out: list[Signal] = []
        for env in (Env.production, Env.staging):
            try:
                issues = await self.sentry.list_issues(environment=env.value, query=self._query())
            except Exception as e:
                log.warning("sentry poll failed for %s: %r", env, e)
                continue
            for issue in issues:
                cache_key = f"sentry_event:{issue['id']}"
                event = self.store.get_kv(cache_key)
                if event is None:
                    try:
                        event = await self.sentry.latest_event(issue["id"])
                    except Exception as e:
                        log.warning("latest_event failed for %s: %r", issue["id"], e)
                        continue
                    self.store.put_kv(cache_key, event)
                self.store.put_kv(f"sentry_issue_meta:{issue['id']}", {
                    "title": issue.get("title"), "permalink": issue.get("permalink"), "shortId": issue.get("shortId")})
                sig = self.sentry.to_signal(issue, event, self.settings.trial_id)
                sig.signal_id = f"sentry:{issue['id']}"
                out.append(sig)
        return out

    async def last_seen(self, issue_id: str) -> datetime | None:
        try:
            issue = await self.sentry.get_issue(issue_id)
        except Exception as e:
            log.warning("get_issue failed: %r", e)
            return None
        ls = issue.get("lastSeen")
        return datetime.fromisoformat(ls.replace("Z", "+00:00")) if ls else None


class SloPoller:
    """Evaluates slos.yaml over the metrics backend. Emits a Signal per firing SLO."""

    def __init__(self, metrics: MetricsBackend, config, settings, store: Store):
        self.metrics = metrics
        self.config = config
        self.settings = settings
        self.store = store
        self._pending: dict[str, datetime] = {}

    def _window(self, w: int) -> int:
        return int(max(20, w * max(self.settings.time_scale, 0.3)))

    def firing(self) -> list[tuple[str, dict, float]]:
        res = []
        if not self.metrics.available():
            return res
        for name, slo in self.config.slos.items():
            svc = slo["service"]
            if slo["metric"] == "error_rate":
                win = self._window(slo["fast_burn"]["window_s"])
                v = self.metrics.value("error_rate", svc, win)
                rps = self.metrics.value("rps", svc, win)
                if v is None or not rps:
                    continue
                budget = 1 - slo["objective"]
                burn = v / budget if budget else 0
                if burn >= slo["fast_burn"]["factor"]:
                    res.append((name, slo, burn))
            elif slo["metric"] == "latency_p95":
                v = self.metrics.value("latency_p95", svc, self._window(slo["window_s"]))
                if v is not None and v >= slo["threshold"]:
                    res.append((name, slo, v / slo["threshold"]))
        return res

    async def poll(self) -> list[Signal]:
        """Like an alert rule with `for:` — a condition must hold for a sustained period before it is a signal.
        Avoids paging on one noisy window (e.g. a CPU hiccup on a loaded host)."""
        out = []
        t = now()
        hold_s = max(10.0, 60 * self.settings.time_scale)
        firing = self.firing()
        firing_now = {name for name, _, _ in firing}
        for name in list(self._pending):
            if name not in firing_now:
                self._pending.pop(name)
        for name, slo, burn in firing:
            # a fact for the resolve check (P4) even before the hold period makes it a new signal
            self.store.put_kv(f"slo_last_firing:{slo['service']}", t.isoformat())
            since = self._pending.setdefault(name, t)
            if (t - since).total_seconds() < hold_s:
                continue
            out.append(Signal(
                signal_id=f"slo:{name}", source="slo",
                fingerprint=slo_fingerprint(name, "production", slo["service"]),
                service=slo["service"], environment=Env.production, error_type="SLOBurn", culprit=name,
                slo_name=name, burn_rate=burn, last_seen=t, first_seen=self._pending.get(name, t),
                trial_id=self.settings.trial_id,
            ))
        return out

    def last_firing(self, service: str) -> datetime | None:
        v = self.store.get_kv(f"slo_last_firing:{service}")
        return datetime.fromisoformat(v) if v else None
