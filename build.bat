@echo off
setlocal
chcp 65001 >nul
echo [Synology Drive Monitor] Building Standalone Executable...

where pyinstaller >nul 2>nul
if %errorlevel% neq 0 (
    echo PyInstaller not found. Installing...
    pip install pyinstaller
)

pyinstaller --noconsole --onefile --clean --name SynologyDriveMonitor synology_drive_monitor.py

if %errorlevel% equ 0 (
    echo.
    echo ===================================================
    echo  Build Complete!
    echo  Executable created at: dist\SynologyDriveMonitor.exe
    echo ===================================================
) else (
    echo.
    echo Build failed. Please check error output above.
)
pause
