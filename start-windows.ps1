<#
.SYNOPSIS
  Starts the PLS Fusion Dashboard service (Scheduled Task "PLSFusionDashboardService").
  If the task is not installed yet, runs the dashboard in this window instead (Ctrl+C stops it).
  Usually started by double-clicking Start-FusionDashboard.bat (self-elevates).
#>
$ErrorActionPreference = "Stop"
$TaskName = "PLSFusionDashboardService"
$ServiceDir = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "fusion-dashboard-service"
$ServerPy = Join-Path $ServiceDir "server.py"

$Port = 8090
$portLine = Select-String -Path $ServerPy -Pattern '^PORT\s*=\s*(\d+)' -ErrorAction SilentlyContinue | Select-Object -First 1
if ($portLine) { $Port = [int]$portLine.Matches[0].Groups[1].Value }
$Url = "http://localhost:$Port/"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    if ((Get-ScheduledTask -TaskName $TaskName).State -eq "Running") {
        Write-Host "Task '$TaskName' is already running." -ForegroundColor Yellow
    } else {
        Write-Host "Starting task '$TaskName' ..."
        Start-ScheduledTask -TaskName $TaskName
        Start-Sleep -Seconds 3
    }
    $state = (Get-ScheduledTask -TaskName $TaskName).State
    Write-Host "Task state: $state"
    if ($state -ne "Running") {
        Write-Host "Task did not report Running - check fusion-dashboard-service\service.log." -ForegroundColor Yellow
    } else {
        Write-Host "Dashboard: $Url   (other PCs: http://$($env:COMPUTERNAME):$Port/)" -ForegroundColor Green
    }
} else {
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
        throw "Python was not found on PATH. Install Python 3.8+ from https://www.python.org/downloads/windows/ and tick 'Add python.exe to PATH'."
    }
    Write-Host "Task '$TaskName' is not installed - running the dashboard in this window (Ctrl+C stops it)." -ForegroundColor Yellow
    Write-Host "Dashboard: $Url"
    Set-Location $ServiceDir
    python server.py
}
