# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Resource metrics: per-container CPU/RAM from cAdvisor (cgroups), GPU totals from NVML, from
the Intel GPU exporter (``deploy/scripts/intel-gpu-stats.py``) or from the amdgpu driver's sysfs
files.

Both are read on demand and cached for a few seconds, so many open dashboards cost one request.
Either source may be missing (no cAdvisor yet, no GPU numbers); callers then get empty results.
"""

import logging
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx2

log = logging.getLogger(__name__)

CACHE_SECONDS = 5.0
# systemd driver: /system.slice/docker-<id>.scope; cgroupfs driver: /docker/<id>
CGROUP_ID_RE = re.compile(r"(?:docker-|/docker/)([0-9a-f]{64})(?:\.scope)?$")

# AMD GPUs (ADR 0048). sysfs is read-only in a container but shows the host's devices.
AMD_VENDOR_ID = "0x1002"
DRM_DIR = Path("/sys/class/drm")
KFD_NODES_DIR = Path("/sys/class/kfd/kfd/topology/nodes")
# Product names by PCI device and revision, from libdrm (Debian package libdrm-common).
AMDGPU_IDS = Path("/usr/share/libdrm/amdgpu.ids")
CARD_RE = re.compile(r"^card(\d+)$")


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
    temperature: int | None = None  # degrees Celsius
    slowdown: int | None = None  # the GPU slows itself down from this temperature


class ContainerMetrics:
    """CPU and memory per container, read from cAdvisor's cgroup summary.

    cAdvisor runs without any Docker access, so it only knows cgroups
    (``/system.slice/docker-<id>.scope``). The caller supplies the IDs and names of the
    containers it cares about.
    """

    def __init__(self, base_url: str, client: httpx2.Client | None = None) -> None:
        """Create the cAdvisor reader.

        Args:
            base_url: cAdvisor's base URL.
            client: HTTP client to use instead of a new one (e.g. in tests).
        """
        self._client = client or httpx2.Client(base_url=base_url, timeout=4.0)
        self._lock = threading.Lock()
        self._by_id: dict[str, Usage] = {}
        self._at = 0.0
        self.available = True

    def _summary(self) -> dict[str, Usage]:
        """Return the usage of every Docker container cgroup, cached for ``CACHE_SECONDS``.

        Returns:
            Usage by full container ID; empty when cAdvisor is not reachable.
        """
        now = time.monotonic()
        if now - self._at < CACHE_SECONDS:
            return self._by_id
        try:
            r = self._client.get("/api/v2.0/summary/", params={"type": "name", "recursive": "true"})
            r.raise_for_status()
            summary = r.json()
        except (httpx2.HTTPError, ValueError) as exc:
            if self.available:
                log.warning("cAdvisor is not reachable: %s", exc)
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
        """Return the usage of the given containers.

        Args:
            names_by_id: Container names by full container ID.

        Returns:
            Usage keyed by container name; containers cAdvisor does not report are left out.
        """
        with self._lock:
            by_id = self._summary()
        return {name: by_id[cid] for cid, name in names_by_id.items() if cid in by_id}


class AmdGpus:
    """Load, memory and temperature of AMD GPUs, read from the amdgpu driver's sysfs files.

    Every file used is readable by any user, so the non-root service reads them itself: no
    exporter and no capability (ADR 0048). Graphics built into the processor are left out when
    there is a discrete GPU. A GPU's index is its HIP device number (its place among the KFD GPU
    nodes), the number the AI session puts in ``HIP_VISIBLE_DEVICES``.
    """

    def __init__(
        self,
        drm_dir: Path = DRM_DIR,
        kfd_dir: Path = KFD_NODES_DIR,
        ids_file: Path = AMDGPU_IDS,
    ) -> None:
        """Create the reader.

        Args:
            drm_dir: The DRM class folder in sysfs (tests pass a fake one).
            kfd_dir: The KFD topology nodes folder in sysfs.
            ids_file: libdrm's table of AMD product names.
        """
        self._drm_dir = drm_dir
        self._kfd_dir = kfd_dir
        self._ids_file = ids_file
        self._names: dict[tuple[str, str], str] | None = None

    def stats(self) -> list[GpuStat]:
        """Return the numbers of every AMD GPU.

        Returns:
            One entry per GPU, ordered by index; empty without AMD GPUs.
        """
        hip = self._hip_indexes()
        cards = []  # (integrated, stat)
        for position, (pci, device) in enumerate(self._devices()):
            stat = self._stat(device, hip.get(pci, position))
            if stat is not None:
                cards.append((_is_integrated(device, stat), stat))
        if any(not integrated for integrated, _ in cards):
            cards = [card for card in cards if not card[0]]
        return sorted((stat for _, stat in cards), key=lambda gpu: gpu.index)

    def _devices(self) -> list[tuple[str, Path]]:
        """Return the AMD cards, ordered by PCI address.

        Returns:
            (PCI address, the card's ``device`` folder) per AMD GPU.
        """
        try:
            entries = list(self._drm_dir.iterdir())
        except OSError:
            return []
        devices = []
        for entry in entries:
            device = entry / "device"
            if CARD_RE.match(entry.name) and _read(device / "vendor") == AMD_VENDOR_ID:
                devices.append((device.resolve().name, device))
        return sorted(devices)

    def _hip_indexes(self) -> dict[str, int]:
        """Number the GPUs the way ROCm does: in the order of the KFD topology's GPU nodes.

        Returns:
            HIP device number by PCI address; empty if the topology can't be read.
        """
        try:
            nodes = sorted(
                (node for node in self._kfd_dir.iterdir() if node.name.isdigit()),
                key=lambda node: int(node.name),
            )
        except OSError:
            return {}
        indexes: dict[str, int] = {}
        for node in nodes:
            props = _properties(node / "properties")
            if not props.get("simd_count"):  # a CPU node
                continue
            location = props.get("location_id", 0)
            pci = (
                f"{props.get('domain', 0):04x}:{location >> 8:02x}:"
                f"{(location >> 3) & 0x1F:02x}.{location & 0x7}"
            )
            indexes[pci] = len(indexes)
        return indexes

    def _stat(self, device: Path, index: int) -> GpuStat | None:
        """Read one card's numbers.

        Args:
            device: The card's ``device`` folder.
            index: The number to give the GPU.

        Returns:
            The numbers, or None if the driver does not give the card's load and memory.
        """
        busy = _read_int(device / "gpu_busy_percent")
        used = _read_int(device / "mem_info_vram_used")
        total = _read_int(device / "mem_info_vram_total")
        if busy is None or used is None or total is None:
            return None
        hwmon = next(iter(sorted(device.glob("hwmon/hwmon*"))), None)
        temperature, critical = _edge_temperature(hwmon) if hwmon else (None, None)
        return GpuStat(index, self._name(device), busy, used, total, temperature, critical)

    def _name(self, device: Path) -> str:
        """Return a card's product name from libdrm's table.

        Args:
            device: The card's ``device`` folder.

        Returns:
            Its name, e.g. ``AMD Radeon RX 7900 XTX``; ``AMD GPU 0x744c`` if it isn't listed.
        """
        if self._names is None:
            self._names = _amdgpu_ids(self._ids_file)
        device_id = _read(device / "device") or "?"
        revision = _read(device / "revision") or "?"
        key = (device_id.removeprefix("0x").upper(), revision.removeprefix("0x").upper())
        return self._names.get(key) or f"AMD GPU {device_id}"


def _read(path: Path) -> str | None:
    """Read a one-line sysfs file.

    Args:
        path: The file.

    Returns:
        Its stripped text, or None if it can't be read.
    """
    try:
        return path.read_text().strip()
    except OSError:
        return None


def _read_int(path: Path) -> int | None:
    """Read a sysfs file that holds a number.

    Args:
        path: The file.

    Returns:
        The number, or None if it can't be read.
    """
    text = _read(path)
    try:
        return int(text) if text is not None else None
    except ValueError:
        return None


def _properties(path: Path) -> dict[str, int]:
    """Read a KFD topology ``properties`` file (``name value`` lines).

    Args:
        path: The file.

    Returns:
        The numeric properties by name; empty if the file can't be read.
    """
    props = {}
    for line in (_read(path) or "").splitlines():
        name, _, value = line.partition(" ")
        if value.strip().isdigit():
            props[name] = int(value)
    return props


def _edge_temperature(hwmon: Path) -> tuple[int | None, int | None]:
    """Read the GPU's edge temperature and its critical limit from the amdgpu hwmon.

    The edge sensor is the GPU temperature other tools show; amdgpu throttles at the limit.

    Args:
        hwmon: The card's hwmon folder.

    Returns:
        Degrees Celsius (temperature, critical limit), None where unknown.
    """
    sensor = "temp1"
    for label in sorted(hwmon.glob("temp*_label")):
        if _read(label) == "edge":
            sensor = label.name.removesuffix("_label")
            break
    temperature = _read_int(hwmon / f"{sensor}_input")  # millidegrees
    critical = _read_int(hwmon / f"{sensor}_crit")
    return (
        None if temperature is None else temperature // 1000,
        None if critical is None else critical // 1000,
    )


def _is_integrated(device: Path, stat: GpuStat) -> bool:
    """Tell whether a card is graphics built into the processor (an APU).

    amdgpu hides the VRAM vendor on APUs (there is no VRAM chip) and gives them no critical
    temperature. Either alone may also be missing on a discrete GPU, so both must be missing.

    Args:
        device: The card's ``device`` folder.
        stat: The card's numbers.

    Returns:
        True for built-in graphics.
    """
    return not (device / "mem_info_vram_vendor").exists() and stat.slowdown is None


def _amdgpu_ids(path: Path) -> dict[tuple[str, str], str]:
    """Read libdrm's ``amdgpu.ids``: ``DEVICE,\tREVISION,\tNAME`` lines.

    Args:
        path: The file.

    Returns:
        Product name by (device, revision), both upper-case hex without ``0x``; empty if the file
        is missing.
    """
    names = {}
    for line in (_read(path) or "").splitlines():
        if line.startswith("#"):
            continue
        parts = [part.strip() for part in line.split(",", 2)]
        if len(parts) == 3:
            names[(parts[0].upper(), parts[1].upper())] = parts[2]
    return names


class GpuMetrics:
    """Whole-GPU utilisation and memory.

    On AMD hosts from the amdgpu driver's sysfs files; else from the GPU exporter if there is one
    (Intel), else from NVML (NVIDIA). Empty when the source is not available (no GPU, no driver,
    or the exporter is down).
    """

    def __init__(
        self,
        url: str = "",
        client: httpx2.Client | None = None,
        vendor: Callable[[], str] | None = None,
        amd: AmdGpus | None = None,
    ) -> None:
        """Create the reader.

        Args:
            url: The Intel GPU exporter's URL; empty to use NVML (NVIDIA).
            client: HTTP client for the exporter (tests pass one with a mock transport).
            vendor: Returns the GPU vendor setting (it can change while the service runs);
                ``amd`` reads the amdgpu driver. None never reads it.
            amd: The AMD reader (tests pass one on a fake sysfs).
        """
        self._url = url
        self._client = client or (httpx2.Client(timeout=2.0) if url else None)
        self._vendor = vendor
        self._amd = amd or AmdGpus()
        self._lock = threading.Lock()
        self._nvml: Any = None
        self._ready = False
        self._failed = False
        self._cache: list[GpuStat] = []
        self._at = 0.0

    def _init(self) -> bool:
        """Initialise NVML once; remember a failure so it is not retried.

        Returns:
            True if NVML is ready to use.
        """
        if self._ready:
            return True
        if self._failed:
            return False
        try:
            import pynvml

            pynvml.nvmlInit()
        except Exception as exc:  # library missing, no driver, no device
            log.info("NVML not available (GPU numbers hidden): %s", exc)
            self._failed = True
            return False
        self._nvml, self._ready = pynvml, True
        return True

    def _temperatures(self, handle: Any) -> tuple[int | None, int | None]:
        """Read a GPU's temperature and slowdown threshold; not every GPU reports both.

        Args:
            handle: NVML device handle.

        Returns:
            Degrees Celsius (temperature, slowdown threshold), None where unknown.
        """
        nv = self._nvml
        values: list[int | None] = []
        for read in (
            lambda: nv.nvmlDeviceGetTemperature(handle, nv.NVML_TEMPERATURE_GPU),
            lambda: nv.nvmlDeviceGetTemperatureThreshold(
                handle, nv.NVML_TEMPERATURE_THRESHOLD_SLOWDOWN
            ),
        ):
            try:
                values.append(int(read()))
            except Exception:  # NVMLError_NotSupported and friends
                values.append(None)
        return values[0], values[1]

    def stats(self) -> list[GpuStat]:
        """Return utilisation and memory of every GPU, cached for ``CACHE_SECONDS``.

        Returns:
            One entry per GPU; empty when the GPU numbers are not available.
        """
        with self._lock:
            now = time.monotonic()
            if now - self._at < CACHE_SECONDS:
                return self._cache
            if self._vendor is not None and self._vendor() == "amd":
                try:
                    self._cache = self._amd.stats()
                except OSError as exc:  # a card went away while it was read
                    log.warning("Reading the AMD GPUs failed: %s", exc)
                    self._cache = []
                self._at = now
                return self._cache
            if self._url:
                self._cache, self._at = self._exporter_stats(), now
                return self._cache
            if not self._init():
                self._cache, self._at = [], now  # not the numbers of another vendor
                return self._cache
            nv = self._nvml
            result = []
            try:
                for index in range(nv.nvmlDeviceGetCount()):
                    handle = nv.nvmlDeviceGetHandleByIndex(index)
                    name = nv.nvmlDeviceGetName(handle)
                    mem = nv.nvmlDeviceGetMemoryInfo(handle)
                    util = nv.nvmlDeviceGetUtilizationRates(handle)
                    result.append(
                        GpuStat(
                            index,
                            name.decode() if isinstance(name, bytes) else str(name),
                            int(util.gpu),
                            int(mem.used),
                            int(mem.total),
                            *self._temperatures(handle),
                        )
                    )
            except Exception as exc:
                log.warning("NVML query failed: %s", exc)
            self._cache, self._at = result, now
            return result

    def _exporter_stats(self) -> list[GpuStat]:
        """Read the GPU exporter.

        Returns:
            One entry per GPU; empty when the exporter can't be reached or answers badly.
        """
        assert self._client is not None
        try:
            response = self._client.get(self._url)
            response.raise_for_status()
            return [GpuStat(**gpu) for gpu in response.json()]
        except (httpx2.HTTPError, ValueError, TypeError) as exc:
            log.warning("GPU exporter %s: %s", self._url, exc)
            return []


def fmt_bytes(n: int | None) -> str:
    """Format a byte count for display.

    Args:
        n: Number of bytes, or None for no limit.

    Returns:
        ``"1.5 GB"``, ``"200 MB"`` or ``"unlimited"``.
    """
    if n is None:
        return "unlimited"
    gb = n / 1024**3
    return f"{gb:.1f} GB" if gb >= 1 else f"{n / 1024**2:.0f} MB"
