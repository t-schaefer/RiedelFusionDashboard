@echo off
setlocal enabledelayedexpansion

rem Double-click launcher for control-windows.ps1: a small menu to start, stop,
rem restart and check the PLS Fusion Dashboard service, open it in the browser,
rem edit the device list and show the log. Optional argument for one-shot use
rem without the menu:  Control-FusionDashboard.bat start|stop|restart|status|open|config|log|update

set "SCRIPT_DIR=%~dp0"
set "TARGET=%SCRIPT_DIR%control-windows.ps1"

if not exist "%TARGET%" (
    set "TARGET=%SCRIPT_DIR%..\control-windows.ps1"
)

if not exist "!TARGET!" (
    echo Could not find control-windows.ps1 next to this file, or one folder up.
    echo Make sure this .bat file is placed in the root of the RiedelFusionDashboard
    echo git checkout, alongside control-windows.ps1.
    pause
    exit /b 1
)

rem Self-elevate (controlling the SYSTEM-owned task needs Administrator).
net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator privileges - approve the prompt that appears...
    if "%~1"=="" (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    ) else (
        powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%~1' -Verb RunAs"
    )
    exit /b
)

if "%~1"=="" (
    powershell -NoProfile -ExecutionPolicy Bypass -File "!TARGET!"
) else (
    powershell -NoProfile -ExecutionPolicy Bypass -File "!TARGET!" -Action %~1
    echo.
    pause
)
