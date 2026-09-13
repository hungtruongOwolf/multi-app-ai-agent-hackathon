import math

import pytest

from judge.signals.scrape_backend import DirectScrapeBackend, histogram_quantile, parse_exposition


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def expo(ok: float, err: float, buckets: dict[str, float], in_use=2, size=10, mem=50 * 1024 * 1024,
         route="/pay") -> str:
    lines = [
        "# TYPE http_requests_total counter",
        f'http_requests_total{{route="{route}",service="checkout",status="200"}} {ok}',
        f'http_requests_total{{route="{route}",service="checkout",status="500"}} {err}',
        "# TYPE http_request_duration_seconds histogram",
    ]
    for le, c in buckets.items():
        lines.append(f'http_request_duration_seconds_bucket{{le="{le}",route="{route}",service="checkout"}} {c}')
    total = buckets["+Inf"]
    lines += [
        f'http_request_duration_seconds_count{{route="{route}",service="checkout"}} {total}',
        f'http_request_duration_seconds_sum{{route="{route}",service="checkout"}} 1.0',
        "# TYPE db_pool_in_use gauge",
        f'db_pool_in_use{{service="checkout"}} {in_use}',
        "# TYPE db_pool_size gauge",
        f'db_pool_size{{service="checkout"}} {size}',
        "# TYPE process_resident_memory_bytes gauge",
        f'process_resident_memory_bytes{{service="checkout"}} {mem}',
    ]
    return "\n".join(lines) + "\n"


def test_parse_skips_created_and_keeps_labels():
    vals = parse_exposition(expo(10, 2, {"0.1": 5, "0.5": 10, "+Inf": 12}))
    assert vals[("http_requests_total", (("route", "/pay"), ("service", "checkout"), ("status", "500")))] == 2


def test_histogram_quantile_interpolates_like_prometheus():
    # 100 obs: 50 <= 0.1, 90 <= 0.5, 100 <= 1.0
    b = {0.1: 50, 0.5: 90, 1.0: 100, math.inf: 100}
    assert histogram_quantile(0.5, b) == pytest.approx(0.1)
    assert histogram_quantile(0.95, b) == pytest.approx(0.5 + 0.5 * (95 - 90) / 10)
    assert histogram_quantile(0.95, {0.1: 0, math.inf: 0}) is None
    # everything above the last finite bucket -> highest finite bound
    assert histogram_quantile(0.95, {0.1: 0, 1.0: 0, math.inf: 10}) == pytest.approx(1.0)


def test_error_rate_rps_latency_over_window():
    clock = Clock()
    be = DirectScrapeBackend({"checkout": "http://x"}, clock=clock, stale_after_s=10)
    be.ingest("checkout", expo(100, 0, {"0.1": 100, "0.5": 100, "+Inf": 100}))
    clock.t += 10
    be.ingest("checkout", expo(170, 30, {"0.1": 140, "0.5": 190, "+Inf": 200}))
    assert be.available()
    assert be.value("error_rate", "checkout", 10) == pytest.approx(30 / 100)
    assert be.value("rps", "checkout", 10) == pytest.approx(10.0)
    # window deltas: 40 <= 0.1, 90 <= 0.5, 100 total -> p95 in +Inf bucket -> 0.5
    assert be.value("latency_p95", "checkout", 10) == pytest.approx(0.5)
    assert be.value("pool_utilization", "checkout", 10) == pytest.approx(0.2)
    assert be.value("memory_mb", "checkout", 10) == pytest.approx(50.0)
    assert be.value("error_rate", "checkout", 10, route="/other") is None


def test_counter_reset_after_restart_is_not_negative():
    clock = Clock()
    be = DirectScrapeBackend({"checkout": "http://x"}, clock=clock)
    be.ingest("checkout", expo(1000, 100, {"0.1": 1100, "0.5": 1100, "+Inf": 1100}))
    clock.t += 5
    be.ingest("checkout", expo(1040, 110, {"0.1": 1150, "0.5": 1150, "+Inf": 1150}))
    clock.t += 5  # process restarted: counters start from zero
    be.ingest("checkout", expo(20, 0, {"0.1": 20, "0.5": 20, "+Inf": 20}))
    assert be.value("rps", "checkout", 10) == pytest.approx((50 + 20) / 10)
    assert be.value("error_rate", "checkout", 10) == pytest.approx(10 / 70)


def test_series_appearing_mid_window_counts_from_zero():
    clock = Clock()
    be = DirectScrapeBackend({"checkout": "http://x"}, clock=clock)
    first = expo(100, 0, {"0.1": 100, "0.5": 100, "+Inf": 100}).replace(
        'http_requests_total{route="/pay",service="checkout",status="500"} 0\n', "")
    be.ingest("checkout", first)
    clock.t += 10
    be.ingest("checkout", expo(150, 50, {"0.1": 200, "0.5": 200, "+Inf": 200}))
    assert be.value("error_rate", "checkout", 10) == pytest.approx(0.5)


def test_stale_and_insufficient_data_is_not_measurable():
    clock = Clock()
    be = DirectScrapeBackend({"checkout": "http://x"}, clock=clock, stale_after_s=10)
    assert not be.available()
    assert be.value("error_rate", "checkout", 30) is None
    be.ingest("checkout", expo(1, 0, {"0.1": 1, "0.5": 1, "+Inf": 1}))
    assert be.value("rps", "checkout", 30) is None  # one snapshot only
    clock.t += 5
    be.ingest("checkout", expo(1, 0, {"0.1": 1, "0.5": 1, "+Inf": 1}))
    assert be.value("error_rate", "checkout", 30) is None  # no traffic -> ratio undefined
    assert be.value("rps", "checkout", 30) == 0.0
    clock.t += 60
    assert not be.available()
    assert be.value("rps", "checkout", 30) is None


def test_window_uses_baseline_before_window_start():
    clock = Clock()
    be = DirectScrapeBackend({"checkout": "http://x"}, clock=clock)
    for i in range(10):  # 10 rps of successes, snapshots at t=1000..1090
        be.ingest("checkout", expo(100 * i, 0, {"0.1": 100 * i, "0.5": 100 * i, "+Inf": 100 * i}))
        clock.t += 10
    # t=1100: last 10s had 100 requests, 50 of them errors
    be.ingest("checkout", expo(950, 50, {"0.1": 1000, "0.5": 1000, "+Inf": 1000}))
    assert be.value("error_rate", "checkout", 10) == pytest.approx(0.5)
    assert be.value("error_rate", "checkout", 100) == pytest.approx(0.05)
    assert be.value("rps", "checkout", 100) == pytest.approx(10.0)
