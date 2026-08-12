@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

set "PYTHON_EXE=C:\Users\jiangyue\AppData\Local\Programs\Python\Python312\python.exe"
if not exist "%PYTHON_EXE%" set "PYTHON_EXE=py -3.12"

if not exist ".venv-local\Scripts\python.exe" (
  echo [1/3] 正在创建独立运行环境...
  %PYTHON_EXE% -m venv .venv-local || goto :error
)

echo [2/3] 正在检查依赖...
.venv-local\Scripts\python.exe -c "import yaml,pydantic" >nul 2>&1
if errorlevel 1 .venv-local\Scripts\python.exe -m pip install -e ".[futu,ibkr]" APScheduler playwright lark-oapi discord.py || goto :error

echo [3/3] 正在启动本地面板...
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
.venv-local\Scripts\python.exe -m options_radar.local_runtime
exit /b 0

:error
echo.
echo 启动准备失败，请保留此窗口中的错误信息。
pause
exit /b 1
