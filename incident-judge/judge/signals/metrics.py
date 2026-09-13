"""Metrics backend contract. Named metrics (see models.MetricName) are computed by a backend
from ShopLab's Prometheus exposition — either by scraping /metrics directly (DirectScrapeBackend,
no infra needed) or via a Prometheus server (PrometheusBackend)."""

from __future__ import annotations

from typing import Protocol

from judge.core.models import MetricCondition, MetricName


class MetricsBackend(Protocol):
    def available(self) -> bool:
        """True if recent scrapes succeeded (data is fresh enough to decide on)."""
        ...

    def value(self, metric: MetricName, service: str, window_s: int, route: str | None = None) -> float | None:
        """None = not measurable (no data / no traffic for ratio metrics)."""
        ...


def evaluate(backend: MetricsBackend, cond: MetricCondition) -> tuple[bool | None, float | None]:
    """Returns (holds, observed). holds=None when not measurable."""
    observed = backend.value(cond.metric, cond.service, cond.window_s, cond.route)
    if observed is None:
        return None, None
    return cond.holds(observed), observed
