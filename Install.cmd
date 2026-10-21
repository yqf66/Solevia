@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install.ps1" %*
if errorlevel 1 (echo 安装未完成，请查看上方原因；保留当前文件夹，再运行 Install.cmd 可重试。)
pause
endlocal
