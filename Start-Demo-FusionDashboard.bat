@echo off
rem Starts the Fusion Dashboard in DEMO mode: simulated devices, nothing real is touched, no login needed.
rem One factory-fresh unit to set up with New device (MAC 40:A3:6B:A2:16:14) and two finished ones in monitoring.
rem Runs in this window (no service, no admin rights) on http://127.0.0.1:8190/ - close the window to stop it.
rem All demo data lives in fusion-dashboard-service\demo-data and is wiped at every start.
setlocal
set "HERE=%~dp0"
cd /d "%HERE%fusion-dashboard-service"

where python >nul 2>&1
if %errorlevel% neq 0 (
    echo Python was not found on PATH.
    echo Install Python 3.8+ from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

echo Preparing the demo (takes a few seconds), then the browser opens ...
start "" cmd /c "timeout /t 25 /nobreak >nul & start http://127.0.0.1:8190/guide"
python server.py --demo
echo.
pause
