@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel% equ 0 (
    start "" pythonw synology_drive_monitor.py
    goto :eof
)

where py >nul 2>nul
if %errorlevel% equ 0 (
    start "" py -3 synology_drive_monitor.py
    goto :eof
)

python synology_drive_monitor.py
