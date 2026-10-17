@echo off
rem Stops the PLS Fusion Dashboard service (Scheduled Task PLSFusionDashboardService). It starts again at the
rem next boot, with Start-FusionDashboard.bat or Update-FusionDashboard.bat.
rem Self-elevates via UAC, pauses so the result stays visible.
setlocal
set "HERE=%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%stop-windows.ps1"
echo.
pause
