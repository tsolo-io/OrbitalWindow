# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Resource metrics: per-container CPU/RAM from cAdvisor, GPU totals from NVML.

Both are read on demand and cached for a few seconds, so many open dashboards cost one request.
Either source may be missing (no cAdvisor yet, no NVIDIA GPU); callers then get empty results.
"""

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx2

log = logging.getLogger(__name__)

CACHE_SECONDS = 5.0
SPEC_CACHE_SECONDS = 60.0
NO_LIMIT = 1 << 62  # cAdvisor reports "no memory limit" as a huge number


@dataclass(frozen=True)
class Usage:
    cpu_cores: float  # current CPU use in cores (1.0 = one full core)
    mem_bytes: int
    mem_limit: int | None  # None = unlimited


@dataclass(frozen=True)
class GpuStat:
    index: int
    name: str
    util_percent: int
    mem_used: int
    mem_total: int


class ContainerMetrics:
    """CPU and memory of OrbitalWindow-managed containers, by container name."""

    def __init__(self, base_url: str, client: httpx2.Client | None = None) -> None:
        self._client = client or httpx2.Client(base_url=base_url, timeout=4.0)
        self._lock = threading.Lock()
        self._spec: dict[str, dict[str, Any]] = {}
        self._spec_at = 0.0
        self._usage: dict[str, Usage] = {}
        self._usage_at = 0.0
        self.available = True

    def _refresh_spec(self, now: float) -> None:
        if now - self._spec_at < SPEC_CACHE_SECONDS and self._spec:
            return
        r = self._client.get("/api/v2.0/spec", params={"type": "docker", "recursive": "true"})
        r.raise_for_status()
        self._spec = {
            key: spec
            for key, spec in r.json().items()
            if (spec.get("labels") or {}).get("ow.managed") == "true" and spec.get("aliases")
        }
        self._spec_at = now

    def usage(self) -> dict[str, Usage]:
        with self._lock:
            now = time.monotonic()
            if now - self._usage_at < CACHE_SECONDS:
                return self._usage
            try:
                self._refresh_spec(now)
                r = self._client.get(
                    "/api/v2.0/summary", params={"type": "docker", "recursive": "true"}
                )
                r.raise_for_status()
                summary = r.json()
            except (httpx2.HTTPError, ValueError) as e:
                if self.available:
                    log.warning("cAdvisor is not reachable: %s", e)
                self.available = False
                self._usage, self._usage_at = {}, now
                return self._usage
            self.available = True
            usage: dict[str, Usage] = {}
            for key, spec in self._spec.items():
                latest = (summary.get(key) or {}).get("latest_usage")
                if latest is None:
                    continue
                limit = (spec.get("memory") or {}).get("limit")
                usage[spec["aliases"][0]] = Usage(
                    cpu_cores=latest.get("cpu", 0) / 1000.0,
                    mem_bytes=int(latest.get("memory", 0)),
                    mem_limit=int(limit) if limit and limit < NO_LIMIT else None,
                )
            # New containers appear in the summary before the cached spec knows them.
            if any(k not in self._spec for k in summary):
                self._spec_at = 0.0
            self._usage, self._usage_at = usage, now
            return usage


class GpuMetrics:
    """Whole-GPU utilisation and memory via NVML. Empty when there is no NVIDIA driver."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._nvml: Any = None
        self._ready = False
        self._failed = False
        self._cache: list[GpuStat] = []
        self._at = 0.0

    def _init(self) -> bool:
        if self._ready:
            return True
        if self._failed:
            return False
        try:
            import pynvml

            pynvml.nvmlInit()
        except Exception as e:  # library missing, no driver, no device
            log.info("NVML not available (GPU numbers hidden): %s", e)
            self._failed = True
            return False
        self._nvml, self._ready = pynvml, True
        return True

    def stats(self) -> list[GpuStat]:
        with self._lock:
            now = time.monotonic()
            if now - self._at < CACHE_SECONDS or not self._init():
                return self._cache
            nv = self._nvml
            result = []
            try:
                for i in range(nv.nvmlDeviceGetCount()):
                    h = nv.nvmlDeviceGetHandleByIndex(i)
                    name = nv.nvmlDeviceGetName(h)
                    mem = nv.nvmlDeviceGetMemoryInfo(h)
                    util = nv.nvmlDeviceGetUtilizationRates(h)
                    result.append(
                        GpuStat(
                            i,
                            name.decode() if isinstance(name, bytes) else str(name),
                            int(util.gpu),
                            int(mem.used),
                            int(mem.total),
                        )
                    )
            except Exception as e:
                log.warning("NVML query failed: %s", e)
            self._cache, self._at = result, now
            return result


def fmt_bytes(n: int | None) -> str:
    if n is None:
        return "unlimited"
    gb = n / 1024**3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n / 1024**2:.0f} MB"
