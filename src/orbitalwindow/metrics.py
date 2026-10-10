# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Resource metrics: per-container CPU/RAM from cAdvisor (cgroups), GPU totals from NVML.

Both are read on demand and cached for a few seconds, so many open dashboards cost one request.
Either source may be missing (no cAdvisor yet, no NVIDIA GPU); callers then get empty results.
"""

import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx2

log = logging.getLogger(__name__)

CACHE_SECONDS = 5.0
# systemd driver: /system.slice/docker-<id>.scope; cgroupfs driver: /docker/<id>
CGROUP_ID_RE = re.compile(r"(?:docker-|/docker/)([0-9a-f]{64})(?:\.scope)?$")


@dataclass(frozen=True)
class Usage:
    cpu_cores: float  # current CPU use in cores (1.0 = one full core)
    mem_bytes: int


@dataclass(frozen=True)
class GpuStat:
    index: int
    name: str
    util_percent: int
    mem_used: int
    mem_total: int


class ContainerMetrics:
    """CPU and memory per container, read from cAdvisor's cgroup summary.

    cAdvisor runs without any Docker access, so it only knows cgroups
    (``/system.slice/docker-<id>.scope``). The caller supplies the IDs and names of the
    containers it cares about.
    """

    def __init__(self, base_url: str, client: httpx2.Client | None = None) -> None:
        self._client = client or httpx2.Client(base_url=base_url, timeout=4.0)
        self._lock = threading.Lock()
        self._by_id: dict[str, Usage] = {}
        self._at = 0.0
        self.available = True

    def _summary(self) -> dict[str, Usage]:
        now = time.monotonic()
        if now - self._at < CACHE_SECONDS:
            return self._by_id
        try:
            r = self._client.get("/api/v2.0/summary/", params={"type": "name", "recursive": "true"})
            r.raise_for_status()
            summary = r.json()
        except (httpx2.HTTPError, ValueError) as e:
            if self.available:
                log.warning("cAdvisor is not reachable: %s", e)
            self.available = False
            self._by_id, self._at = {}, now
            return self._by_id
        self.available = True
        by_id: dict[str, Usage] = {}
        for key, value in summary.items():
            match = CGROUP_ID_RE.search(key)
            latest = (value or {}).get("latest_usage")
            if match and latest is not None:
                by_id[match.group(1)] = Usage(
                    cpu_cores=latest.get("cpu", 0) / 1000.0,
                    mem_bytes=int(latest.get("memory", 0)),
                )
        self._by_id, self._at = by_id, now
        return by_id

    def usage(self, names_by_id: dict[str, str]) -> dict[str, Usage]:
        """Usage of the given containers (full ID → name), keyed by name."""
        with self._lock:
            by_id = self._summary()
        return {name: by_id[cid] for cid, name in names_by_id.items() if cid in by_id}


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
