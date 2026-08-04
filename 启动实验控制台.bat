@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul
title EffMeet start_effmeet.ps1
echo Calling scripts\start_effmeet.ps1 ...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_effmeet.ps1"
set "EXITCODE=!ERRORLEVEL!"
if "!EXITCODE!"=="0" goto ok
echo.
echo [FAIL] exit code !EXITCODE!. Window kept open for troubleshooting.
echo To see full error, run this manually in CMD:
echo   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_effmeet.ps1"
echo.
pause
exit /b !EXITCODE!

:ok
echo.
echo [OK] Done.
echo.
pause
