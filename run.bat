@echo off
rem SNNU campus reconnect watcher - for Windows user session (F21)
rem 解释器探测顺序：项目 venv > PATH 中的 pythonw > py 启动器解析
setlocal
cd /d "%~dp0"

set "PYW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%PYW%" goto check

for %%I in (pythonw.exe) do if not "%%~$PATH:I"=="" set "PYW=%%~$PATH:I"
if exist "%PYW%" goto check

for /f "delims=" %%I in ('py -3 -c "import sys,os;print(os.path.join(os.path.dirname(sys.executable),'pythonw.exe'))" 2^>nul') do set "PYW=%%I"
if exist "%PYW%" goto check

echo [错误] 未找到 pythonw.exe。
echo 请安装 Python 3.10+ 并把依赖装进项目 venv： .venv\Scripts\python.exe -m pip install -r requirements.txt
pause
exit /b 1

:check
rem 启动前验证运行依赖，缺失则明确报错而不是静默失败
"%PYW%" -c "import requests, keyring" 2>nul
if errorlevel 1 (
    echo [错误] 缺少依赖 requests / keyring。
    echo 请执行： "%~dp0.venv\Scripts\python.exe" -m pip install -r requirements.txt
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0campus_reconnect.py" watch --ssid SNNU --yys mobile
