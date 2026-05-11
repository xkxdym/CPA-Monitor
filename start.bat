@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set PYTHONUTF8=1

where python >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Python not found in PATH.
  pause
  exit /b 1
)

start "" "http://127.0.0.1:8088"
python server.py

endlocal
