<#
.SYNOPSIS
  Installs the PLS Fusion Dashboard service as a Windows Scheduled Task
  that runs under the SYSTEM account - starts at boot, keeps running whether
  or not any user is logged in, and restarts itself if it ever crashes.

.NOTES
  Run this script from an elevated (Administrator) PowerShell, from inside
  this same folder (so the paths below resolve correctly):

      cd C:\path\to\fusion-dashboard-service
      powershell -ExecutionPolicy Bypass -File .\install-task-windows.ps1

  Uses only built-in Windows features (Task Scheduler) - no NSSM, no
  third-party service wrapper needed.
#>

$ErrorActionPreference = "Stop"

$TaskName = "PLSFusionDashboardService"
$ScriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$ServerPy = Join-Path $ScriptDir "server.py"
$Port = 8090

if (-not (Test-Path $ServerPy)) {
    throw "server.py not found next to this script ($ServerPy). Run this from inside the fusion-dashboard-service folder."
}

# Locate a Python interpreter. pythonw.exe runs without a console window;
# fall back to python.exe if that's all that's available.
$python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command python.exe -ErrorAction SilentlyContinue }
if (-not $python) {
    Write-Host "Python was not found on PATH." -ForegroundColor Yellow
    Write-Host "Install it from https://www.python.org/downloads/windows/ (check 'Add python.exe to PATH')," -ForegroundColor Yellow
    Write-Host "or, if you cannot run an installer here, download the 'Windows embeddable package'" -ForegroundColor Yellow
    Write-Host "from the same page, unzip it anywhere, and re-run this script with that folder on PATH." -ForegroundColor Yellow
    throw "Python not found."
}
Write-Host "Using Python at: $($python.Source)"

# Allow inbound connections to the dashboard port so it can be viewed from
# other machines on the network, not just locally.
$fwRuleName = "PLS Fusion Dashboard (TCP $Port)"
if (-not (Get-NetFirewallRule -DisplayName $fwRuleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $fwRuleName -Direction Inbound -Protocol TCP -LocalPort $Port -Action Allow | Out-Null
    Write-Host "Firewall rule created for TCP port $Port."
} else {
    Write-Host "Firewall rule for TCP port $Port already exists."
}

$action = New-ScheduledTaskAction -Execute $python.Source -Argument "`"$ServerPy`"" -WorkingDirectory $ScriptDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero)

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Existing task '$TaskName' found - replacing it."
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
}

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings `
    -Description "PLS Fusion Dashboard - polls the Fusion device fleet and serves a web dashboard on port $Port. Runs as SYSTEM, no user login required." | Out-Null

Write-Host "Task '$TaskName' registered. Starting it now..."
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 3

$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Host "Task state: $state"
Write-Host ""
Write-Host "Dashboard should now be reachable at: http://localhost:$Port/"
Write-Host "From another machine on the network:  http://<this-PC-hostname-or-IP>:$Port/"
Write-Host "Logs: $ScriptDir\service.log"
