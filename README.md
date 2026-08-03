# APT Prometheus Monitoring

Prometheus + Grafana server monitoring stack exported from the active
monitoring center at `192.168.33.35` on 2026-07-31.

## Features

- Prometheus scrapes server metrics every 15 seconds.
- Grafana server overview with a per-host selector.
- CPU, memory, disk, load, network throughput, packet loss and error panels.
- VPP IDS Slab memory panel for targets with the custom textfile collector.
- Directory usage panels for `/home`, `/data` and `/data_pts` when present.
- Superflows connection gauges from `dpdkClient --connstat`.
- VPP, MDB collector and AUD packet-loss rate panels derived from cumulative
  counters.
- Five-minute sustained packet-loss/error alert export.
- Grafana iframe embedding with anonymous Viewer access for private-network
  integration.
- Standalone systemd probe deployment through a selected monitoring center;
  Docker is not required on probe servers.

## Current targets

- Monitoring center: `192.168.33.35`
- Probe: `192.168.33.177`
- Probe: `192.168.33.226`
- Probe: `192.168.33.238`

Edit `prometheus/prometheus.yml` or use `deploy_probe.py` to add another target.

## Start the monitoring center

Create the Grafana admin password file first:

```powershell
New-Item -ItemType Directory -Force .\secrets | Out-Null
Set-Content -NoNewline .\secrets\grafana_admin_password 'CHANGE_THIS_PASSWORD'
```

Then start the isolated Compose project:

```sh
docker compose up -d
docker compose ps
```

Endpoints:

- Grafana: `http://<monitoring-center>:3000`
- Prometheus: `http://<monitoring-center>:9090`

Grafana anonymous access is Viewer-only and is enabled for iframe embedding.
Keep ports 3000 and 9090 restricted to the trusted private network.

## Deploy a standalone probe

Install the local dependency:

```sh
python -m pip install paramiko
```

Run the deployment script. Passwords are prompted without echoing:

```powershell
python .\deploy_probe.py `
  --target-ip 192.168.33.177 `
  --target-user root `
  --center-ip 192.168.33.35 `
  --center-user root `
  --display-name probe-177
```

The selected monitoring center is used as the SSH jump host and temporary
package staging point. The script refuses to replace another process on TCP
9100, validates the Prometheus configuration, and performs a hot reload without
restarting Docker or unrelated services.

## VPP IDS Slab collector

The optional collector files are under `probes/vpp-ids-slab`. The deployment
script installs them automatically when the target contains:

```text
/data/vpp/vppids/vppctl.sh
```

The additional VPP/DPDK collector is under `probes/vpp-dpdk`. It runs every 15
seconds and invokes the read-only commands `vppctl show interface`,
`dpdkClient --connstat`, `dpdkClient --mdbstat` and `vppctl show ids shm info`.
It writes metrics to the Node Exporter textfile collector and does not change
VPP or DPDK runtime state.

## Alert export

The active Grafana-managed alert was exported to:

```text
grafana/alerting/network-packet-loss-5m.json
```

It detects interface receive/transmit drops, interface errors, and UDP receive
buffer errors sustained for five minutes. On a fresh Grafana data volume,
import this file through Grafana alerting provisioning or the provisioning API.

## Export provenance

`docs/source-manifest-20260731.json` records the source host, exclusions and
SHA-256 checksums. Passwords, Grafana/Prometheus data volumes, runtime databases
and historical backup files are intentionally excluded.
