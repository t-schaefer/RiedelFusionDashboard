# The "tools" VM

One small Ubuntu VM on Proxmox that hosts the Fusion demo and every further tool, one Docker Compose project each
under `/opt/tools/<name>`. Reachable only through Tailscale; public parts go out through Tailscale Funnel.

1. **VM** - in the Proxmox node shell (UI > node > Shell):
   `bash proxmox-create-vm.sh 106 "<your ssh public key>"` (pick a free VM id; MEM/CORES/DISK can be set).
2. **Bootstrap** - on the VM: `bash vm-bootstrap.sh` (Docker, Tailscale, updates; open the printed login link yourself).
3. **Fusion demo** - on the Mac in the repo: `deploy/deploy-to-vm.sh` (copies the files, starts the demo on 8090, Funnel).
   The VM's Tailscale name is `tools-fusion`, so the demo is at `https://tools-fusion.<tailnet>.ts.net` and SSH is `thorsten@tools-fusion`.

Adding a tool later: its own folder `/opt/tools/<name>` with a compose file and its own port; publish it with
`tailscale funnel` (or `tailscale serve` for tailnet-only). Port 8090 is used by the Fusion demo.
