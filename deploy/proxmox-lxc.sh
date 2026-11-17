#!/usr/bin/env bash
# Create a small Debian LXC on a Proxmox VE host and install MediaFinder in it.
# Run this ON THE PROXMOX HOST (as root):
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/jthy10/MediaFinder/main/deploy/proxmox-lxc.sh)"
#
# Override defaults with environment variables, e.g.:
#   CTID=120 STORAGE=local-zfs BRIDGE=vmbr0 MEMORY=512 bash proxmox-lxc.sh
set -euo pipefail

CTID="${CTID:-$(pvesh get /cluster/nextid)}"
HOSTNAME="${HOSTNAME_CT:-mediafinder}"
STORAGE="${STORAGE:-local-lvm}"          # where the container disk lives
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
DISK_GB="${DISK_GB:-4}"
MEMORY="${MEMORY:-512}"
CORES="${CORES:-1}"
BRIDGE="${BRIDGE:-vmbr0}"
INSTALL_URL="${INSTALL_URL:-https://raw.githubusercontent.com/jthy10/MediaFinder/main/deploy/install.sh}"

command -v pct >/dev/null || { echo "This must run on a Proxmox VE host (pct not found)."; exit 1; }
[ "$(id -u)" -eq 0 ] || { echo "Run as root."; exit 1; }

echo "==> Finding a Debian 12 template"
pveam update >/dev/null
TEMPLATE="$(pveam available --section system | awk '/debian-12-standard/ {print $2}' | sort -V | tail -1)"
[ -n "$TEMPLATE" ] || { echo "No debian-12 template found via pveam."; exit 1; }
if ! pveam list "$TEMPLATE_STORAGE" | grep -q "$TEMPLATE"; then
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE"
fi

echo "==> Creating container $CTID ($HOSTNAME)"
pct create "$CTID" "$TEMPLATE_STORAGE:vztmpl/$TEMPLATE" \
  --hostname "$HOSTNAME" --cores "$CORES" --memory "$MEMORY" --swap 256 \
  --rootfs "$STORAGE:$DISK_GB" \
  --net0 "name=eth0,bridge=$BRIDGE,ip=dhcp" \
  --unprivileged 1 --onboot 1 --features nesting=0

pct start "$CTID"

echo "==> Waiting for the container network"
for _ in $(seq 1 30); do
  pct exec "$CTID" -- getent hosts deb.debian.org >/dev/null 2>&1 && break
  sleep 2
done

echo "==> Installing MediaFinder"
pct exec "$CTID" -- bash -c "apt-get update -qq && apt-get install -y -qq curl ca-certificates >/dev/null && curl -fsSL '$INSTALL_URL' | bash"

IP="$(pct exec "$CTID" -- hostname -I | awk '{print $1}')"
echo
echo "Done. Container $CTID is up: http://$IP:8080"
echo "Tip: give it a DHCP reservation / static IP. NAS shares can be added over SMB in the UI;"
echo "to browse a folder mounted on the Proxmox host instead, bind-mount it, e.g.:"
echo "  pct set $CTID -mp0 /mnt/pve/nas,mp=/mnt/nas,ro=1"
