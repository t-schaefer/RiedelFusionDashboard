#!/usr/bin/env bash
# Creates the "tools" VM (Docker + Tailscale host for the Fusion demo and further tools) on a Proxmox VE node
# from the Ubuntu 24.04 cloud image.
# Run as root in the node's shell (Proxmox UI > node > Shell):
#   bash proxmox-create-vm.sh <vmid> "<ssh public key>"
# Example: bash proxmox-create-vm.sh 106 "ssh-ed25519 AAAA... me@mac"
# Sizing via environment: MEM (MB, default 4096) CORES (2) DISK (40G) NAME (tools)
# The VM gets DHCP on vmbr0, user "thorsten" with key login only, and the QEMU guest agent.
set -euo pipefail

VMID="${1:?usage: $0 <vmid> \"<ssh public key>\"}"
SSH_KEY="${2:?usage: $0 <vmid> \"<ssh public key>\"}"
NAME="${NAME:-tools}"
MEM="${MEM:-4096}"
CORES="${CORES:-2}"
DISK="${DISK:-40G}"
VM_USER="${VM_USER:-thorsten}"
BRIDGE="${BRIDGE:-vmbr0}"
DISK_STORAGE="${DISK_STORAGE:-local-lvm}"
IMG_URL="https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img"
IMG="noble-server-cloudimg-amd64.qcow2"
NODE="$(hostname)"

# 'local' must hold cloud images (import) and the first-boot file (snippets)
content=$(pvesh get /storage/local --output-format json | python3 -c 'import sys,json;print(json.load(sys.stdin)["content"])')
for c in import snippets; do
  echo "$content" | grep -qw "$c" || content="$content,$c"
done
pvesm set local --content "$content"

if ! pvesm list local --content import | grep -q "$IMG"; then
  sum=$(curl -fsSL "$(dirname "$IMG_URL")/SHA256SUMS" | awk '/noble-server-cloudimg-amd64.img$/{print $1}')
  pvesh create "/nodes/$NODE/storage/local/download-url" --content import --filename "$IMG" \
    --url "$IMG_URL" --checksum "$sum" --checksum-algorithm sha256
fi

# First boot installs the guest agent, so commands can run through Proxmox without the console
mkdir -p /var/lib/vz/snippets
cat > /var/lib/vz/snippets/tools-vendor.yaml <<'EOF'
#cloud-config
packages:
  - qemu-guest-agent
runcmd:
  - systemctl enable --now qemu-guest-agent
EOF

keyfile=$(mktemp)
echo "$SSH_KEY" > "$keyfile"
qm create "$VMID" --name "$NAME" --memory "$MEM" --cores "$CORES" --cpu host --ostype l26 \
  --scsihw virtio-scsi-single --scsi0 "$DISK_STORAGE:0,import-from=local:import/$IMG,discard=on,iothread=1" \
  --ide2 "$DISK_STORAGE:cloudinit" --boot order=scsi0 --net0 "virtio,bridge=$BRIDGE" \
  --agent enabled=1 --serial0 socket --vga serial0 --onboot 1 \
  --ciuser "$VM_USER" --sshkeys "$keyfile" --ipconfig0 ip=dhcp \
  --cicustom vendor=local:snippets/tools-vendor.yaml \
  --description "Tools host: Fusion demo and further tools (Docker). Access via Tailscale."
rm -f "$keyfile"
qm resize "$VMID" scsi0 "$DISK"
qm start "$VMID"

echo "Waiting for the guest agent (first boot installs it) ..."
for i in $(seq 1 60); do
  qm agent "$VMID" ping >/dev/null 2>&1 && break
  sleep 5
done
ip=$(qm agent "$VMID" network-get-interfaces 2>/dev/null | python3 -c '
import sys, json
for i in json.load(sys.stdin):
    for a in i.get("ip-addresses", []):
        if a["ip-address-type"] == "ipv4" and not a["ip-address"].startswith("127."):
            print(a["ip-address"]); sys.exit()' || true)
echo "VM $VMID ($NAME) is up${ip:+ at $ip}. Next: deploy/vm-bootstrap.sh on the VM, then deploy/deploy-to-vm.sh (docs/TOOLS-VM.md)."
