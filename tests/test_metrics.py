# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import httpx2

from orbitalwindow.metrics import ContainerMetrics, GpuMetrics, fmt_bytes

A, B, C = "a" * 64, "b" * 64, "c" * 64
SUMMARY = {
    "/": {"latest_usage": {"cpu": 9000, "memory": 1}},
    f"/system.slice/docker-{A}.scope": {"latest_usage": {"cpu": 1500, "memory": 2 * 1024**3}},
    f"/docker/{B}": {"latest_usage": {"cpu": 0, "memory": 100 * 1024**2}},  # cgroupfs driver
    f"/system.slice/docker-{C}.scope": {"latest_usage": {"cpu": 1, "memory": 1}},
}


def make(handler):
    # Same base path as the real deployment (cAdvisor's --url_base_prefix).
    base = "http://cadvisor:8080/admin/cadvisor"
    client = httpx2.Client(base_url=base, transport=httpx2.MockTransport(handler))
    return ContainerMetrics(base, client)


def test_usage_from_cadvisor_cgroups():
    calls = []

    def handler(request):
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
    m = make(lambda request: httpx2.Response(503))
    assert m.usage({A: "ow-user1"}) == {}
    assert not m.available


def test_gpu_metrics_without_nvidia():
    assert GpuMetrics().stats() == []  # no driver on test machines


def test_fmt_bytes():
    assert fmt_bytes(None) == "unlimited"
    assert fmt_bytes(3 * 1024**3) == "3.0 GB"
    assert fmt_bytes(200 * 1024**2) == "200 MB"
