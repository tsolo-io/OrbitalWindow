# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""Host status from Prometheus node-exporter (installed on the host, not started by us).

Read only while someone has the status page open: each page takes its own samples, so nothing is
cached or polled in the background. Rates (CPU, disk and network traffic) need two samples, so
they appear from the second refresh on.
"""

import re
from collections import Counter
from dataclasses import dataclass, field

import httpx2

LINE_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{(.*)\})?\s+(\S+)")
LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')
# Partitions, optical drives, RAM disks and device-mapper volumes would count the same I/O twice.
SKIP_DISK_RE = re.compile(
    r"^((sd|vd|xvd|hd)[a-z]+\d+|nvme\d+n\d+p\d+|mmcblk\d+p\d+|loop\d+|ram\d+|zram\d+|sr\d+|fd\d+"
    r"|dm-\d+)$"
)
SKIP_NET_RE = re.compile(r"^(lo|veth.*|br-.*|docker\d*|virbr.*|vnet.*|cali.*|flannel.*|cni.*)$")
SKIP_FSTYPES = frozenset({"tmpfs", "overlay", "squashfs", "nsfs", "ramfs", "vfat", "fuse.lxcfs"})
SKIP_MOUNT_PREFIXES = ("/boot", "/run", "/snap", "/var/snap", "/dev", "/sys")
# Plain names for hwmon chips (node_hwmon_chip_names ``chip_name`` without a trailing number).
CHIP_LABELS = {
    "coretemp": "CPU",
    "k10temp": "CPU",
    "zenpower": "CPU",
    "nvme": "NVMe disk",
    "drivetemp": "Disk",
    "acpitz": "Motherboard",
    "pch": "Chipset",
    "amdgpu": "AMD GPU",
    "i915": "Intel GPU",
    "xe": "Intel GPU",
    "iwlwifi": "Wi-Fi",
}
# Readings outside this range are unconnected or broken sensors (and bogus limits).
TEMP_RANGE = (0.0, 150.0)
TEMP_WARN_MARGIN = 10.0  # warm when the hottest sensor is this close to the critical limit

Labels = tuple[tuple[str, str], ...]
Metrics = dict[str, list[tuple[dict[str, str], float]]]


class NodeExporterError(Exception):
    """node-exporter could not be read."""


def parse(text: str) -> Metrics:
    """Parse the Prometheus text format.

    Args:
        text: The ``/metrics`` response.

    Returns:
        Samples by metric name: a list of (labels, value).
    """
    metrics: Metrics = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        match = LINE_RE.match(line)
        if not match:
            continue
        name, _, raw_labels, raw_value = match.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        labels = dict(LABEL_RE.findall(raw_labels or ""))
        metrics.setdefault(name, []).append((labels, value))
    return metrics


def _one(metrics: Metrics, name: str) -> float | None:
    """Return the value of a metric without labels.

    Args:
        metrics: Parsed samples.
        name: Metric name.

    Returns:
        The first sample's value, or None if the metric is missing.
    """
    samples = metrics.get(name)
    return samples[0][1] if samples else None


@dataclass(frozen=True)
class Filesystem:
    mountpoint: str
    device: str
    size: float
    avail: float

    @property
    def used_fraction(self) -> float:
        """Return how full the filesystem is.

        Returns:
            Used share from 0 to 1.
        """
        return (self.size - self.avail) / self.size if self.size else 0.0


@dataclass(frozen=True)
class ChipTemp:
    """The temperature sensors of one hwmon chip (CPU package, disk, …)."""

    chip: str  # node-exporter's chip id, e.g. platform_coretemp_0
    label: str
    average: float
    hottest: float
    sensors: int
    critical: float | None  # lowest critical limit of its sensors

    @property
    def level(self) -> str:
        """Tell how close the chip is to its critical limit.

        Returns:
            See ``temperature_level``.
        """
        return temperature_level(self.hottest, self.critical)


def temperature_level(hottest: float, limit: float | None) -> str:
    """Rate a temperature against its limit.

    Args:
        hottest: Degrees Celsius.
        limit: The critical (or slowdown) temperature, or None if unknown.

    Returns:
        ``hot`` at or above the limit, ``warm`` within ``TEMP_WARN_MARGIN`` of it, else ``ok``.
    """
    if limit is None:
        return "ok"
    if hottest >= limit:
        return "hot"
    return "warm" if hottest >= limit - TEMP_WARN_MARGIN else "ok"


@dataclass(frozen=True)
class Sample:
    """The counters and gauges of one read of node-exporter."""

    time: float
    hostname: str
    os_name: str
    uptime: float | None
    load: tuple[float, float, float] | None
    cpus: int
    cpu_busy: float  # seconds spent not idle (all CPUs)
    cpu_total: float  # seconds (all CPUs, all modes)
    mem_total: float | None
    mem_available: float | None
    swap_total: float | None
    swap_free: float | None
    filesystems: tuple[Filesystem, ...]
    disk_read: dict[str, float] = field(default_factory=dict)  # bytes, per disk
    disk_written: dict[str, float] = field(default_factory=dict)
    disk_busy: dict[str, float] = field(default_factory=dict)  # io_time seconds
    net_rx: dict[str, float] = field(default_factory=dict)  # bytes, per interface
    net_tx: dict[str, float] = field(default_factory=dict)
    temperatures: tuple[ChipTemp, ...] = ()


def _by_device(metrics: Metrics, name: str, skip: re.Pattern[str]) -> dict[str, float]:
    """Return a per-device counter, without the devices ``skip`` matches.

    Args:
        metrics: Parsed samples.
        name: Metric name with a ``device`` label.
        skip: Devices to leave out.

    Returns:
        Value by device name.
    """
    return {
        labels["device"]: value
        for labels, value in metrics.get(name, [])
        if "device" in labels and not skip.match(labels["device"])
    }


def _filesystems(metrics: Metrics) -> tuple[Filesystem, ...]:
    """Return the real filesystems, one per device (the shortest mountpoint).

    Args:
        metrics: Parsed samples.

    Returns:
        Filesystems sorted by mountpoint.
    """
    avail = {
        labels.get("mountpoint"): value
        for labels, value in metrics.get("node_filesystem_avail_bytes", [])
    }
    by_device: dict[str, Filesystem] = {}
    for labels, size in metrics.get("node_filesystem_size_bytes", []):
        mountpoint, device = labels.get("mountpoint", ""), labels.get("device", "")
        if (
            labels.get("fstype") in SKIP_FSTYPES
            or mountpoint.startswith(SKIP_MOUNT_PREFIXES)
            or size <= 0
            or mountpoint not in avail
        ):
            continue
        current = by_device.get(device)
        if current is None or len(mountpoint) < len(current.mountpoint):
            by_device[device] = Filesystem(mountpoint, device, size, avail[mountpoint])
    return tuple(sorted(by_device.values(), key=lambda fs: fs.mountpoint))


def _temperatures(metrics: Metrics) -> tuple[ChipTemp, ...]:
    """Group the hwmon temperature sensors by chip and average them.

    Args:
        metrics: Parsed samples.

    Returns:
        One entry per chip with readings, sorted by label.
    """
    low, high = TEMP_RANGE
    names = {
        labels.get("chip", ""): labels.get("chip_name", "")
        for labels, _ in metrics.get("node_hwmon_chip_names", [])
    }
    readings: dict[str, list[float]] = {}
    for labels, value in metrics.get("node_hwmon_temp_celsius", []):
        if low < value < high:
            readings.setdefault(labels.get("chip", ""), []).append(value)
    limits: dict[str, float] = {}
    for labels, value in metrics.get("node_hwmon_temp_crit_celsius", []):
        chip = labels.get("chip", "")
        if low < value < high:
            limits[chip] = min(value, limits.get(chip, value))
    bases = {chip: re.sub(r"_\d+$", "", names.get(chip) or chip) for chip in readings}
    counts = Counter(bases.values())
    chips = []
    for chip, values in readings.items():
        label = CHIP_LABELS.get(bases[chip], bases[chip])
        if counts[bases[chip]] > 1:  # two NVMe disks, two CPU sockets, several ACPI zones
            label = f"{label} {chip.rsplit('_', 1)[-1]}"
        chips.append(
            ChipTemp(
                chip,
                label,
                sum(values) / len(values),
                max(values),
                len(values),
                limits.get(chip),
            )
        )
    return tuple(sorted(chips, key=lambda temp: temp.label))


def sample(metrics: Metrics) -> Sample:
    """Pick the values the status page shows out of parsed node-exporter output.

    Args:
        metrics: Parsed samples.

    Returns:
        One sample.
    """
    busy = total = 0.0
    cpus: set[str] = set()
    for labels, value in metrics.get("node_cpu_seconds_total", []):
        total += value
        if labels.get("mode") == "idle":
            cpus.add(labels.get("cpu", ""))
        elif labels.get("mode") != "iowait":
            busy += value
    now = _one(metrics, "node_time_seconds") or 0.0
    boot = _one(metrics, "node_boot_time_seconds")
    loads = [_one(metrics, f"node_load{minutes}") for minutes in (1, 5, 15)]
    uname = metrics.get("node_uname_info", [({}, 0)])[0][0]
    os_info = metrics.get("node_os_info", [({}, 0)])[0][0]
    return Sample(
        time=now,
        hostname=uname.get("nodename", ""),
        os_name=os_info.get("pretty_name", ""),
        uptime=now - boot if boot and now else None,
        load=tuple(loads) if None not in loads else None,  # type: ignore[arg-type]
        cpus=len(cpus),
        cpu_busy=busy,
        cpu_total=total,
        mem_total=_one(metrics, "node_memory_MemTotal_bytes"),
        mem_available=_one(metrics, "node_memory_MemAvailable_bytes"),
        swap_total=_one(metrics, "node_memory_SwapTotal_bytes"),
        swap_free=_one(metrics, "node_memory_SwapFree_bytes"),
        filesystems=_filesystems(metrics),
        disk_read=_by_device(metrics, "node_disk_read_bytes_total", SKIP_DISK_RE),
        disk_written=_by_device(metrics, "node_disk_written_bytes_total", SKIP_DISK_RE),
        disk_busy=_by_device(metrics, "node_disk_io_time_seconds_total", SKIP_DISK_RE),
        net_rx=_by_device(metrics, "node_network_receive_bytes_total", SKIP_NET_RE),
        net_tx=_by_device(metrics, "node_network_transmit_bytes_total", SKIP_NET_RE),
        temperatures=_temperatures(metrics),
    )


@dataclass(frozen=True)
class DeviceRate:
    name: str
    read: float  # bytes/s (disks) or received bytes/s (network)
    write: float  # bytes/s (disks) or sent bytes/s (network)
    busy: float | None = None  # share of time the disk was busy, 0 to 1


@dataclass(frozen=True)
class Rates:
    cpu: float  # share of CPU time in use, 0 to 1
    disks: tuple[DeviceRate, ...]
    networks: tuple[DeviceRate, ...]


def rates(before: Sample, after: Sample) -> Rates | None:
    """Work out CPU use and disk and network traffic between two samples.

    Args:
        before: The earlier sample.
        after: The later sample.

    Returns:
        The rates, or None when the samples are not usable (no time passed, counters reset).
    """
    seconds = after.time - before.time
    cpu_total = after.cpu_total - before.cpu_total
    if seconds <= 0 or cpu_total <= 0:
        return None
    cpu = min(max((after.cpu_busy - before.cpu_busy) / cpu_total, 0.0), 1.0)

    def per_second(new: dict[str, float], old: dict[str, float], name: str) -> float:
        """Return a counter's increase per second, 0 if it is new or went backwards.

        Args:
            new: Counters of the later sample.
            old: Counters of the earlier sample.
            name: Device name.

        Returns:
            The rate per second.
        """
        return max(new.get(name, 0.0) - old.get(name, new.get(name, 0.0)), 0.0) / seconds

    disks = tuple(
        DeviceRate(
            name,
            per_second(after.disk_read, before.disk_read, name),
            per_second(after.disk_written, before.disk_written, name),
            min(per_second(after.disk_busy, before.disk_busy, name), 1.0),
        )
        for name in sorted(after.disk_read)
    )
    networks = tuple(
        DeviceRate(
            name,
            per_second(after.net_rx, before.net_rx, name),
            per_second(after.net_tx, before.net_tx, name),
        )
        for name in sorted(after.net_rx)
    )
    return Rates(cpu, disks, networks)


class NodeExporter:
    """Reads node-exporter's ``/metrics``."""

    def __init__(self, url: str, client: httpx2.Client | None = None) -> None:
        """Create the reader.

        Args:
            url: The metrics URL, e.g. ``http://host.docker.internal:9100/metrics``.
            client: HTTP client to use (tests pass one with a mock transport).
        """
        self.url = url
        self._client = client or httpx2.Client(timeout=4.0)

    def read(self) -> Sample:
        """Read and summarise the current metrics.

        Returns:
            One sample.

        Raises:
            NodeExporterError: When node-exporter can't be reached or answers badly.
        """
        try:
            response = self._client.get(self.url)
            response.raise_for_status()
        except httpx2.HTTPError as exc:
            raise NodeExporterError(str(exc) or type(exc).__name__) from exc
        metrics = parse(response.text)
        if "node_cpu_seconds_total" not in metrics:
            raise NodeExporterError("the answer has no node-exporter metrics")
        return sample(metrics)
