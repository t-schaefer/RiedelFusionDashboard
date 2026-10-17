@echo off
rem Starts the PLS Fusion Dashboard service (Scheduled Task PLSFusionDashboardService). If the task is not
rem installed yet it runs the dashboard in this window instead (Ctrl+C stops it) - for a first test.
rem Self-elevates via UAC, pauses so the result stays visible.
setlocal
set "HERE=%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%start-windows.ps1"
echo.
pause
