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

Dashboard controls
-------------------
Pause polling (global) - top-right button. Stops the background health/config
    sweeps for the whole fleet until you resume it (state persists in the
    running service, not per-browser). Use this if you need to quiet things
    down, e.g. while debugging something else on the network.

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

Temperature colour thresholds
-------------------------------
In the Temp column of the dashboard (dashboard.html, search "tempColor"):
    green   below 70C
    yellow  70C and above
    red     73C and above

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
