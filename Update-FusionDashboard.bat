@echo off
setlocal enabledelayedexpansion

rem Double-click launcher for update-windows.ps1: updates this checkout from
rem git and restarts the PLS Fusion Dashboard service, with no manual
rem PowerShell/Administrator steps needed. Works wherever this repo was
rem cloned to - it locates update-windows.ps1 relative to its own location
rem rather than assuming a fixed path.

set "SCRIPT_DIR=%~dp0"
set "TARGET=%SCRIPT_DIR%update-windows.ps1"

if not exist "%TARGET%" (
    rem Fall back to one level up, in case this .bat ended up copied into a
    rem subfolder instead of staying at the repo root next to the script.
    set "TARGET=%SCRIPT_DIR%..\update-windows.ps1"
)

if not exist "!TARGET!" (
    echo Could not find update-windows.ps1 next to this file, or one folder up.
    echo Expected it at:
    echo   %SCRIPT_DIR%update-windows.ps1
    echo Make sure this .bat file is placed in the root of the RiedelFusionDashboard
    echo git checkout, alongside update-windows.ps1.
    pause
    exit /b 1
)

rem Self-elevate if not already running as Administrator (Stop/Start-ScheduledTask
rem on the SYSTEM-owned task needs it) - this triggers the normal Windows UAC prompt.
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges - approve the prompt that appears...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "!TARGET!"

echo.
pause
