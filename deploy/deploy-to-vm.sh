#!/usr/bin/env bash
# From the Mac, in the repository:  deploy/deploy-to-vm.sh [ssh-host]     (default: thorsten@tools)
# Copies the committed files to the VM (no GitHub login needed there), (re)starts the demo on port 8090
# and publishes it with Tailscale Funnel.
set -euo pipefail
HOST="${1:-thorsten@tools}"
DIR=/opt/tools/fusion-dashboard
cd "$(git rev-parse --show-toplevel)"

git archive HEAD | ssh "$HOST" "mkdir -p $DIR && tar -x -C $DIR"
ssh "$HOST" "cd $DIR && docker compose -f deploy/docker-compose.tunnel.yml up -d --build && sudo tailscale funnel --bg 8090 && tailscale funnel status"
