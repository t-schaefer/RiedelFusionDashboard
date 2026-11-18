# Hosting the public demo

The demo (`server.py --demo`) runs completely on **simulated devices**: it never talks to a real
network, needs no login, and starts over by itself every 30 minutes. This setup puts it on the
internet behind HTTPS.

## What you need
- A server with Docker (a VM is ideal; see "Where to run it").
- A **fixed public IPv4** and a DNS name: an **A record** `demo.<your-domain>` -> that IPv4.
- Ports **80 and 443** forwarded from the router/firewall to that server (80 is needed for the certificate).

## Start
```bash
export DEMO_HOST=demo.example.com
docker compose -f deploy/docker-compose.yml up -d --build
```
Open `https://demo.example.com/guide`. Update: `git pull` and the same command again.
Logs: `docker compose -f deploy/docker-compose.yml logs -f app`.

## What protects the public demo
- Simulated devices only (the real network calls are replaced; the "check this computer" panel shows made-up interfaces).
- No login and no real data: everything lives in a throw-away folder inside the container, rebuilt on every start.
- The whole demo restarts every 30 minutes (`--reset-every`, Docker `restart: always`); all visitors share one state.
- At most 600 requests per minute per visitor (429 beyond that), 1 MB request bodies, HTTPS headers (Caddy).
- The app container publishes no port; only Caddy is reachable, and it runs without extra privileges.

## Where to run it
Do **not** run it on a machine inside your office/production network: an open port 443 leads straight to it.
Better, in this order:
1. A small VM in a **separate VLAN / DMZ** with no route into the internal network.
2. A cheap VPS (Hetzner, ~4 EUR/month) - no router changes, no inside access at all.
3. **Cloudflare Tunnel** instead of port forwarding (no open ports, no fixed IP needed).

## Not covered
- One shared state for all visitors (per-visitor sessions are on the roadmap).
- The real tool is not hosted: it must run on the engineer's laptop next to the device.
