@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "OPTIONS_RADAR_BUILD_VERSION=v2-local"
for /f "tokens=*" %%G in ('git rev-parse --short HEAD 2^>nul') do set "OPTIONS_RADAR_GIT_SHA=%%G"

rem 优先使用已就绪的 .venv（Python 3.8，已装好全部依赖）
if exist ".venv\Scripts\python.exe" (
  set "PY=.venv\Scripts\python.exe"
) else (
  echo 未找到 .venv，请先运行: python -m venv .venv
  echo 然后安装依赖: .venv\Scripts\python.exe -m pip install -r requirements.txt
  pause
  exit /b 1
)

echo 正在启动本地面板: http://127.0.0.1:8787/
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
%PY% -m options_radar.local_runtime
exit /b 0
