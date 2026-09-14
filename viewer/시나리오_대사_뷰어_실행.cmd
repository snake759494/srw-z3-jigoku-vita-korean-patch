@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0.."
if not defined SIOK_VIEWER_PORT set "SIOK_VIEWER_PORT=8765"
set "VIEWER_PATH=/viewer/%%EC%%8B%%9C%%EB%%82%%98%%EB%%A6%%AC%%EC%%98%%A4_%%EB%%8C%%80%%EC%%82%%AC_%%EB%%B7%%B0%%EC%%96%%B4.html"
set "VIEWER_URL=http://127.0.0.1:%SIOK_VIEWER_PORT%%VIEWER_PATH%?build=%RANDOM%%RANDOM%"

where py >nul 2>nul
if %errorlevel%==0 (
  start "SRW Z Scenario Viewer" "%VIEWER_URL%"
  py scripts\scenario_viewer_server.py --port %SIOK_VIEWER_PORT%
  exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
  start "SRW Z Scenario Viewer" "%VIEWER_URL%"
  python scripts\scenario_viewer_server.py --port %SIOK_VIEWER_PORT%
  exit /b %errorlevel%
)

echo Python 3 was not found. Install Python 3 and run this file again.
pause
exit /b 1
