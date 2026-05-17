# Talos / HPE DL380 Gen10 — unexpected reboot investigation

Use this runbook when the Talos node (`k3s`, `192.168.1.5`) reboots without an obvious cause. **iLO logs survive reboots; Talos `dmesg` does not** unless logging to Loki is configured (see [machine-logging.yaml](../talos/patches/global/machine-logging.yaml)).

## Quick triage (5 minutes)

| Source | What it tells you |
|--------|-------------------|
| **iLO IML** | Power loss, ASR, thermal, PSU, memory, POST failures |
| **iLO last reset reason** | Who initiated the reset (OS, BMC, user, watchdog) |
| **AHS bundle** | Correlated hardware timeline (best single artifact) |
| **Grafana → Loki** | `{job="talos"}` — last Talos service logs before reboot |
| **Grafana → Prometheus** | `NodeRecentlyRebooted`, EDAC, Redfish PSU/fan alerts |
| **Talos (current boot only)** | `mise exec -- talosctl -n 192.168.1.5 dmesg \| tail -200` |

## 1. iLO web UI

1. Open `https://<ilo-ip>/` (dedicated iLO NIC or shared management port).
2. **Information → Integrated Management Log (IML)** — filter **Critical** and **Caution**, note timestamps vs last known uptime.
3. **Information → Active Health System** — download **AHS log** (90 days if available).
4. **Power and Thermal** — check PSU status, fan redundancy, inlet/exhaust temps at time of event.
5. **Administration → Firmware** — note iLO and BIOS versions (see §5).

Upload the AHS file to [HPE AHS Viewer](https://servicecenter.hpe.com/ahs/) and open the **Self repair** / **Insight Diagnostics** tab.

## 2. iLO SSH (HPE CLI)

```bash
ssh Administrator@<ilo-ip>
```

```text
# Integrated Management Log (hardware events)
show /system1/log1

# iLO event log
show /map1/log1

# Last reset reason (POST, ASR, power button, etc.)
show /system1

# PSU / power regulator / cap events
show /system1/oemhpe_power1

# Thermal sensors
show /system1/OemHpe/HealthData1/ThermalData1
```

Look for entries such as:

- `Automatic Server Recovery` / `ASR` — OS hung, iLO reset the host
- `Power Supply` / `Input lost` — PSU or PDU issue
- `Temperature` / `Fan` — cooling failure
- `Memory` / `Correctable/Uncorrectable` — DIMM faults
- `Uncorrectable Machine Check` — CPU/memory bus

## 3. Redfish (optional, from workstation)

```bash
ILO=https://<ilo-ip>
curl -sk -u "Administrator:$ILO_PASS" "$ILO/redfish/v1/Systems/1" | jq '.LastResetTime, .PowerState, .Status'
curl -sk -u "Administrator:$ILO_PASS" "$ILO/redfish/v1/Managers/1/LogServices/IEL/Entries" | jq '.Members[] | {Created, Severity, Message}'
```

In-cluster: Prometheus scrapes `redfish-exporter` (see `kubernetes/apps/observability/redfish-exporter/`).

## 4. Talos (in-band, current boot)

```bash
cd /path/to/home-ops
mise exec -- talosctl -n 192.168.1.5 service          # uptime since last boot
mise exec -- talosctl -n 192.168.1.5 read /proc/uptime
mise exec -- talosctl -n 192.168.1.5 dmesg | grep -iE 'mce|panic|oom|hardware|thermal|edac|watchdog|reset'
mise exec -- talosctl -n 192.168.1.5 read /sys/fs/pstore  # after kernel-panic tuning + upgrade
```

## 5. Firmware baseline (DL380 Gen10)

| Component | This host (snapshot) | Action |
|-----------|---------------------|--------|
| BIOS | U30 07/20/2023 | Compare with latest at [HPE Support](https://support.hpe.com) — memory/CPU stability fixes |
| iLO 5 | check in UI | Target **2.93+**; older 2.7x had spurious iLO watchdog / NMI issues |

**BIOS settings to verify:**

- **Power Regulator:** Static High Performance (matches `cpufreq.default_governor=performance` in schematic)
- **ASR (Automatic Server Recovery):** note timeout — explains “mystery” reboots after OS hang
- **Workload Profile:** if not performance, thermal throttling may increase under load

## 6. After observability stack is deployed

### Grafana Loki

```logql
{job="talos"} |= "error"
{job="talos", talos_service="machined"}
```

Correlate log timestamps with IML entries (± timezone).

### Prometheus alerts

- `NodeRecentlyRebooted` — fires within 10 min of boot
- `NodeMemoryEDACUncorrectable` / `NodeMemoryEDACCorrectable` — ECC
- `RedfishPSUFailed`, `RedfishFanFailed`, `RedfishThermalCritical` — iLO health

### Controlled test (maintenance window)

```bash
mise exec -- talosctl -n 192.168.1.5 reboot --debug
```

Confirm shutdown lines appear in Loki **before** the node returns.

## 7. Talos config changes in this repo

| File | Purpose | Apply |
|------|---------|-------|
| [machine-logging.yaml](../talos/patches/global/machine-logging.yaml) | Stream Talos logs → Alloy TCP :12345 | `task talos:generate-config` + `task talos:apply-node IP=192.168.1.5` — **no reboot** |
| [machine-kernel-watchdog.yaml](../talos/patches/global/machine-kernel-watchdog.yaml) | Panic on oops/NMI | Same apply-config |
| [schematic.yaml](../talos/schematic.yaml) | `pstore.backend=acpi_erst`, `printk.time=1` | Rebuild installer image + `task talos:upgrade-node IP=192.168.1.5` — **requires reboot** |

## 8. Bitwarden prerequisite (Redfish exporter)

Create Bitwarden Secrets Manager item **`ilo-redfish`** with fields:

- `REDFISH_USERNAME` — e.g. `Administrator`
- `REDFISH_PASSWORD` — iLO password
- `REDFISH_HOST` — iLO IP or hostname (e.g. `192.168.1.10`)

Flux `ExternalSecret` in `redfish-exporter` syncs these into the cluster.

## Decision tree

```text
IML shows power/PSU     → PDU, PSU, cabling, iLO power cap
IML shows thermal/fan    → datacenter cooling, fan module, dust
IML shows memory/MCE     → run HPE memory test, reseat DIMMs, replace faulty DIMM
IML shows ASR/watchdog   → OS hang: check Loki pre-reboot, ZFS, OOM, GPU/TPU drivers
IML empty, host reboots  → check ASR timeout, iLO firmware, BIOS updates
Loki shows OOM/panic     → kernel/software; correlate with Prometheus OOMKilled alerts
```

Update this document when the root cause is found.
