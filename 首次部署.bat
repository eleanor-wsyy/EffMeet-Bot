@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title EffMeet 首次部署
echo 正在为这台电脑创建独立的 Python 虚拟环境...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\setup_env.ps1"
set "EXITCODE=!ERRORLEVEL!"
if "!EXITCODE!"=="0" goto ok
echo.
echo [FAIL] 部署失败，退出码 !EXITCODE!。
echo 请保留此窗口中的错误信息，检查 Python 和网络后重试。
echo.
pause
exit /b !EXITCODE!

:ok
echo.
echo [OK] 本机虚拟环境已准备好。
echo 现在可以双击“启动实验控制台.bat”。
echo.
pause
