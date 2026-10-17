@echo off
rem Edit the device list: stops the service, opens fusion-dashboard-service\devices.json in Notepad, checks it is
rem valid JSON (restores the backup if not) and starts the service again.
rem Self-elevates via UAC, pauses so the result stays visible.
setlocal
set "HERE=%~dp0"

net session >nul 2>&1
if %errorlevel% neq 0 (
    echo Requesting administrator rights...
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
    exit /b
)

powershell -NoProfile -ExecutionPolicy Bypass -File "%HERE%config-windows.ps1"
echo.
pause
