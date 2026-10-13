@echo off
rem SNNU campus reconnect - tray edition (windowless) (F21)
rem 源码模式优先用项目 venv（避免调试新分支时误跑相邻旧 exe）；否则用打包 exe；再退回 PATH/pythonw
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYW%" goto check

for %%I in (pythonw.exe) do if not "%%~$PATH:I"=="" set "PYW=%%~$PATH:I"
if exist "%PYW%" goto check

for /f "delims=" %%I in ('py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%I"
if exist "%PYW%" goto check

if exist "%~dp0CampusReconnect.exe" (
    start "" "%~dp0CampusReconnect.exe"
    exit /b 0
)

echo [错误] 未找到可用的 pythonw.exe 或 CampusReconnect.exe。
echo 请安装 Python 3.10+（含依赖）或使用打包好的 CampusReconnect.exe。
pause
exit /b 1

:check
rem 启动前验证运行依赖（含托盘依赖）
"%PYW%" -c "import requests, keyring, pystray, PIL" 2>nul
if errorlevel 1 (
    echo [错误] 缺少依赖（requests / keyring / pystray / pillow）。
    echo 请执行： "%~dp0.venv\Scripts\python.exe" -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0campus_reconnect_tray.py"
