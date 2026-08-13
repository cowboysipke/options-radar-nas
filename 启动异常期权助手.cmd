@echo off
setlocal
cd /d "%~dp0"
title Options Radar ±¾µØÃæ°å

set "OPTIONS_RADAR_BUILD_VERSION=v2-local"
for /f "tokens=*" %%G in ('git rev-parse --short HEAD 2^>nul') do set "OPTIONS_RADAR_GIT_SHA=%%G"

rem You xian shi yong yi jiu xu de .venv
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  echo [ERROR] .venv not found. Run: python -m venv .venv
  echo Then: .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

echo [OK] Starting panel: http://127.0.0.1:8787/
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
%PY% -m options_radar.local_runtime
pause
exit /b 0