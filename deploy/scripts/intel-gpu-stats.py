# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

"""GPU load, memory and temperature of Intel GPUs (i915), served as JSON for OrbitalWindow.

Runs in its own container (compose.intel.yaml) as root with only CAP_PERFMON and /dev/dri: the
i915 PMU (engine busy time) and the real free memory of a memory region need CAP_PERFMON, which
the non-root service can't have. Python standard library only, so it runs in python:3.14-slim.

GET / answers a JSON list, one object per GPU, with the fields of OrbitalWindow's ``GpuStat``.
"""

import ctypes
import fcntl
import json
import logging
import os
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

log = logging.getLogger("intel-gpu-stats")

PORT = 9400
SAMPLE_SECONDS = 2.0
DRIVERS = ("i915",)
# DRM_IOCTL_I915_QUERY: _IOWR('d', DRM_COMMAND_BASE + 0x39, struct drm_i915_query)
I915_QUERY = 0xC0106479
QUERY_MEMORY_REGIONS = 4
MEMORY_CLASS_DEVICE = 1  # device-local memory (VRAM); class 0 is system memory
QUERY_ITEM = "QiIQ"  # query_id, length, flags, data_ptr
QUERY = "IIQ"  # num_items, flags, items_ptr
REGION = "HHIQQ"  # memory_class, memory_instance, rsvd0, probed_size, unallocated_size
REGION_SIZE = 88
REGIONS_HEADER = 16
PERF_EVENT_OPEN = 298  # x86_64 syscall number
# PCI device ids of common Intel GPUs. Other Arc A-series and Flex GPUs (DG2, ids 0x5690–0x56cf)
# show as "Intel Arc A-series GPU (8086:xxxx)": OrbitalWindow runs vLLM differently on these.
# Any other GPU shows as "Intel GPU (8086:xxxx)".
NAMES = {
    "0x56a0": "Intel Arc A770",
    "0x56a1": "Intel Arc A750",
    "0x56a2": "Intel Arc A580",
    "0x56a5": "Intel Arc A380",
    "0x56a6": "Intel Arc A310",
    "0x56c0": "Intel Data Center GPU Flex 170",
    "0x56c1": "Intel Data Center GPU Flex 140",
}


class PerfEventAttr(ctypes.Structure):
    """The start of ``struct perf_event_attr``; the kernel accepts zeros for the rest."""

    _fields_ = [
        ("type", ctypes.c_uint32),
        ("size", ctypes.c_uint32),
        ("config", ctypes.c_uint64),
        ("sample", ctypes.c_uint64),
        ("sample_type", ctypes.c_uint64),
        ("read_format", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("rest", ctypes.c_uint8 * 80),
    ]


def read_text(path: Path) -> str | None:
    """Read a small sysfs file.

    Args:
        path: The file.

    Returns:
        Its stripped contents, or None if it can't be read.
    """
    try:
        return path.read_text().strip()
    except OSError:
        return None


def gpu_name(device: Path) -> str:
    """Name a GPU from its PCI device id.

    Args:
        device: The GPU's sysfs device folder.

    Returns:
        A plain name such as ``Intel Arc A770``.
    """
    pci_id = read_text(device / "device") or "?"
    if pci_id in NAMES:
        return NAMES[pci_id]
    short = pci_id.removeprefix("0x")
    try:
        dg2 = 0x5690 <= int(pci_id, 16) <= 0x56CF
    except ValueError:
        dg2 = False
    return f"Intel Arc A-series GPU (8086:{short})" if dg2 else f"Intel GPU (8086:{short})"


def temperatures(device: Path) -> tuple[int | None, int | None]:
    """Read a GPU's temperature and critical limit from its hwmon folder.

    Args:
        device: The GPU's sysfs device folder.

    Returns:
        Degrees Celsius (temperature, limit), None where unknown.
    """
    for hwmon in sorted((device / "hwmon").glob("hwmon*")):
        values = []
        for name in ("temp1_input", "temp1_crit"):
            text = read_text(hwmon / name)
            values.append(int(text) // 1000 if text and text.lstrip("-").isdigit() else None)
        if values[0] is not None:
            return values[0], values[1]
    return None, None


def parse_regions(raw: bytes) -> tuple[int, int]:
    """Sum the device-local memory regions of a ``DRM_I915_QUERY_MEMORY_REGIONS`` answer.

    Args:
        raw: The query's data (``struct drm_i915_query_memory_regions``).

    Returns:
        Bytes (used, total) of device-local memory; (0, 0) for a GPU without its own memory.
    """
    count = struct.unpack_from("I", raw)[0]
    used = total = 0
    for index in range(count):
        mem_class, _, _, probed, unallocated = struct.unpack_from(
            REGION, raw, REGIONS_HEADER + index * REGION_SIZE
        )
        if mem_class == MEMORY_CLASS_DEVICE:
            total += probed
            used += probed - unallocated
    return used, total


def query_regions(node: Path) -> bytes:
    """Ask i915 for its memory regions. Without CAP_PERFMON, free memory reads as all of it.

    Args:
        node: The GPU's render node, e.g. ``/dev/dri/renderD128``.

    Returns:
        The raw answer (``struct drm_i915_query_memory_regions``).
    """
    fd = os.open(node, os.O_RDWR)
    try:
        item = bytearray(struct.pack(QUERY_ITEM, QUERY_MEMORY_REGIONS, 0, 0, 0))
        item_buf = (ctypes.c_char * len(item)).from_buffer(item)
        query = bytearray(struct.pack(QUERY, 1, 0, ctypes.addressof(item_buf)))
        fcntl.ioctl(fd, I915_QUERY, query)  # length 0: the kernel fills in the size it needs
        length = struct.unpack_from(QUERY_ITEM, item)[1]
        if length <= 0:
            raise OSError(-length, "memory region query failed")
        data = (ctypes.c_char * length)()
        address = ctypes.addressof(data)
        struct.pack_into(QUERY_ITEM, item, 0, QUERY_MEMORY_REGIONS, length, 0, address)
        fcntl.ioctl(fd, I915_QUERY, query)
        return bytes(data)
    finally:
        os.close(fd)


class EngineLoad:
    """Busy time of every engine of one GPU, from its i915 PMU."""

    def __init__(self, pmu: Path) -> None:
        """Open one perf counter per ``*-busy`` event of the PMU.

        Args:
            pmu: The PMU's folder under ``/sys/bus/event_source/devices``.
        """
        self._libc = ctypes.CDLL(None, use_errno=True)
        self._fds: list[int] = []
        pmu_type = int(read_text(pmu / "type") or "0")
        cpu = int((read_text(pmu / "cpumask") or "0").split(",")[0].split("-")[0])
        for event in sorted((pmu / "events").glob("*-busy")):
            config = int((read_text(event) or "config=0").split("=")[1], 16)
            attr = PerfEventAttr(type=pmu_type, size=ctypes.sizeof(PerfEventAttr), config=config)
            fd = self._libc.syscall(PERF_EVENT_OPEN, ctypes.byref(attr), -1, cpu, -1, 0)
            if fd < 0:
                log.warning("%s: %s", event.name, os.strerror(ctypes.get_errno()))
                continue
            self._fds.append(fd)
        self._last = self._read()

    def _read(self) -> tuple[float, list[int]]:
        """Read every counter.

        Returns:
            The time (ns) and each engine's total busy time (ns).
        """
        return time.monotonic_ns(), [struct.unpack("Q", os.read(fd, 8))[0] for fd in self._fds]

    def percent(self) -> int | None:
        """Return the busiest engine's load since the previous call.

        Returns:
            Percent (0–100), or None if no counter could be opened.
        """
        if not self._fds:
            return None
        now = self._read()
        elapsed = now[0] - self._last[0]
        busy = max(new - old for new, old in zip(now[1], self._last[1], strict=True))
        self._last = now
        return max(0, min(100, round(100 * busy / elapsed))) if elapsed > 0 else 0


class Gpu:
    """One Intel GPU: its render node, sysfs folder and PMU."""

    def __init__(self, index: int, node: Path, device: Path, pmu: Path | None) -> None:
        """Remember where to read the GPU.

        Args:
            index: Position among the GPUs (render node order).
            node: Its render node.
            device: Its sysfs device folder.
            pmu: Its i915 PMU folder, or None if there is none.
        """
        self.index = index
        self.node = node
        self.device = device
        self.name = gpu_name(device)
        self.load = EngineLoad(pmu) if pmu else None
        self.util = 0

    def sample(self) -> None:
        """Update the load from the PMU (called every ``SAMPLE_SECONDS``)."""
        if self.load:
            self.util = self.load.percent() or 0

    def stat(self) -> dict[str, Any]:
        """Read memory and temperature now.

        Returns:
            The fields of OrbitalWindow's ``GpuStat``.
        """
        try:
            used, total = parse_regions(query_regions(self.node))
        except OSError as exc:
            log.warning("%s: %s", self.node, exc)
            used = total = 0
        temperature, limit = temperatures(self.device)
        return {
            "index": self.index,
            "name": self.name,
            "util_percent": self.util,
            "mem_used": used,
            "mem_total": total,
            "temperature": temperature,
            "slowdown": limit,
        }


def pmu_folder(device: Path, sys_root: Path) -> Path | None:
    """Find the i915 PMU of a GPU: ``i915_<PCI address>`` (discrete) or ``i915`` (integrated).

    Args:
        device: The GPU's sysfs device folder.
        sys_root: Where sysfs is mounted.

    Returns:
        The PMU folder, or None.
    """
    sources = sys_root / "bus" / "event_source" / "devices"
    address = device.resolve().name.replace(":", "_")
    for name in (f"i915_{address}", "i915"):
        if (sources / name / "type").exists():
            return sources / name
    return None


def find_gpus(sys_root: Path = Path("/sys"), dev_root: Path = Path("/dev")) -> list[Gpu]:
    """Find the GPUs driven by i915, in render node order.

    Args:
        sys_root: Where sysfs is mounted.
        dev_root: Where the device nodes are.

    Returns:
        The GPUs.
    """
    gpus = []
    renders = sorted(
        (sys_root / "class" / "drm").glob("renderD*"), key=lambda path: int(path.name[7:])
    )
    for render in renders:
        device = render / "device"
        driver = (device / "driver").resolve().name
        if driver not in DRIVERS:
            continue
        node = dev_root / "dri" / render.name
        gpus.append(Gpu(len(gpus), node, device, pmu_folder(device, sys_root)))
    return gpus


def serve(gpus: list[Gpu], port: int = PORT) -> None:
    """Sample the load in the background and answer GET / with the stats.

    Args:
        gpus: The GPUs to report.
        port: TCP port to listen on.
    """

    def sampler() -> None:
        """Update every GPU's load every ``SAMPLE_SECONDS``."""
        while True:
            time.sleep(SAMPLE_SECONDS)
            for gpu in gpus:
                gpu.sample()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (http.server's name)
            """Answer with the stats of every GPU."""
            body = json.dumps([gpu.stat() for gpu in gpus]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            """Don't log every request.

            Args:
                format: Message format.
                *args: Format arguments.
            """

    threading.Thread(target=sampler, daemon=True).start()
    ThreadingHTTPServer(("", port), Handler).serve_forever()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    found = find_gpus()
    log.info("GPUs: %s", ", ".join(f"{gpu.name} ({gpu.node})" for gpu in found) or "none")
    if not found:
        sys.exit("No Intel GPU with the i915 driver found.")
    serve(found)
