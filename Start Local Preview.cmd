@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
title QuantDesk - Safe Local Preview
if not exist ".venv\Scripts\python.exe" (
    echo The project's Python environment is missing.
    echo Please ask for help setting up .venv in this folder.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -m quantdesk.preview %*
set "result=%errorlevel%"
echo.
echo Local preview stopped. Your preview history is saved separately.
pause
exit /b %result%
