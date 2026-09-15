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

## `standalone/fusionDashboard.html`

A single, fully self-contained HTML file (no server, no dependencies) - open
it directly in a browser on a PC that's on the same network as the Fusion
devices. Good for a quick one-off check without setting up the service.
Device list and settings are stored per-browser (localStorage).

## `docs/fusion-api.json`

Reference documentation of the Fusion device REST API
(`/emsfp/node/v1/...`) that both tools talk to - endpoints, sample
responses, and which parts are confirmed vs. inferred.
