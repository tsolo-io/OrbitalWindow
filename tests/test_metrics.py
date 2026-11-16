# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import httpx2

from orbitalwindow.metrics import AmdGpus, ContainerMetrics, GpuMetrics, GpuStat, fmt_bytes

A, B, C = "a" * 64, "b" * 64, "c" * 64
SUMMARY = {
    "/": {"latest_usage": {"cpu": 9000, "memory": 1}},
    f"/system.slice/docker-{A}.scope": {"latest_usage": {"cpu": 1500, "memory": 2 * 1024**3}},
    f"/docker/{B}": {"latest_usage": {"cpu": 0, "memory": 100 * 1024**2}},  # cgroupfs driver
    f"/system.slice/docker-{C}.scope": {"latest_usage": {"cpu": 1, "memory": 1}},
}


def make(handler):
    """Return ContainerMetrics talking to a mocked cAdvisor.

    Args:
        handler: httpx mock transport handler that answers the requests.

    Returns:
        ContainerMetrics using the deployment's cAdvisor base path.
    """
    # Same base path as the real deployment (cAdvisor's --url_base_prefix).
    base = "http://cadvisor:8080/admin/cadvisor"
    client = httpx2.Client(base_url=base, transport=httpx2.MockTransport(handler))
    return ContainerMetrics(base, client)


def test_usage_from_cadvisor_cgroups():
    """Usage is read from cAdvisor cgroups for the asked containers and cached."""
    calls = []

    def handler(request):
        """Record the request URL and answer with the canned summary.

        Args:
            request: Incoming mocked request.

        Returns:
            A 200 response with ``SUMMARY`` as JSON.
        """
        calls.append(str(request.url))
        return httpx2.Response(200, json=SUMMARY)

    m = make(handler)
    usage = m.usage({A: "ow-user1", B: "ow-user2", "d" * 64: "ow-gone"})
    assert set(usage) == {"ow-user1", "ow-user2"}  # only the containers asked for, if known
    assert usage["ow-user1"].cpu_cores == 1.5
    assert usage["ow-user1"].mem_bytes == 2 * 1024**3
    m.usage({A: "ow-user1"})  # cached
    assert calls == [
        "http://cadvisor:8080/admin/cadvisor/api/v2.0/summary/?type=name&recursive=true"
    ]


def test_cadvisor_down_gives_empty_usage():
    """An unavailable cAdvisor gives empty usage and marks metrics unavailable."""
    m = make(lambda request: httpx2.Response(503))
    assert m.usage({A: "ow-user1"}) == {}
    assert not m.available


def test_gpu_metrics_without_nvidia():
    """GPU metrics are empty without an NVIDIA driver."""
    assert GpuMetrics().stats() == []  # no driver on test machines


def test_fmt_bytes():
    """Byte counts are formatted for display."""
    assert fmt_bytes(None) == "unlimited"
    assert fmt_bytes(3 * 1024**3) == "3.0 GB"
    assert fmt_bytes(200 * 1024**2) == "200 MB"


def test_gpu_temperature_and_slowdown_from_nvml():
    """Temperature and slowdown threshold come from NVML; unsupported reads are None."""
    from types import SimpleNamespace

    def unsupported(*args):
        """Fail like NVML does for a value the GPU doesn't report."""
        raise RuntimeError("Not Supported")

    gpus = GpuMetrics()
    gpus._nvml = SimpleNamespace(
        NVML_TEMPERATURE_GPU=0,
        NVML_TEMPERATURE_THRESHOLD_SLOWDOWN=1,
        nvmlDeviceGetTemperature=lambda handle, sensor: 61,
        nvmlDeviceGetTemperatureThreshold=lambda handle, kind: 88,
    )
    assert gpus._temperatures("gpu0") == (61, 88)
    gpus._nvml.nvmlDeviceGetTemperatureThreshold = unsupported
    assert gpus._temperatures("gpu0") == (61, None)


def test_gpu_metrics_from_the_intel_exporter():
    """With an exporter URL, GPU numbers come from its JSON; a failing exporter gives none."""
    answer = [
        {
            "index": 0,
            "name": "Intel Arc A770",
            "util_percent": 37,
            "mem_used": 1024**3,
            "mem_total": 16 * 1024**3,
            "temperature": 49,
            "slowdown": None,
        }
    ]

    def transport(status, body):
        """Return a client whose every request gets this answer.

        Args:
            status: HTTP status.
            body: JSON body.

        Returns:
            The client.
        """
        handler = lambda request: httpx2.Response(status, json=body)  # noqa: E731
        return httpx2.Client(transport=httpx2.MockTransport(handler))

    url = "http://gpu-stats:9400/"
    gpus = GpuMetrics(url, transport(200, answer)).stats()
    assert gpus == [GpuStat(0, "Intel Arc A770", 37, 1024**3, 16 * 1024**3, 49, None)]
    assert GpuMetrics(url, transport(503, {})).stats() == []
    assert GpuMetrics(url, transport(200, [{"index": 0}])).stats() == []


def fake_card(root, card, pci, files, hwmon):
    """Add an amdgpu card to a fake sysfs.

    Args:
        root: The fake sysfs root.
        card: The DRM card name, e.g. ``card1``.
        pci: The card's PCI address.
        files: Device files by name.
        hwmon: hwmon files by name.
    """
    device = root / "devices" / pci
    (device / "hwmon" / "hwmon3").mkdir(parents=True)
    for name, value in files.items():
        (device / name).write_text(f"{value}\n")
    for name, value in hwmon.items():
        (device / "hwmon" / "hwmon3" / name).write_text(f"{value}\n")
    (root / "drm" / card).mkdir(parents=True)
    (root / "drm" / card / "device").symlink_to(device)
    (root / "drm" / f"{card}-DP-1").mkdir()  # a connector, not a card


def fake_sysfs(tmp_path):
    """Build the sysfs of the AMD production server: an RX 7900 XTX and Ryzen graphics.

    Args:
        tmp_path: pytest's temporary folder.

    Returns:
        An AmdGpus reader on the fake sysfs.
    """
    fake_card(
        tmp_path,
        "card1",
        "0000:03:00.0",
        {
            "vendor": "0x1002",
            "device": "0x744c",
            "revision": "0xc8",
            "gpu_busy_percent": 12,
            "mem_info_vram_used": 2 * 1024**3,
            "mem_info_vram_total": 24 * 1024**3,
            "mem_info_vram_vendor": "samsung",
        },
        {
            "temp1_label": "edge",
            "temp1_input": 31000,
            "temp1_crit": 100000,
            "temp2_label": "junction",
            "temp2_input": 38000,
            "temp2_crit": 110000,
        },
    )
    fake_card(
        tmp_path,
        "card2",
        "0000:0f:00.0",
        {
            "vendor": "0x1002",
            "device": "0x13c0",
            "revision": "0xc1",
            "gpu_busy_percent": 3,
            "mem_info_vram_used": 24 * 1024**2,
            "mem_info_vram_total": 2 * 1024**3,
        },
        {"temp1_label": "edge", "temp1_input": 48000},
    )
    kfd = tmp_path / "kfd"
    for node, props in enumerate(
        [
            "cpu_cores_count 32\nsimd_count 0\nlocation_id 0\ndomain 0\n",
            "simd_count 192\nlocation_id 768\ndomain 0\n",  # 03:00.0
            "simd_count 4\nlocation_id 3840\ndomain 0\n",  # 0f:00.0
        ]
    ):
        (kfd / str(node)).mkdir(parents=True)
        (kfd / str(node) / "properties").write_text(props)
    ids = tmp_path / "amdgpu.ids"
    ids.write_text(
        "# comment\n1.0.0\n744C,\tC8,\tAMD Radeon RX 7900 XTX\n744C,\tCC,\tAMD Radeon RX 7900 XT\n"
    )
    return AmdGpus(tmp_path / "drm", kfd, ids)


def test_amd_gpu_numbers_from_sysfs(tmp_path):
    """AMD numbers come from sysfs; the built-in graphics is left out next to a discrete GPU."""
    assert fake_sysfs(tmp_path).stats() == [
        GpuStat(0, "AMD Radeon RX 7900 XTX", 12, 2 * 1024**3, 24 * 1024**3, 31, 100)
    ]


def test_amd_index_follows_the_kfd_topology(tmp_path):
    """The index is the HIP device number, the GPU's place among the KFD GPU nodes."""
    amd = fake_sysfs(tmp_path)
    kfd = tmp_path / "kfd"
    # ROCm lists the built-in graphics first: the RX 7900 XTX is HIP device 1.
    (kfd / "1" / "properties").write_text("simd_count 4\nlocation_id 3840\ndomain 0\n")
    (kfd / "2" / "properties").write_text("simd_count 192\nlocation_id 768\ndomain 0\n")
    assert [gpu.index for gpu in amd.stats()] == [1]


def test_amd_built_in_graphics_alone_is_shown(tmp_path):
    """A host with only built-in graphics shows it, with a fallback name."""
    amd = fake_sysfs(tmp_path)
    (tmp_path / "drm" / "card1" / "device" / "gpu_busy_percent").unlink()  # no numbers
    assert amd.stats() == [GpuStat(1, "AMD GPU 0x13c0", 3, 24 * 1024**2, 2 * 1024**3, 48, None)]


def test_amd_without_kfd_or_names(tmp_path):
    """Without the KFD topology the PCI order numbers the GPUs; unknown IDs get a fallback name."""
    fake_sysfs(tmp_path)
    amd = AmdGpus(tmp_path / "drm", tmp_path / "missing", tmp_path / "missing.ids")
    assert [(gpu.index, gpu.name) for gpu in amd.stats()] == [(0, "AMD GPU 0x744c")]
    assert AmdGpus(tmp_path / "missing").stats() == []


def test_gpu_metrics_follow_the_vendor_setting(tmp_path):
    """GpuMetrics reads the amdgpu driver only while the GPU vendor setting is AMD."""
    vendor = ["amd"]
    gpus = GpuMetrics(vendor=lambda: vendor[0], amd=fake_sysfs(tmp_path))
    assert [gpu.name for gpu in gpus.stats()] == ["AMD Radeon RX 7900 XTX"]
    vendor[0] = "nvidia"
    gpus._at = 0.0  # skip the cache
    assert gpus.stats() == []  # NVML: no driver on test machines


def test_amd_sleeping_gpu_counts_as_idle(tmp_path):
    """A discrete GPU in runtime power saving is idle, not missing; it has no temperature."""
    amd = fake_sysfs(tmp_path)
    device = tmp_path / "drm" / "card1" / "device"
    # amdgpu answers "Device or resource busy" while the GPU sleeps; a missing file reads the same.
    (device / "gpu_busy_percent").unlink()
    (device / "hwmon" / "hwmon3" / "temp1_input").unlink()
    (device / "power").mkdir()
    (device / "power" / "runtime_status").write_text("suspended\n")
    assert amd.stats() == [
        GpuStat(0, "AMD Radeon RX 7900 XTX", 0, 2 * 1024**3, 24 * 1024**3, None, 100)
    ]
