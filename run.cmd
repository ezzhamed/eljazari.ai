@echo off
REM ============================================================================
REM  eljazari.ai - double-click to run the whole stack
REM
REM  Thin launcher on purpose: Explorer runs .cmd on double-click but not .ps1,
REM  and -ExecutionPolicy Bypass keeps it working on a machine whose policy has
REM  never been loosened. The actual work is in infrastructure/run.ps1.
REM
REM  ASCII only, deliberately. cmd.exe reads this file in the system code page,
REM  so a UTF-8 dash here decodes into bytes it tries to run as a command
REM  ("'le' is not recognized...") even from inside a REM.
REM
REM    run.cmd              start everything and open the browser
REM    run.cmd -NoBrowser   start everything, open nothing
REM    run.cmd -Stop        stop the API and web servers
REM ============================================================================
title eljazari.ai
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0infrastructure\run.ps1" %*
if errorlevel 1 (
    echo.
    echo Startup failed. This window stays open so the error above is readable.
    pause
)
