<#
.SYNOPSIS
  Start / stop / restart / status / open / edit the device list / show the log
  of the PLS Fusion Dashboard service (the Scheduled Task), from a small menu
  or with a single action:

      powershell -ExecutionPolicy Bypass -File .\control-windows.ps1 [-Action menu|status|start|stop|restart|open|config|log|update]

  Normally started by double-clicking Control-FusionDashboard.bat, which asks
  for Administrator rights (needed to control the SYSTEM-owned task).
#>
param(
    [ValidateSet("menu", "status", "start", "stop", "restart", "open", "config", "log", "update")]
    [string]$Action = "menu"
)

$ErrorActionPreference = "Stop"

$RepoDir = $PSScriptRoot
$TaskName = "PLSFusionDashboardService"
$ServiceDir = Join-Path $RepoDir "fusion-dashboard-service"
$ServerPy = Join-Path $ServiceDir "server.py"
$DevicesJson = Join-Path $ServiceDir "devices.json"
$LogFile = Join-Path $ServiceDir "service.log"

$Port = 8090
$portLine = Select-String -Path $ServerPy -Pattern '^PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portLine) { $Port = [int]$portLine.Matches[0].Groups[1].Value }
$Url = "http://localhost:$Port/"

function Get-DashboardTask {
    $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if (-not $task) {
        throw "Task '$TaskName' is not installed. Run install-task-windows.ps1 from the fusion-dashboard-service folder first."
    }
    return $task
}

function Show-Status {
    $state = (Get-DashboardTask).State
    Write-Host "Task '$TaskName': $state"
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3
        Write-Host "Web page $Url answers (HTTP $($r.StatusCode))." -ForegroundColor Green
    } catch {
        Write-Host "Web page $Url does not answer." -ForegroundColor Yellow
    }
}

function Stop-Dashboard {
    Get-DashboardTask | Out-Null
    Write-Host "Stopping task '$TaskName'..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    # Task Scheduler can keep reporting "Running" for a few seconds; wait,
    # then kill this service's own python process (matched by the full
    # server.py path - other tools on this PC also run a server.py).
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running" -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        Write-Host "Still running after 15s - stopping the python process directly..." -ForegroundColor Yellow
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*$ServerPy*" } |
            ForEach-Object {
                Write-Host "  Killing PID $($_.ProcessId)"
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
        Start-Sleep -Seconds 1
    }
    Write-Host "Stopped. The dashboard page is offline until you start it again."
}

function Start-Dashboard {
    Get-DashboardTask | Out-Null
    Write-Host "Starting task '$TaskName'..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Task state: $state"
    if ($state -ne "Running") {
        Write-Host "Task did not report Running - check the log (menu option 7)." -ForegroundColor Yellow
    } else {
        Write-Host "Dashboard: $Url"
    }
}

function Edit-DeviceList {
    # Stop first: the service rewrites devices.json itself (e.g. "+ Device"),
    # which would overwrite a hand edit made while it runs.
    if (-not (Test-Path $DevicesJson)) {
        throw "devices.json not found at $DevicesJson - start the service once so it gets created."
    }
    Stop-Dashboard
    $backup = "$DevicesJson.bak"
    Copy-Item $DevicesJson $backup -Force
    Write-Host "Opening devices.json in Notepad - save and close it to continue (backup: devices.json.bak)."
    Start-Process notepad.exe -ArgumentList "`"$DevicesJson`"" -Wait
    try {
        $null = Get-Content $DevicesJson -Raw | ConvertFrom-Json
    } catch {
        Copy-Item $backup $DevicesJson -Force
        Write-Host "devices.json is not valid JSON - the previous version was restored." -ForegroundColor Red
    }
    Start-Dashboard
}

function Show-Log {
    if (Test-Path $LogFile) {
        Get-Content $LogFile -Tail 40
    } else {
        Write-Host "No service.log yet at $LogFile"
    }
}

function Update-Dashboard {
    & (Join-Path $RepoDir "update-windows.ps1")
}

function Invoke-Action($name) {
    switch ($name) {
        "status"  { Show-Status }
        "start"   { Start-Dashboard }
        "stop"    { Stop-Dashboard }
        "restart" { Stop-Dashboard; Start-Dashboard }
        "open"    { Start-Process $Url }
        "config"  { Edit-DeviceList }
        "log"     { Show-Log }
        "update"  { Update-Dashboard }
    }
}

if ($Action -ne "menu") {
    Invoke-Action $Action
    return
}

while ($true) {
    Write-Host ""
    Write-Host "PLS Fusion Dashboard - service control ($Url)" -ForegroundColor Cyan
    Write-Host "  1  Status"
    Write-Host "  2  Start"
    Write-Host "  3  Stop"
    Write-Host "  4  Restart"
    Write-Host "  5  Open dashboard in browser"
    Write-Host "  6  Config: edit device list (stops, opens Notepad, restarts)"
    Write-Host "  7  Show log (last 40 lines)"
    Write-Host "  8  Update from git (and restart)"
    Write-Host "  Q  Quit"
    $choice = (Read-Host "Choose").Trim().ToLower()
    $map = @{ "1" = "status"; "2" = "start"; "3" = "stop"; "4" = "restart"; "5" = "open"; "6" = "config"; "7" = "log"; "8" = "update" }
    if ($choice -eq "q") { break }
    if ($map.ContainsKey($choice)) {
        try { Invoke-Action $map[$choice] } catch { Write-Host $_.Exception.Message -ForegroundColor Red }
    }
}
