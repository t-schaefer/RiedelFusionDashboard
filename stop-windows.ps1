<#
.SYNOPSIS
  Stops the PLS Fusion Dashboard service: the Scheduled Task "PLSFusionDashboardService" (waits until it is
  stopped) and any python still running fusion-dashboard-service\server.py.
  It starts again at the next boot, with Start-FusionDashboard.bat or Update-FusionDashboard.bat.
  Usually started by double-clicking Stop-FusionDashboard.bat (self-elevates).
#>
$ErrorActionPreference = "Stop"
$TaskName = "PLSFusionDashboardService"
$ServerPy = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "fusion-dashboard-service\server.py"

if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
    Write-Host "Stopping task '$TaskName' ..."
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    for ($i = 0; $i -lt 30; $i++) {
        if ((Get-ScheduledTask -TaskName $TaskName).State -ne "Running") { break }
        Start-Sleep -Seconds 1
    }
    Write-Host "Task state: $((Get-ScheduledTask -TaskName $TaskName).State)"
} else {
    Write-Host "Task '$TaskName' is not installed." -ForegroundColor Yellow
}
# Match this service's own server.py by full path - other tools on this PC also run a server.py.
Get-CimInstance Win32_Process -Filter "Name like 'python%'" |
    Where-Object { $_.CommandLine -and $_.CommandLine -like "*$ServerPy*" } |
    ForEach-Object {
        Write-Host "Stopping python (PID $($_.ProcessId)) running $ServerPy"
        Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
    }
Write-Host "Stopped. Start again: Start-FusionDashboard.bat" -ForegroundColor Green
