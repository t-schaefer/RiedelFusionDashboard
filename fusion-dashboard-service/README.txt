PLS Fusion Dashboard - background service
============================================

What this is
-------------
A small always-on service that polls your fleet of Fusion ST2110 devices
(107 seeded in devices.json) and serves a web dashboard showing health and
config for all of them. It is designed to run as a Windows background
service under the SYSTEM account, so it keeps running whether or not anyone
is logged into that PC - view it from any browser on the network instead.

Files
-----
server.py                    the service itself (Python standard library only, no pip installs needed)
dashboard.html                the web page it serves (talks only to this service, never directly to the Fusion devices)
devices.json                  the LIVE device list (ip, name, tag) - not tracked in git, so a
                              `git pull` update never touches it. Edit it by hand or via the
                              "+ Device" form in the dashboard. Created automatically on first
                              run from devices.example.json if it doesn't exist yet.
devices.example.json          the tracked starting-point device list - only used to seed a
                              fresh devices.json on first run
devices.seeded.json         (auto-created, not in git) remembers which devices.example.json
                              entries were already offered to devices.json - see "New devices"
install-task-windows.ps1      one-time setup: registers the Windows Scheduled Task
uninstall-task-windows.ps1    removes the scheduled task and firewall rule again
../update-windows.ps1         (repo root) pulls the latest version from git and restarts the
                              service - see "Upgrading" below

Requirements
------------
Python 3.8 or newer on the Windows PC that will run the service.
Check with (PowerShell):
    python --version
    py --version
    where.exe python

If Python is missing:
- Normal install: https://www.python.org/downloads/windows/
  (tick "Add python.exe to PATH" during setup)
- If you can't run an installer (locked-down PC): download the "Windows
  embeddable package" from the same page instead, unzip it anywhere (e.g.
  C:\python), and add that folder to PATH. The embeddable package includes
  everything this script needs (it only uses Python's standard library).

Install as a service (run once, as Administrator)
--------------------------------------------------
1. Copy this whole folder to the target Windows PC.
2. Open PowerShell as Administrator.
3. cd into this folder.
4. Run:
       powershell -ExecutionPolicy Bypass -File .\install-task-windows.ps1
5. Open http://localhost:8090/ in a browser on that PC, or
   http://<that-PC's-hostname-or-IP>:8090/ from any other PC on the network.

The script:
- Finds your Python installation automatically.
- Opens Windows Firewall for TCP port 8090 (inbound), so other PCs can view it.
- Registers a Scheduled Task that starts at boot, runs as SYSTEM (no login
  needed), and restarts automatically if the process ever exits.
- Starts the task immediately, so you don't need to reboot to try it.

Uninstalling
------------
Open PowerShell as Administrator, cd into this folder, run:
    powershell -ExecutionPolicy Bypass -File .\uninstall-task-windows.ps1

Upgrading
---------
If this folder is a git checkout of the RiedelFusionDashboard repo, go one
level up to the repo root and double-click Update-FusionDashboard.bat (it
will ask for Administrator rights via the normal Windows prompt). Or run
update-windows.ps1 yourself from a PowerShell prompt:
    cd ..
    powershell -ExecutionPolicy Bypass -File .\update-windows.ps1
Either way, this stops the scheduled task, runs `git pull`, and starts it
again. devices.json is not tracked in git, so your local device list is
never touched by an update.

If this is not a git checkout (files were copied manually), stop the task,
replace server.py and dashboard.html with the newer versions, leave
devices.json alone, then start the task again:
    Stop-ScheduledTask -TaskName PLSFusionDashboardService
    ... copy the new files over ...
    Start-ScheduledTask -TaskName PLSFusionDashboardService

New devices (from an update)
----------------------------
devices.json is local, but when an update adds devices to devices.example.json
(e.g. the OC.IP/S MuoN cards), the service appends them to devices.json by
itself on its next start - nothing to do besides the normal update. It only
adds IPs the example gained since the last start, so a device you deleted
from devices.json by hand stays deleted. (The first start after upgrading to
this version offers every example device that's missing from devices.json
once, then remembers it in devices.seeded.json.)

Device Type column: read from the device itself. Devices that were never
reachable since the service started (offline) show "-" until they answer once.

Changing settings
------------------
Near the top of server.py:
    PORT                          web UI port (default 8090)
    POLL_INTERVAL_SEC             how often health is refreshed per device (default 30s)
    CONCURRENCY                   how many devices are polled at once (default 8)
    REQUEST_TIMEOUT_SEC           per-request timeout for health polls (default 4s)
    CONFIG_REFRESH_INTERVAL_SEC   how often the slower config data (firmware,
                                  license, ports, NMOS) is refreshed (default 600s)
After changing these, restart the task:
    Restart-ScheduledTask -TaskName PLSFusionDashboardService
(or just reboot - it starts automatically).

Red/Blue network failover
---------------------------
Every request to a device (health poll, config fetch, reboot, syslog
changes) tries the device's Red (primary) address first - the one in
devices.json - and automatically retries over Blue (backup) if Red doesn't
answer. The Blue address is derived, not configured: same IP with the
second octet incremented by one (e.g. 10.101.4.158 red -> 10.102.4.158
blue), matching this fleet's SMPTE 2022-7 style addressing. The dashboard
outlines whichever of the Red/Blue badges is currently in use, and the
device detail panel shows a blue banner when it's talking to a device over
Blue only.

Color bars and stream rates (per device, on demand)
------------------------------------------------------
In the expanded device row, "Color bars" and "Stream rates" each start
collapsed with a "Load" button - unlike everything else in this file, they
are NOT fetched by the background poll, only when you click Load, because
either one is several extra calls per device and nobody needs live data for
a device nobody is looking at right now. Click "Refresh" to update; the
loaded data is cached per-browser-tab so it survives the 4s auto-refresh.

Color bars: one checkbox per SDI output, mirroring the old
fusionFunctions.js setColorBarInfo(). Turning one ON puts a test pattern on
that output in place of the live picture, so it asks for confirmation
first - only do this if you know that output isn't on air.

Stream rates: live packet rate / sequence-error count per flow
(/telemetry/devices), grouped by logical channel: a "CH01 RX" header row
(RX = decapsulator/receiver, TX = encapsulator/sender) with all of that
channel's streams under it, both legs (primary / secondary) each. Streams
are named by channel and essence: CH01 - V01 (video), CH01 - A01..A04
(audio), CH01 - D01 (data / ancillary) - the n-th engine of that essence in
the order the device lists them. The short flow ID is shown too (hover for
the full one). Each CH group is a toggle: click its header row to expand /
collapse it (all collapsed by default; the header always shows the flow
count and the channel's total sequence errors, in red when above 0, and its
Clr / Reset buttons work while collapsed). Open/closed state is kept
across the 4s auto-refresh.

Flow resets (in the Stream rates table / header)
  Clr          per flow: clears just that flow's packet / sequence-error
               counters (/self/diag/flow/{id} reset_counter). The stream
               itself is not touched.
  Reset        per flow: resets the flow itself (same endpoint, "reset"
               flag). Can glitch that one stream while it re-locks - asks
               for a stronger confirmation.
  Clr CHxx / Reset CHxx (channel header row): the same, for EVERY flow of
               that channel at once - all its streams, primary + secondary.
               Done in one request by the service, which reads the
               channel's flows fresh from the device; the confirmation
               shows how many flows were accepted and the channel's total
               sequence errors before -> after.
  Enable CHxx (channel header row): switches ON every flow of that channel
               (12 on a Fusion: V, A1-A4, D, pri + sec) in one go by writing
               {"network": {"enable": "1"}} to /flows/{id} - the same write
               the old flow editor does, verified on a live Fusion. The
               device silently ignores it while a flow has no destination
               address (0.0.0.0 = not routed yet), so every flow is read back
               afterwards and the dialog lists the ones that stayed off and
               why. Flows already on are left alone. Enabling does NOT route:
               it only switches on what is already addressed (by Cerebrum /
               NMOS). A receiver then joins the multicast groups set on its
               flows, a sender starts sending.
  Reset all counters (button next to Load/Refresh): clears the counters
               of every flow on that Fusion at once (/self/system
               counters_reset), for when you don't want to go flow by flow.
Every reset asks for confirmation first. IMPORTANT: none of these writes
were ever done by the old tools, so the request bodies are inferred from
the field names and have NOT been confirmed on a live unit. To make that
visible rather than silent, a per-flow reset reports the flow's counters
before and after (and warns if the device still shows a flag stuck at 1);
the raw result is also in service.log. Try it once on a flow you can
afford to touch - if the numbers don't drop, note what the device
answered (service.log) so the request body can be adjusted.

Dashboard controls
-------------------
SDI bitrate (per device, expanded row, left of Color bars) - the global SDI
    operating bit rate of one Fusion: integer (50/60 Hz families), fractional
    (59.94 / 29.97 = 1000/1001 families) or auto (follow the signal). Shows
    what is set and what the device is actually running; pick a value and
    click Apply (asks first - the SDI outputs may re-lock). The device is read
    back afterwards and the dialog says if it did not take the value. Same
    POST as the old config tool's "Set Bitrate" (/sdi/ {"configuration":
    {"operating_bit_rate": ...}}); "auto" is new in this tool but is the value
    about half of the fleet is already set to. Not on MuoN cards (no /sdi).
    The value is read with the 10-minute config refresh.
    Also shown as the "SDI rate" column (sortable / filterable): the set value,
    with the actually running one in brackets when it differs, e.g.
    "auto (integer)". "-" = MuoN card or not read yet.

Group buttons (bar above the table, "Group: All / GFX / ...") - show one device group:
    GFX, ST1, CS Rooms, REPLAY, ASX, DCR/DCB, GR, MC, OC, PCR, SCR
    (plus "Other" if a device name matches none). The group comes from the
    device name (CS1..CS29 = CS Rooms, DCR + DCB = DCR/DCB, OC.IP/HI +
    OC.IP/S = OC, ...; see GROUP_RULES in dashboard.html). It filters the
    table, the summary badges and the warnings list, and is remembered in
    this browser. Each button shows the group's device count and, in red,
    how many are offline. Combines with the text filter / column filters.

Device rows - click a row (blue arrow on the left, row highlights on hover)
    to open its details: ports, color bars, stream rates, resets, reboot.
Pause polling (global) - top-right button. Stops the background health/config
    sweeps for the whole fleet until you resume it (state persists in the
    running service, not per-browser). Use this if you need to quiet things
    down, e.g. while debugging something else on the network.

Remove from dashboard (per device, in the expanded row) - drops the device from
    devices.json and stops polling it; the device itself is not touched.
    Asks first. A device that came from devices.example.json stays removed
    after a restart or update (devices.seeded.json remembers it was offered);
    add it back any time with "+ Device".

Reboot (per device, in the expanded row) - sends a reboot command to that one
    Fusion unit. This interrupts its SDI/IP signal briefly. A confirmation
    dialog is shown before anything is sent. IMPORTANT: the underlying API
    call (POST self/system {"reboot":"1"}) is inferred from the field name
    self/system already returns, not confirmed against a live unit - the
    only way to confirm it is to actually try it, so test it once on a
    device you know is safe to take down before relying on it operationally.

Syslog (all devices) - toolbar button, opens a form to push one syslog
    server/port/enable setting, plus PTP-event and temperature-event
    monitoring flags, to every device currently in the fleet list at once.
    Server field defaults to 10.12.64.24 (the address already seen in use).
    Also unverified against a live unit for the same reason as Reboot
    (POST self/syslog {"config":{"server","port","enable"},"monitoring":
    {"common":{"ptp_event","temp_event"}}}) - test on one device first.
    Applies with the same CONCURRENCY-limited fan-out as health polling, so
    it won't hit all devices in the same instant, but it IS a real config
    change on every device that responds - there is no
    per-device confirmation step, only the one fleet-wide confirmation
    dialog, so double-check the server/port before clicking Apply.

Warning severity (Warn column)
-------------------------------
The Warn column distinguishes real warnings from informational flags:
    yellow badge   real warning (device status "warning")
    red "offline"  both links down
    grey "N info"  informational flag only - the device stays green/online

Currently the only code ever seen on the fleet is 1001, classified as info
(WARNING_SEVERITY in server.py; add a code there with "warning" or "info" to
change how it is treated - unknown codes default to "warning"). 1001 is NOT
a "stream down" alarm: it means the timing (line_offset) status flag of one
SDI output is set, and 20 of 21 affected outputs in a fleet scan still had
video arriving. The dashboard names the output without any extra queries,
e.g. "SDI 8 (CH08): output timing status flag set (1001)", since the output
UUIDs are the same on every device (SDI 1/2/3/4/6/8 = b0/b1/b2/b3/b5/b7).
The summary bar shows a grey "N info" badge that opens the warnings panel.
"Show only problems" ignores info-only devices. Real stream-down detection
would need extra per-device polling and was deliberately not added.

Temperature colour thresholds
-------------------------------
In the Temp column of the dashboard (dashboard.html, search "tempColor"):
    green   below 70C
    yellow  70C and above
    red     73C and above

Many viewers at once
---------------------
Viewers never talk to the Fusion devices - they only read the service's cached
state, so any number of open browser tabs costs the fleet nothing. Each open
tab fetches /api/state every 4s (~1.2 MB of JSON). The service builds that
once per second and shares it (gzip-compressed, ~50 KB) between all viewers,
and accepts bursts of up to 128 pending connections. Tested here: 200
simultaneous clients, no errors, ~30 ms median. Per-device actions (Load
color bars / stream rates, resets, reboot) do hit the device, once per click.

Why polling is safe for the fleet
----------------------------------
- Health comes from a single lightweight endpoint per device
  (/telemetry/node), not the many small calls the old per-device config page
  used.
- Only CONCURRENCY devices are ever polled at the same instant; the rest
  queue, so 107 devices never turn into 107 simultaneous requests.
- A full sweep across the fleet always finishes before the next one starts.
- Every request has a hard timeout, so one unresponsive device can't stall
  the others.
- Firmware/license/port/NMOS config barely changes, so it's refreshed far
  less often than health (every 10 minutes by default, not every poll).

Logs
----
service.log in this folder (rotates automatically, keeps the last few MB).
Check it if the dashboard looks stuck or a device won't show up.
