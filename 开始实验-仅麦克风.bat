@echo off
setlocal EnableDelayedExpansion
chcp 65001 >/dev/null
title EffMeet begin_experiment.ps1 (NoRobot)
echo Calling scripts\begin_experiment.ps1 -NoRobot ...
echo.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\begin_experiment.ps1" -NoRobot
set "EXITCODE=!ERRORLEVEL!"
if "!EXITCODE!"=="0" goto ok
echo.
echo [FAIL] exit code !EXITCODE!. Window kept open for troubleshooting.
echo To see full error, run this manually in CMD:
echo   powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\begin_experiment.ps1" -NoRobot
echo.
pause
exit /b !EXITCODE!

:ok
echo.
echo [OK] Done.
echo.
pause
