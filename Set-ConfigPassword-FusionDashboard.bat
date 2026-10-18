@echo off
rem Sets (or changes) the login password of the Config page (http://<this PC>:8090/config).
rem Stored only as a salted PBKDF2 hash in fusion-dashboard-service\auth.json (not in git).
rem Takes effect for new logins immediately, no restart needed.
rem Moving over from the old MuoN Config Tool? Copy its config-tool\auth.json to
rem fusion-dashboard-service\auth.json to keep the same password.
cd /d "%~dp0fusion-dashboard-service"
python server.py --set-password
echo.
pause
