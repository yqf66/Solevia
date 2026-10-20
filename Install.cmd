@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
if errorlevel 1 (echo Installation failed. Run Install.cmd again to resume.)
pause
endlocal
