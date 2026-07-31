#!/usr/bin/env python3
"""Install a standalone Node Exporter probe and register it in Prometheus."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import re
import sys
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paramiko


VERSION = "1.12.1"
SERVICE_NAME = "pg-node-exporter.service"
REMOTE_ROOT = "/opt/pg-node-exporter"
TEXTFILE_DIR = "/var/lib/node_exporter/textfile_collector"
PROM_CONFIG = "/opt/prometheus-grafana-monitoring/prometheus/prometheus.yml"


def run(client: paramiko.SSHClient, command: str, timeout: int = 30, check: bool = True) -> str:
    _, stdout, stderr = client.exec_command(command, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    if check and stdout.channel.recv_exit_status() != 0:
        raise RuntimeError(f"remote command failed: {command}\n{out}{err}")
    return out.strip()


def connect(host: str, user: str, password: str, sock=None) -> paramiko.SSHClient:
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(host, username=user, password=password, timeout=15, sock=sock)
    return client


def connect_through_center(
    center: paramiko.SSHClient, target_host: str, target_user: str, target_password: str
) -> paramiko.SSHClient:
    transport = center.get_transport()
    if transport is None or not transport.is_active():
        raise RuntimeError("monitoring center SSH transport is not active")
    channel = transport.open_channel(
        "direct-tcpip", (target_host, 22), ("127.0.0.1", 0), timeout=15
    )
    return connect(target_host, target_user, target_password, sock=channel)


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


def arch_name(raw: str) -> str:
    mapping = {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "armv7", "armv6l": "armv6"}
    if raw not in mapping:
        raise RuntimeError(f"unsupported target architecture: {raw}")
    return mapping[raw]


def download_binary(arch: str, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    archive = cache_dir / f"node_exporter-{VERSION}.linux-{arch}.tar.gz"
    if not archive.exists():
        url = f"https://github.com/prometheus/node_exporter/releases/download/v{VERSION}/{archive.name}"
        print(f"Downloading {url}")
        urllib.request.urlretrieve(url, archive)
    with tarfile.open(archive, "r:gz") as tf:
        member = next((m for m in tf.getmembers() if m.name.endswith("/node_exporter")), None)
        if member is None:
            raise RuntimeError("node_exporter binary not found in archive")
        target = cache_dir / f"node_exporter-{VERSION}-{arch}"
        with tf.extractfile(member) as src, target.open("wb") as dst:
            assert src is not None
            dst.write(src.read())
    target.chmod(0o755)
    return target


def copy_from_center(
    center: paramiko.SSHClient,
    target: paramiko.SSHClient,
    center_path: str,
    target_path: str,
) -> None:
    source_sftp = center.open_sftp()
    target_sftp = target.open_sftp()
    try:
        with source_sftp.open(center_path, "rb") as source, target_sftp.open(target_path, "wb") as destination:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                destination.write(chunk)
    finally:
        source_sftp.close()
        target_sftp.close()


def install_probe(
    client: paramiko.SSHClient,
    center: paramiko.SSHClient,
    target_ip: str,
    center_ip: str,
    staged_binary: str,
    vpp_dir: Path | None,
) -> None:
    arch = run(client, "uname -m")
    run(client, "command -v systemctl >/dev/null")
    listen_ip = run(client, f"ip -4 route get {shell_quote(center_ip)} | sed -n 's/.* src \\([0-9.]*\\).*/\\1/p' | head -1", check=False)
    listen_ip = listen_ip or target_ip
    port_info = run(client, "ss -ltnp '( sport = :9100 )' 2>/dev/null || true", check=False)
    existing = run(client, f"systemctl is-active node_exporter.service 2>/dev/null || true", check=False)
    if existing == "active":
        exec_start = run(client, "systemctl show -p ExecStart --value node_exporter.service", check=False)
        if f"{REMOTE_ROOT}/node_exporter" not in exec_start:
            raise RuntimeError("TCP 9100 is occupied by an existing node_exporter.service outside this bundle; refusing to modify it")
        metrics = run(client, f"curl -fsS http://{listen_ip}:9100/metrics | grep -E 'node_cpu_seconds_total|node_memory_MemTotal_bytes' | head -2")
        print(f"Existing bundle probe already active on {listen_ip}:9100\n{metrics}")
        return
    if port_info:
        raise RuntimeError(f"TCP 9100 is already occupied on {target_ip}; refusing to stop or replace it:\n{port_info}")

    run(client, f"install -d -m 0755 {REMOTE_ROOT} {TEXTFILE_DIR}")
    run(client, "id -u node_exporter >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter")
    copy_from_center(center, client, staged_binary, f"{REMOTE_ROOT}/node_exporter.new")
    run(client, f"install -o root -g root -m 0755 {REMOTE_ROOT}/node_exporter.new {REMOTE_ROOT}/node_exporter")

    unit = f"""[Unit]
Description=Prometheus Node Exporter probe
After=network-online.target
Wants=network-online.target

[Service]
User=node_exporter
Group=node_exporter
ExecStart={REMOTE_ROOT}/node_exporter --web.listen-address={listen_ip}:9100 --collector.textfile.directory={TEXTFILE_DIR}
Restart=on-failure
RestartSec=5s
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths={TEXTFILE_DIR}

[Install]
WantedBy=multi-user.target
"""
    sftp = client.open_sftp()
    with sftp.open(f"/tmp/{SERVICE_NAME}", "w") as f:
        f.write(unit)
    if vpp_dir and run(client, "test -x /data/vpp/vppids/vppctl.sh && echo yes || true", check=False) == "yes":
        sftp.put(str(vpp_dir / "vpp-ids-slab-collector.py"), "/tmp/vpp-ids-slab-collector.py")
        sftp.put(str(vpp_dir / "vpp-ids-slab-collector.service"), "/tmp/vpp-ids-slab-collector.service")
        sftp.put(str(vpp_dir / "vpp-ids-slab-collector.timer"), "/tmp/vpp-ids-slab-collector.timer")
    sftp.close()
    run(client, f"install -m 0755 /tmp/{SERVICE_NAME} /etc/systemd/system/{SERVICE_NAME}; rm -f /tmp/{SERVICE_NAME}")
    if vpp_dir:
        run(client, "if [ -f /tmp/vpp-ids-slab-collector.py ]; then install -m 0755 /tmp/vpp-ids-slab-collector.py /usr/local/sbin/vpp-ids-slab-collector.py; install -m 0644 /tmp/vpp-ids-slab-collector.service /etc/systemd/system/vpp-ids-slab-collector.service; install -m 0644 /tmp/vpp-ids-slab-collector.timer /etc/systemd/system/vpp-ids-slab-collector.timer; rm -f /tmp/vpp-ids-slab-collector.*; fi")
    run(client, "systemctl daemon-reload")
    run(client, f"systemctl enable --now {SERVICE_NAME}")
    if vpp_dir:
        run(client, "if [ -f /usr/local/sbin/vpp-ids-slab-collector.py ]; then systemctl enable --now vpp-ids-slab-collector.timer; fi")
    metrics = run(client, f"curl -fsS http://{listen_ip}:9100/metrics | grep -E 'node_cpu_seconds_total|node_memory_MemTotal_bytes' | head -2")
    print(f"Probe active on {listen_ip}:9100 ({arch})\n{metrics}")


def update_prometheus(center: paramiko.SSHClient, target_ip: str, display_name: str) -> None:
    sftp = center.open_sftp()
    with sftp.open(PROM_CONFIG, "r") as f:
        original = f.read().decode()
    endpoint = f"{target_ip}:9100"
    if endpoint in original:
        print("Prometheus target already exists; leaving configuration unchanged")
        sftp.close()
        return
    block = f"      - targets:\n          - {endpoint}\n        labels:\n          host: {target_ip}\n          display_name: {display_name}\n"
    lines = original.splitlines(keepends=True)
    start = next((i for i, line in enumerate(lines) if line.startswith("  - job_name: node")), None)
    if start is None:
        raise RuntimeError("node scrape job not found in Prometheus configuration")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("  - job_name:")), len(lines))
    new_config = "".join(lines[:end]) + block + "".join(lines[end:])
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
    backup = f"{PROM_CONFIG}.bak.{stamp}"
    with sftp.open(backup, "w") as f:
        f.write(original.encode())
    with sftp.open(PROM_CONFIG, "w") as f:
        f.write(new_config.encode())
    sftp.close()
    try:
        run(center, "docker exec pg-monitor-prometheus promtool check config /etc/prometheus/prometheus.yml")
    except Exception:
        sftp = center.open_sftp()
        with sftp.open(backup, "r") as src, sftp.open(PROM_CONFIG, "w") as dst:
            dst.write(src.read())
        sftp.close()
        raise
    run(center, "curl -fsS -X POST http://127.0.0.1:9090/-/reload >/dev/null")
    deadline = time.time() + 45
    while time.time() < deadline:
        raw = run(center, "curl -fsS http://127.0.0.1:9090/api/v1/targets")
        data = json.loads(raw)
        found = [t for t in data["data"]["activeTargets"] if t.get("labels", {}).get("instance") == endpoint]
        if found and found[0].get("health") == "up":
            print(f"Prometheus target UP: {endpoint}")
            return
        time.sleep(3)
    raise RuntimeError(f"Prometheus target did not become UP: {endpoint}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-ip", required=True)
    parser.add_argument("--target-user", default="root")
    parser.add_argument("--target-password", default=None)
    parser.add_argument("--center-ip", default="192.168.33.35")
    parser.add_argument("--center-user", default="root")
    parser.add_argument("--center-password", default=None)
    parser.add_argument("--display-name", default=None)
    parser.add_argument("--version", default=VERSION)
    args = parser.parse_args()
    if not args.target_password:
        args.target_password = getpass.getpass(f"SSH password for {args.target_user}@{args.target_ip}: ")
    if not args.center_password:
        args.center_password = getpass.getpass(f"SSH password for {args.center_user}@{args.center_ip}: ")
    if args.version != VERSION:
        raise SystemExit("This bundle currently supports node_exporter v1.12.1 only")
    display = args.display_name or f"probe-{args.target_ip.split('.')[-1]}"
    cache = Path(__file__).with_name(".probe-cache")
    center = connect(args.center_ip, args.center_user, args.center_password)
    target = None
    staged_binary = None
    try:
        target = connect_through_center(center, args.target_ip, args.target_user, args.target_password)
        arch = arch_name(run(target, "uname -m"))
        binary = download_binary(arch, cache)
        staged_binary = f"/tmp/pg-probe-node_exporter-{VERSION}-{arch}"
        center_sftp = center.open_sftp()
        center_sftp.put(str(binary), staged_binary)
        center_sftp.close()
        run(center, f"chmod 0600 {shell_quote(staged_binary)}")
        print(f"Probe package staged on monitoring center: {args.center_ip}:{staged_binary}")
        vpp_dir = Path(__file__).parent / "probes" / "192.168.33.177"
        install_probe(
            target,
            center,
            args.target_ip,
            args.center_ip,
            staged_binary,
            vpp_dir if vpp_dir.exists() else None,
        )
        update_prometheus(center, args.target_ip, display)
        print("Deployment completed through the selected monitoring center without restarting Docker or other containers")
    finally:
        if staged_binary:
            run(center, f"rm -f {shell_quote(staged_binary)}", check=False)
        if target:
            target.close()
        center.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
