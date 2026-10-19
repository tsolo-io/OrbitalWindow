# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import importlib.util
import struct
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "deploy" / "scripts" / "intel-gpu-stats.py"


@pytest.fixture(scope="module")
def stats():
    """The exporter script, loaded as a module."""
    spec = importlib.util.spec_from_file_location("intel_gpu_stats", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def regions(*entries):
    """Pack a ``drm_i915_query_memory_regions`` answer.

    Args:
        *entries: (memory class, probed size, unallocated size) per region.

    Returns:
        The raw answer.
    """
    raw = bytearray(struct.pack("I12x", len(entries)))
    for mem_class, probed, unallocated in entries:
        raw += struct.pack("HHIQQ", mem_class, 0, 0, probed, unallocated) + bytes(64)
    return bytes(raw)


def test_device_memory_is_summed_and_system_memory_ignored(stats):
    """Only device-local regions count; used is probed minus unallocated."""
    raw = regions((0, 32 * 1024**3, 32 * 1024**3), (1, 16 * 1024**3, 15 * 1024**3))
    assert stats.parse_regions(raw) == (1024**3, 16 * 1024**3)
    assert stats.parse_regions(regions((0, 8 * 1024**3, 4 * 1024**3))) == (0, 0)  # integrated


def fake_sysfs(root, driver, device_id, temp=None):
    """Make a sysfs tree with one GPU on renderD128.

    Args:
        root: Folder to build it in.
        driver: Kernel driver name of the GPU.
        device_id: PCI device id, e.g. ``0x56a0``.
        temp: hwmon temp1_input value (millidegrees), or None for no hwmon.

    Returns:
        The sysfs root.
    """
    pci = root / "devices" / "pci0000:00" / "0000:03:00.0"
    (pci / "hwmon" / "hwmon5").mkdir(parents=True)
    (pci / "device").write_text(device_id + "\n")
    if temp is not None:
        (pci / "hwmon" / "hwmon5" / "temp1_input").write_text(f"{temp}\n")
    drivers = root / "bus" / "pci" / "drivers" / driver
    drivers.mkdir(parents=True)
    (pci / "driver").symlink_to(drivers)
    render = root / "class" / "drm" / "renderD128"
    render.mkdir(parents=True)
    (render / "device").symlink_to(pci)
    return root


def test_finds_i915_gpus_with_name_and_temperature(stats, tmp_path):
    """An i915 GPU is found, named from its PCI id, with its hwmon temperature."""
    root = fake_sysfs(tmp_path, "i915", "0x56a0", temp=49000)
    gpus = stats.find_gpus(root, Path("/dev"))
    assert [(gpu.index, gpu.name, str(gpu.node)) for gpu in gpus] == [
        (0, "Intel Arc A770", "/dev/dri/renderD128")
    ]
    assert stats.temperatures(gpus[0].device) == (49, None)
    assert gpus[0].load is None  # no PMU in the fake tree


def test_other_drivers_and_unknown_ids(stats, tmp_path):
    """Other drivers are skipped; an unknown PCI id gets a generic name."""
    assert stats.find_gpus(fake_sysfs(tmp_path / "a", "amdgpu", "0x744c"), Path("/dev")) == []
    gpus = stats.find_gpus(fake_sysfs(tmp_path / "b", "i915", "0x1234"), Path("/dev"))
    assert gpus[0].name == "Intel GPU (8086:1234)"


def test_unknown_arc_a_series_ids_are_named_as_such(stats, tmp_path):
    """DG2 ids not in the table are named Arc A-series, so the service treats them like an A770."""
    gpus = stats.find_gpus(fake_sysfs(tmp_path, "i915", "0x56b3"), Path("/dev"))
    assert gpus[0].name == "Intel Arc A-series GPU (8086:56b3)"
