# SPDX-FileCopyrightText: 2026 Tsolo.io
#
# SPDX-License-Identifier: Apache-2.0

import httpx2
import pytest

from orbitalwindow.hoststats import NodeExporter, NodeExporterError, parse, rates, sample
from orbitalwindow.ui.status import duration, size

TEMPLATE = """\
# HELP node_load1 1m load average.
# TYPE node_load1 gauge
node_load1 {load}
node_load5 0.25
node_load15 0.44
node_time_seconds {time}
node_boot_time_seconds 1000
node_memory_MemTotal_bytes 3.2e+10
node_memory_MemAvailable_bytes 2.4e+10
node_memory_SwapTotal_bytes 0
node_memory_SwapFree_bytes 0
node_cpu_seconds_total{{cpu="0",mode="idle"}} {idle}
node_cpu_seconds_total{{cpu="0",mode="user"}} {user}
node_cpu_seconds_total{{cpu="0",mode="iowait"}} 5
node_cpu_seconds_total{{cpu="1",mode="idle"}} {idle}
node_cpu_seconds_total{{cpu="1",mode="user"}} {user}
node_filesystem_size_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/"}} 1e+11
node_filesystem_avail_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/"}} 2.5e+10
node_filesystem_size_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/srv/x"}} 1e+11
node_filesystem_avail_bytes{{device="/dev/sda1",fstype="ext4",mountpoint="/srv/x"}} 2.5e+10
node_filesystem_size_bytes{{device="/dev/sda15",fstype="vfat",mountpoint="/boot/efi"}} 1e+08
node_filesystem_avail_bytes{{device="/dev/sda15",fstype="vfat",mountpoint="/boot/efi"}} 1e+08
node_filesystem_size_bytes{{device="tmpfs",fstype="tmpfs",mountpoint="/run"}} 1e+09
node_filesystem_avail_bytes{{device="tmpfs",fstype="tmpfs",mountpoint="/run"}} 1e+09
node_disk_read_bytes_total{{device="sda"}} {read}
node_disk_read_bytes_total{{device="sda1"}} {read}
node_disk_read_bytes_total{{device="loop0"}} 10
node_disk_written_bytes_total{{device="sda"}} 0
node_disk_io_time_seconds_total{{device="sda"}} {busy}
node_network_receive_bytes_total{{device="eth0"}} {rx}
node_network_receive_bytes_total{{device="lo"}} 99
node_network_receive_bytes_total{{device="veth1234"}} 99
node_network_transmit_bytes_total{{device="eth0"}} 0
node_uname_info{{nodename="naught",sysname="Linux"}} 1
node_os_info{{pretty_name="Ubuntu 26.04.1 LTS",name="Ubuntu"}} 1
"""


def text(time=2000.0, load=0.5, idle=100.0, user=100.0, read=0.0, busy=0.0, rx=0.0) -> str:
    """Return node-exporter output with the given counters."""
    return TEMPLATE.format(time=time, load=load, idle=idle, user=user, read=read, busy=busy, rx=rx)


def test_parse_labels_and_values():
    """Comments are skipped, labels (also with escaped quotes) and values are read."""
    metrics = parse('# HELP x y\nnode_x{a="1",b="say \\"hi\\""} 2.5e+00\nnode_y 3\nbad line\n')
    assert metrics["node_x"] == [({"a": "1", "b": 'say \\"hi\\"'}, 2.5)]
    assert metrics["node_y"] == [({}, 3.0)]


def test_sample_summarises_and_skips_virtual_devices():
    """Partitions, loop devices, lo/veth, tmpfs and /boot are left out; one entry per device."""
    current = sample(parse(text()))
    assert current.hostname == "naught" and current.os_name == "Ubuntu 26.04.1 LTS"
    assert current.uptime == 1000 and current.cpus == 2 and current.load == (0.5, 0.25, 0.44)
    assert current.mem_total == 3.2e10 and current.mem_available == 2.4e10
    assert [fs.mountpoint for fs in current.filesystems] == ["/"]
    assert current.filesystems[0].used_fraction == pytest.approx(0.75)
    assert list(current.disk_read) == ["sda"] and list(current.net_rx) == ["eth0"]


def test_rates_between_two_samples():
    """CPU use excludes idle and iowait; traffic is bytes per second; busy is capped at 100 %."""
    before = sample(parse(text(time=2000, idle=100, user=100, read=0, busy=0, rx=0)))
    after = sample(parse(text(time=2010, idle=105, user=115, read=5e6, busy=20, rx=1e6)))
    result = rates(before, after)
    assert result.cpu == pytest.approx(30 / 40)
    disk = result.disks[0]
    assert disk.name == "sda" and disk.read == 5e5 and disk.busy == 1.0
    assert result.networks[0].read == 1e5


def test_rates_need_time_to_pass_and_survive_counter_resets():
    """No time passed gives no rates; a counter that went back (reboot) counts as 0."""
    first = sample(parse(text(time=2000, read=5e6)))
    assert rates(first, first) is None
    later = sample(parse(text(time=2010, idle=110, read=0)))
    assert rates(first, later).disks[0].read == 0


def exporter(handler) -> NodeExporter:
    """Return a NodeExporter whose requests go to ``handler``."""
    client = httpx2.Client(transport=httpx2.MockTransport(handler))
    return NodeExporter("http://host:9100/metrics", client)


def test_read_ok_and_errors():
    """A good answer is summarised; refused connections and non-node answers are errors."""
    assert exporter(lambda request: httpx2.Response(200, text=text())).read().cpus == 2
    with pytest.raises(NodeExporterError, match="no node-exporter"):
        exporter(lambda request: httpx2.Response(200, text="up 1\n")).read()
    with pytest.raises(NodeExporterError):
        exporter(lambda request: httpx2.Response(503)).read()

    def refuse(request):
        """Fail like a closed port."""
        raise httpx2.ConnectError("Connection refused", request=request)

    with pytest.raises(NodeExporterError, match="refused"):
        exporter(refuse).read()


def test_formatting():
    """Sizes and uptimes are short and readable."""
    assert size(1.5 * 1024**3) == "1.5 GB" and size(512) == "512 B" and size(None) == "–"
    assert duration(3 * 86400 + 7200) == "3 days 2 h" and duration(3720) == "1 h 2 min"


HWMON = """\
node_hwmon_chip_names{chip="nvme_nvme0",chip_name="nvme"} 1
node_hwmon_chip_names{chip="platform_coretemp_0",chip_name="coretemp"} 1
node_hwmon_chip_names{chip="thermal_thermal_zone0",chip_name="acpitz_0"} 1
node_hwmon_chip_names{chip="thermal_thermal_zone1",chip_name="acpitz_1"} 1
node_hwmon_chip_names{chip="platform_nct6775_656",chip_name="nct6798"} 1
node_hwmon_chip_names{chip="0000:02:01_0_0000:03:00_0",chip_name="i915"} 1
node_hwmon_temp_celsius{chip="nvme_nvme0",sensor="temp1"} 40.85
node_hwmon_temp_celsius{chip="nvme_nvme0",sensor="temp2"} 45.85
node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp1"} 92
node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp2"} 40
node_hwmon_temp_celsius{chip="platform_coretemp_0",sensor="temp3"} 44
node_hwmon_temp_celsius{chip="thermal_thermal_zone0",sensor="temp0"} 27.8
node_hwmon_temp_celsius{chip="thermal_thermal_zone1",sensor="temp0"} 29.8
node_hwmon_temp_celsius{chip="platform_nct6775_656",sensor="temp1"} 35
node_hwmon_temp_celsius{chip="0000:02:01_0_0000:03:00_0",sensor="temp1"} 49
node_hwmon_temp_celsius{chip="platform_nct6775_656",sensor="temp2"} -62
node_hwmon_temp_celsius{chip="platform_nct6775_656",sensor="temp3"} 0
node_hwmon_temp_crit_celsius{chip="nvme_nvme0",sensor="temp1"} 80.85
node_hwmon_temp_crit_celsius{chip="platform_coretemp_0",sensor="temp1"} 100
node_hwmon_temp_crit_celsius{chip="platform_coretemp_0",sensor="temp2"} 100
node_hwmon_temp_max_celsius{chip="nvme_nvme0",sensor="temp2"} 65261.85
"""


def test_temperatures_grouped_and_averaged_per_chip():
    """Sensors are averaged per chip with plain names; broken readings are left out."""
    temps = {temp.label: temp for temp in sample(parse(HWMON)).temperatures}
    assert list(temps) == [
        "CPU",
        "Intel GPU",
        "Motherboard zone0",
        "Motherboard zone1",
        "NVMe disk",
        "nct6798",
    ]
    cpu = temps["CPU"]
    assert cpu.average == pytest.approx(58.67, abs=0.01)
    assert (cpu.hottest, cpu.sensors, cpu.critical) == (92, 3, 100)
    assert cpu.level == "warm"  # the hottest core is within 10 °C of the limit
    assert temps["NVMe disk"].average == pytest.approx(43.35)
    assert temps["NVMe disk"].critical == pytest.approx(80.85) and temps["NVMe disk"].level == "ok"
    assert temps["Intel GPU"].average == 49  # i915 hwmon of an Arc A770
    assert temps["nct6798"].sensors == 1 and temps["nct6798"].critical is None
    assert sample(parse("node_load1 1\n")).temperatures == ()


def test_temperature_levels():
    """ok below the warning margin, warm close to the limit, hot at it; no limit is ok."""
    from orbitalwindow.hoststats import temperature_level

    assert temperature_level(70, 90) == "ok"
    assert temperature_level(80, 90) == "warm"
    assert temperature_level(90, 90) == "hot"
    assert temperature_level(120, None) == "ok"
