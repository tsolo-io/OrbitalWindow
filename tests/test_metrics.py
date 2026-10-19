# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import httpx2

from orbitalwindow.metrics import ContainerMetrics, GpuMetrics, GpuStat, fmt_bytes

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
