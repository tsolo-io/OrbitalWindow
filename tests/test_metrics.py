# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import httpx2

from orbitalwindow.metrics import ContainerMetrics, GpuMetrics, fmt_bytes

SPEC = {
    "/system.slice/docker-a.scope": {
        "aliases": ["ow-user1", "a"],
        "labels": {"ow.managed": "true", "ow.user": "user1"},
        "memory": {"limit": 16 * 1024**3},
    },
    "/system.slice/docker-b.scope": {
        "aliases": ["ow-user2", "b"],
        "labels": {"ow.managed": "true"},
        "memory": {"limit": 18446744073709551615},
    },
    "/system.slice/docker-c.scope": {"aliases": ["someone-else", "c"], "labels": {}},
}
SUMMARY = {
    "/system.slice/docker-a.scope": {"latest_usage": {"cpu": 1500, "memory": 2 * 1024**3}},
    "/system.slice/docker-b.scope": {"latest_usage": {"cpu": 0, "memory": 100 * 1024**2}},
    "/system.slice/docker-c.scope": {"latest_usage": {"cpu": 9000, "memory": 1}},
}


def make(handler):
    # Same base path as the real deployment (cAdvisor's --url_base_prefix).
    base = "http://cadvisor:8080/admin/cadvisor"
    client = httpx2.Client(base_url=base, transport=httpx2.MockTransport(handler))
    return ContainerMetrics(base, client)


def test_usage_from_cadvisor():
    calls = []

    def handler(request):
        calls.append(request.url.path)
        data = SPEC if request.url.path.endswith("/spec") else SUMMARY
        return httpx2.Response(200, json=data)

    m = make(handler)
    usage = m.usage()
    assert set(usage) == {"ow-user1", "ow-user2"}  # only OrbitalWindow's containers
    assert usage["ow-user1"].cpu_cores == 1.5
    assert usage["ow-user1"].mem_limit == 16 * 1024**3
    assert usage["ow-user2"].mem_limit is None
    m.usage()  # cached
    assert calls == ["/admin/cadvisor/api/v2.0/spec", "/admin/cadvisor/api/v2.0/summary"]


def test_cadvisor_down_gives_empty_usage():
    m = make(lambda request: httpx2.Response(503))
    assert m.usage() == {}
    assert not m.available


def test_gpu_metrics_without_nvidia():
    assert GpuMetrics().stats() == []  # no driver on test machines


def test_fmt_bytes():
    assert fmt_bytes(None) == "unlimited"
    assert fmt_bytes(3 * 1024**3) == "3.0 GB"
    assert fmt_bytes(200 * 1024**2) == "200 MB"
