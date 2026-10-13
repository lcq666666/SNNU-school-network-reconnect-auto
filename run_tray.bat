@echo off
rem SNNU campus reconnect - tray edition (windowless)
setlocal
cd /d "%~dp0"
set "PYW=%LOCALAPPDATA%\Programs\Python\Python314\pythonw.exe"
if not exist "%PYW%" (
    echo Cannot find Pythonw: "%PYW%"
    echo Please check Python version using: py -0p
    pause
    exit /b 1
)
start "" "%PYW%" "%~dp0campus_reconnect_tray.py"
