<#
.SYNOPSIS
  Updates this RiedelFusionDashboard checkout from git and restarts the
  PLS Fusion Dashboard service.

.NOTES
  Run from an elevated (Administrator) PowerShell, from the repo root (the
  folder that contains this script and the fusion-dashboard-service folder):

      cd C:\path\to\RiedelFusionDashboard
      powershell -ExecutionPolicy Bypass -File .\update-windows.ps1

  Requires git to be installed and this folder to be a git clone of
  https://github.com/t-schaefer/RiedelFusionDashboard - if it isn't, this
  script exits with an error instead of guessing.

  devices.json (the live device list) is not tracked in git, so `git pull`
  never touches it - only server.py, dashboard.html and the scripts get
  updated.
#>

$ErrorActionPreference = "Stop"

$RepoDir = $PSScriptRoot
$TaskName = "PLSFusionDashboardService"

if (-not (Test-Path (Join-Path $RepoDir ".git"))) {
    throw "This folder ($RepoDir) is not a git checkout. Run this script from the root of a 'git clone' of the RiedelFusionDashboard repo, or update by copying files manually (see README.txt under 'Upgrading')."
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    throw "git was not found on PATH. Install Git for Windows (https://git-scm.com/download/win) and re-run this script."
}

$taskExists = [bool](Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)
if ($taskExists) {
    Write-Host "Stopping task '$TaskName'..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue

    # Task Scheduler can keep reporting a task as "Running" for a few
    # seconds after Stop-ScheduledTask, and won't start a new instance
    # while it still thinks one is active. Wait briefly, then fall back to
    # killing the python process directly so the restart below always works
    # (this is why "only running install-task-windows.ps1 by hand helps" -
    # that script fully re-registers the task, which clears this state;
    # this does the equivalent by force-killing the stuck process instead).
    $deadline = (Get-Date).AddSeconds(15)
    while ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running" -and (Get-Date) -lt $deadline) {
        Start-Sleep -Milliseconds 500
    }
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        Write-Host "Task still reports Running after 15s - stopping the python process directly..." -ForegroundColor Yellow
        Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.CommandLine -like "*server.py*" } |
            ForEach-Object {
                Write-Host "  Killing PID $($_.ProcessId)"
                Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
            }
        Start-Sleep -Seconds 1
    }
} else {
    Write-Host "Task '$TaskName' not found - continuing anyway (nothing to stop)." -ForegroundColor Yellow
}

Push-Location $RepoDir
try {
    Write-Host "Fetching and pulling latest changes..."
    git pull --ff-only
    if ($LASTEXITCODE -ne 0) {
        throw "git pull failed (exit code $LASTEXITCODE) - see output above. The service was NOT restarted; fix the git issue and re-run this script."
    }
} finally {
    Pop-Location
}

if ($taskExists) {
    Write-Host "Starting task '$TaskName'..."
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Task state: $state"
    if ($state -ne "Running") {
        Write-Host "Task did not report Running - check service.log in fusion-dashboard-service\ for errors." -ForegroundColor Yellow
    }
} else {
    Write-Host "No existing task to restart - run install-task-windows.ps1 from fusion-dashboard-service\ if this is a first-time install." -ForegroundColor Yellow
}

Write-Host ""
Write-Host "Update complete."
