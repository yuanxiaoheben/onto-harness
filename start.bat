@echo off
setlocal
title Ontology Harness
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Preparing Python environment...
    py -3 -m venv .venv
    if errorlevel 1 goto failed
    ".venv\Scripts\python.exe" -m pip install -e ".[web]"
    if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" "scripts\launch.py" %*
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Startup failed. See the message above.
pause
exit /b 1
