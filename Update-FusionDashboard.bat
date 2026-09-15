@echo off
setlocal

rem Double-click launcher for update-windows.ps1: updates this checkout from
rem git and restarts the PLS Fusion Dashboard service, with no manual
rem PowerShell/Administrator steps needed.

rem Self-elevate if not already running as Administrator (Stop/Start-ScheduledTask
rem on the SYSTEM-owned task needs it) - this triggers the normal Windows UAC prompt.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-windows.ps1"

echo.
pause
