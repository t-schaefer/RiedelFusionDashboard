#!/usr/bin/env bash
# Run ONCE on the new VM (as the normal user, with sudo):  bash vm-bootstrap.sh
# Installs Docker (Ubuntu packages) and Tailscale, enables automatic security updates.
# Tailscale then prints a login link - open it yourself and sign in (nobody types the password for you).
set -euo pipefail

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y docker.io docker-compose-v2 unattended-upgrades curl
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"

if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi
sudo tailscale up --hostname "${TS_HOSTNAME:-tools}" --ssh

sudo mkdir -p /opt/tools && sudo chown "$USER" /opt/tools
echo "Done. Log out and in once (docker group). Tools live in /opt/tools/<name>."
