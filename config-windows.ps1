<#
.SYNOPSIS
  Edit the device list of the PLS Fusion Dashboard: stops the service, opens
  fusion-dashboard-service\devices.json in Notepad, checks it is valid JSON (restores the backup
  devices.json.bak if not) and starts the service again. The service rewrites devices.json itself
  (e.g. "+ Device"), so a hand edit made while it runs would be lost - hence the stop.
  Usually started by double-clicking Config-FusionDashboard.bat (self-elevates).
#>
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DevicesJson = Join-Path $Root "fusion-dashboard-service\devices.json"

if (-not (Test-Path $DevicesJson)) {
    throw "devices.json not found at $DevicesJson - start the service once (Start-FusionDashboard.bat) so it gets created."
}

& (Join-Path $Root "stop-windows.ps1")

$backup = "$DevicesJson.bak"
Copy-Item $DevicesJson $backup -Force
Write-Host ""
Write-Host "Opening devices.json in Notepad - save and close it to continue (backup: devices.json.bak)."
Start-Process notepad.exe -ArgumentList "`"$DevicesJson`"" -Wait

try {
    $null = Get-Content $DevicesJson -Raw | ConvertFrom-Json
    Write-Host "devices.json is valid." -ForegroundColor Green
} catch {
    Copy-Item $backup $DevicesJson -Force
    Write-Host "devices.json is not valid JSON - the previous version was restored." -ForegroundColor Red
}

& (Join-Path $Root "start-windows.ps1")
