#!/usr/bin/env bash
# Discover Coral Edge TPU PCI devices on the Talos node (vendor 1ac1, device 089a).
#
# Talos config: TALOSCONFIG is set by mise (.mise.toml) to
#   {{config_root}}/talos/clusterconfig/talosconfig
# and by Taskfile to ROOT_DIR/talos/clusterconfig/talosconfig. Use the repo root
# or ensure TALOSCONFIG points at your cluster's talosconfig.
#
# Usage (from repo root; mise sets TALOSCONFIG automatically):
#   ./talos/scripts/coral-pci-discovery.sh
# Optional override endpoint/node (default: 192.168.1.5):
#   ENDPOINT=192.168.1.5 NODE=192.168.1.5 ./talos/scripts/coral-pci-discovery.sh

set -e
ROOT_DIR="${ROOT_DIR:-$(cd "$(dirname "$0")/../.." && pwd)}"
TALOSCONFIG="${TALOSCONFIG:-$ROOT_DIR/talos/clusterconfig/talosconfig}"
NODE="${NODE:-192.168.1.5}"
ENDPOINT="${ENDPOINT:-$NODE}"

export TALOSCONFIG

echo "Using TALOSCONFIG=$TALOSCONFIG"
echo "Node: $NODE  Endpoint: $ENDPOINT"
echo "Listing Coral PCI devices (vendor 0x1ac1, device 0x089a) on node..."
echo ""

# Run on node: list PCI sysfs dirs and filter by vendor/device; print in order (pci:0, pci:1, ...)
CMD='
for d in /sys/bus/pci/devices/*; do
  [ -f "$d/vendor" ] && [ -f "$d/device" ] || continue
  v=$(cat "$d/vendor" 2>/dev/null)
  p=$(cat "$d/device" 2>/dev/null)
  if [ "$v" = "0x1ac1" ] && [ "$p" = "0x089a" ]; then
    echo "$d"
  fi
done
'

# Run command on node (here-string for non-interactive shell)
output=$(talosctl --endpoints "$ENDPOINT" --nodes "$NODE" shell <<< "$CMD" 2>/dev/null || true)
echo "$output" | grep -E '^/sys/bus/pci/devices/' | sort -t: -k2 -k3 -k4 -n | nl -v 0 -w 1 -s ': ' | while read -r line; do
  idx="${line%%:*}"
  path="${line#*: }"
  echo "  pci:$idx  ->  $path"
done

# If no devices found, print manual instructions
if ! echo "$output" | grep -qE '^/sys/bus/pci/devices/'; then
  echo "(No Coral devices found, or talosctl failed. Check TALOSCONFIG and connectivity.)"
  echo ""
  echo "Manual: talosctl --endpoints $ENDPOINT --nodes $NODE shell"
  echo "Then run: $CMD"
fi

echo ""
echo "Use in Frigate: first device = device: pci:0, second = device: pci:1"
