"""Prometheus metrics for a ShopLab service (own registry; exact names per CONTRACTS §2.3)."""

from __future__ import annotations

import os
import sys

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0, 1.5, 2.5, 5.0)


def rss_bytes() -> int:
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            class PMC(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            k32 = ctypes.windll.kernel32
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            fn = k32.K32GetProcessMemoryInfo
            fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
            if fn(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb):
                return int(pmc.WorkingSetSize)
            return 0
        with open("/proc/self/statm") as f:
            return int(f.read().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    except Exception:
        return 0


class ServiceMetrics:
    def __init__(self, service: str):
        self.service = service
        self.registry = r = CollectorRegistry(auto_describe=True)
        self.requests = Counter("http_requests", "HTTP requests", ["service", "route", "status"], registry=r)
        self.duration = Histogram("http_request_duration_seconds", "Request duration", ["service", "route"],
                                  buckets=BUCKETS, registry=r)
        self.pool_in_use = Gauge("db_pool_in_use", "Connections in use", ["service"], registry=r)
        self.pool_size = Gauge("db_pool_size", "Pool size", ["service"], registry=r)
        self.pool_wait = Histogram("db_pool_wait_seconds", "Pool acquire wait", ["service"], buckets=BUCKETS,
                                   registry=r)
        self.db_query = Histogram("db_query_duration_seconds", "DB query duration", ["service"], buckets=BUCKETS,
                                  registry=r)
        self.memory = Gauge("process_resident_memory_bytes", "Resident memory", ["service"], registry=r)
        self.memory.labels(service).set_function(rss_bytes)
        self.flag = Gauge("app_flag", "Feature flag state", ["service", "flag"], registry=r)
        self.version = Gauge("app_version_info", "Running version", ["service", "version"], registry=r)
        self.batch_jobs = Counter("batch_jobs", "Batch job runs", ["service", "job", "status"], registry=r)

    def observe_request(self, route: str, status: int, seconds: float) -> None:
        self.requests.labels(self.service, route, str(status)).inc()
        self.duration.labels(self.service, route).observe(seconds)

    def pool_changed(self, in_use: int, size: int) -> None:
        self.pool_in_use.labels(self.service).set(in_use)
        self.pool_size.labels(self.service).set(size)
