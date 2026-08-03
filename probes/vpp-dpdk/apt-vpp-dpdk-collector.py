#!/usr/bin/env python3
import os
import re
import subprocess
import tempfile
import time


OUTPUT = "/var/lib/node_exporter/textfile_collector/apt_vpp_dpdk.prom"
PATHS = ("/home", "/data", "/data_pts")
VPPCTL = "/data/vpp/vppids/vppctl.sh"
DPDK_CLIENT = "/home/webdefender/bin/dpdkClient"


def label_value(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def sample(name, value, labels=None):
    suffix = ""
    if labels:
        suffix = "{" + ",".join(f'{key}="{label_value(val)}"' for key, val in labels.items()) + "}"
    return f"{name}{suffix} {value}"


def metric(name, help_text, metric_type, values):
    return [f"# HELP {name} {help_text}", f"# TYPE {name} {metric_type}", *values]


def run(command):
    env = os.environ.copy()
    env["VPP_HOME"] = "/data/vpp"
    result = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        timeout=10,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"exit status {result.returncode}")
    return result.stdout


def collect_filesystems():
    values = {name: [] for name in ("exists", "size", "used", "free", "available", "ratio")}
    for path in PATHS:
        labels = {"path": path}
        exists = os.path.isdir(path)
        values["exists"].append(sample("apt_path_exists", int(exists), labels))
        if not exists:
            continue
        stats = os.statvfs(path)
        size = stats.f_blocks * stats.f_frsize
        free = stats.f_bfree * stats.f_frsize
        available = stats.f_bavail * stats.f_frsize
        used = size - free
        ratio = used / size if size else 0
        values["size"].append(sample("apt_path_size_bytes", size, labels))
        values["used"].append(sample("apt_path_used_bytes", used, labels))
        values["free"].append(sample("apt_path_free_bytes", free, labels))
        values["available"].append(sample("apt_path_available_bytes", available, labels))
        values["ratio"].append(sample("apt_path_usage_ratio", f"{ratio:.9f}", labels))
    lines = []
    lines += metric("apt_path_exists", "Whether the monitored path exists.", "gauge", values["exists"])
    lines += metric("apt_path_size_bytes", "Filesystem size for a monitored path.", "gauge", values["size"])
    lines += metric("apt_path_used_bytes", "Filesystem used bytes for a monitored path.", "gauge", values["used"])
    lines += metric("apt_path_free_bytes", "Filesystem free bytes for a monitored path.", "gauge", values["free"])
    lines += metric("apt_path_available_bytes", "Filesystem bytes available to unprivileged users.", "gauge", values["available"])
    lines += metric("apt_path_usage_ratio", "Filesystem usage ratio for a monitored path.", "gauge", values["ratio"])
    return lines


def collect_vpp_interfaces(output):
    counters = {}
    current = None
    interface_re = re.compile(r"^\s*(\S+)\s+\d+\s+(?:up|down)\s+")
    counter_re = re.compile(r"\b(rx-miss|rx[- ]error|rx[- ]no[- ]buf)\s+(\d+)\s*$", re.IGNORECASE)
    for line in output.splitlines():
        interface_match = interface_re.match(line)
        if interface_match:
            current = interface_match.group(1)
            counters.setdefault(current, {"miss": 0, "error": 0, "no_buf": 0})
        if current is None:
            continue
        counter_match = counter_re.search(line)
        if not counter_match:
            continue
        name = counter_match.group(1).lower().replace(" ", "-")
        key = {"rx-miss": "miss", "rx-error": "error", "rx-no-buf": "no_buf"}[name]
        counters[current][key] = int(counter_match.group(2))
    if not counters:
        raise ValueError("no VPP interfaces parsed")
    names = {
        "miss": ("apt_vpp_interface_rx_miss_total", "VPP interface cumulative RX misses."),
        "error": ("apt_vpp_interface_rx_error_total", "VPP interface cumulative RX errors."),
        "no_buf": ("apt_vpp_interface_rx_no_buf_total", "VPP interface cumulative RX no-buffer drops."),
    }
    lines = []
    for key, (name, help_text) in names.items():
        values = [sample(name, data[key], {"interface": interface}) for interface, data in sorted(counters.items())]
        lines += metric(name, help_text, "counter", values)
    return lines


def collect_superflows(output):
    total_match = re.search(
        r"Total:\s*(\d+),\s*TCP:(\d+),\s*UDP:(\d+),\s*ICMP:(\d+),\s*OTHER:(\d+),\s*HTTP:(\d+)",
        output,
    )
    if not total_match:
        raise ValueError("Superflows totals not parsed")
    protocols = ("total", "tcp", "udp", "icmp", "other", "http")
    values = [
        sample("apt_superflows_connections", int(value), {"protocol": protocol})
        for protocol, value in zip(protocols, total_match.groups())
    ]
    frag_match = re.search(r"FragNum:\s*(\d+)", output)
    lines = metric(
        "apt_superflows_connections",
        "Current Superflows connections by protocol.",
        "gauge",
        values,
    )
    if frag_match:
        lines += metric(
            "apt_superflows_fragments",
            "Current Superflows fragment count.",
            "gauge",
            [sample("apt_superflows_fragments", int(frag_match.group(1)))],
        )
    return lines


def collect_mdbstat(output):
    current_interface = None
    current_thread = None
    rows = {}
    interface_re = re.compile(r"^\s*(\S+)\s+\d+\s+(?:up|down)(.*)$")
    thread_rx_re = re.compile(r"^\s*(\d+)\s+rx\s+(\d+)\s*$")
    inline_rx_re = re.compile(r"\s(\d+)\s+rx\s+(\d+)\s*$")
    miss_re = re.compile(r"rx-miss\s+(\d+)\s*$")
    for line in output.splitlines():
        interface_match = interface_re.match(line)
        if interface_match:
            current_interface = interface_match.group(1)
            current_thread = None
            inline_match = inline_rx_re.search(interface_match.group(2))
            if inline_match:
                current_thread = inline_match.group(1)
                rows[(current_interface, current_thread)] = {"rx": int(inline_match.group(2)), "miss": 0}
            continue
        if current_interface is None:
            continue
        thread_match = thread_rx_re.match(line)
        if thread_match:
            current_thread = thread_match.group(1)
            rows[(current_interface, current_thread)] = {"rx": int(thread_match.group(2)), "miss": 0}
            continue
        miss_match = miss_re.search(line)
        if miss_match and current_thread is not None:
            rows[(current_interface, current_thread)]["miss"] = int(miss_match.group(1))
    if not rows:
        raise ValueError("MDB interface/thread counters not parsed")
    rx_values = []
    miss_values = []
    for (interface, thread), data in sorted(rows.items()):
        labels = {"interface": interface, "thread": thread}
        rx_values.append(sample("apt_collector_rx_total", data["rx"], labels))
        miss_values.append(sample("apt_collector_rx_miss_total", data["miss"], labels))
    lines = metric("apt_collector_rx_total", "Collector cumulative received packets.", "counter", rx_values)
    lines += metric(
        "apt_collector_rx_miss_total",
        "Collector cumulative RX misses.",
        "counter",
        miss_values,
    )
    return lines


def collect_aud(output):
    row_re = re.compile(
        r"^\s*(\d+)\s+(\d+)\s+0x[0-9a-fA-F]+\s+\d+\s+(\d+)\s+(\d+)\s+(\d+)/(\d+)\(([0-9.]+)%\)"
    )
    rows = []
    for line in output.splitlines():
        match = row_re.match(line)
        if match:
            rows.append(match.groups())
    if not rows:
        raise ValueError("AUD shared-memory rows not parsed")
    values = {name: [] for name in ("recv", "drop", "current", "total", "ratio")}
    for thread, shm_type, recv, drop, current, total, ratio_percent in rows:
        labels = {"thread": thread, "type": shm_type}
        values["recv"].append(sample("apt_aud_recv_total", int(recv), labels))
        values["drop"].append(sample("apt_aud_drop_total", int(drop), labels))
        values["current"].append(sample("apt_aud_current_count", int(current), labels))
        values["total"].append(sample("apt_aud_total_count", int(total), labels))
        values["ratio"].append(sample("apt_aud_usage_ratio", f"{float(ratio_percent) / 100:.9f}", labels))
    lines = metric("apt_aud_recv_total", "AUD cumulative received packets.", "counter", values["recv"])
    lines += metric("apt_aud_drop_total", "AUD cumulative dropped packets.", "counter", values["drop"])
    lines += metric("apt_aud_current_count", "AUD current shared-memory ring count.", "gauge", values["current"])
    lines += metric("apt_aud_total_count", "AUD shared-memory ring capacity.", "gauge", values["total"])
    lines += metric("apt_aud_usage_ratio", "AUD shared-memory ring usage ratio.", "gauge", values["ratio"])
    return lines


def main():
    started = time.monotonic()
    lines = []
    statuses = {}
    sources = (
        ("filesystem", lambda: collect_filesystems()),
        ("vpp_interface", lambda: collect_vpp_interfaces(run([VPPCTL, "show", "interface"]))),
        ("superflows", lambda: collect_superflows(run([DPDK_CLIENT, "--connstat"]))),
        ("mdbstat", lambda: collect_mdbstat(run([DPDK_CLIENT, "--mdbstat"]))),
        ("aud", lambda: collect_aud(run([VPPCTL, "show", "ids", "shm", "info"]))),
    )
    for source, collector in sources:
        try:
            lines.extend(collector())
            statuses[source] = 1
        except Exception:
            statuses[source] = 0
    status_values = [
        sample("apt_custom_collector_source_success", success, {"source": source})
        for source, success in statuses.items()
    ]
    lines += metric(
        "apt_custom_collector_source_success",
        "Whether a custom collector source succeeded in the last run.",
        "gauge",
        status_values,
    )
    lines += metric(
        "apt_custom_collector_scrape_duration_seconds",
        "Custom collector execution duration.",
        "gauge",
        [sample("apt_custom_collector_scrape_duration_seconds", f"{time.monotonic() - started:.6f}")],
    )
    lines += metric(
        "apt_custom_collector_scrape_timestamp_seconds",
        "Unix timestamp of the custom collector run.",
        "gauge",
        [sample("apt_custom_collector_scrape_timestamp_seconds", int(time.time()))],
    )
    os.makedirs(os.path.dirname(OUTPUT), mode=0o755, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".apt_vpp_dpdk.", dir=os.path.dirname(OUTPUT), text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
        os.chmod(temporary, 0o644)
        os.replace(temporary, OUTPUT)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if not all(statuses.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
