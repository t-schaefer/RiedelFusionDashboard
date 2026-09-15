# RiedelFusionDashboard

Dashboard for monitoring many Nevion/Macnica Fusion ST2110 devices at once (health, config, syslog, reboot).

Two ways to run it, in this repo:

## `fusion-dashboard-service/` (recommended)

A small always-on background service (Python standard library only, no pip
installs needed). Polls the whole fleet server-side and serves a web
dashboard on port 8090 - runs as a Windows Scheduled Task under the SYSTEM
account, so it keeps working whether or not anyone is logged into that PC.
View it from any browser on the network.

Features: fleet health table (temp, PTP, links, warnings, NMOS), global
pause/resume of polling, per-device reboot, and pushing one syslog target
(server/port/enable + PTP/temperature/decap monitoring flags) to every
device at once.

See [`fusion-dashboard-service/README.txt`](fusion-dashboard-service/README.txt)
for install/setup instructions.

### Upgrading a deployed instance

If the Windows PC's copy is a `git clone` of this repo, double-click
[`Update-FusionDashboard.bat`](Update-FusionDashboard.bat) in the repo root
to pull the latest version and restart the service - it asks for
Administrator rights via the normal Windows UAC prompt, then runs
[`update-windows.ps1`](update-windows.ps1) for you and pauses at the end so
you can read the output.

To run it from a PowerShell prompt instead:

```powershell
cd C:\path\to\RiedelFusionDashboard
powershell -ExecutionPolicy Bypass -File .\update-windows.ps1
```

`fusion-dashboard-service/devices.json` (the live device list) is not
tracked in git - only `devices.example.json` is, used to seed a fresh
`devices.json` on first run - so pulling updates never touches a site's own
device list.

## `standalone/fusionDashboard.html`

A single, fully self-contained HTML file (no server, no dependencies) - open
it directly in a browser on a PC that's on the same network as the Fusion
devices. Good for a quick one-off check without setting up the service.
Device list and settings are stored per-browser (localStorage).

## `docs/fusion-api.json`

Reference documentation of the Fusion device REST API
(`/emsfp/node/v1/...`) that both tools talk to - endpoints, sample
responses, and which parts are confirmed vs. inferred.
