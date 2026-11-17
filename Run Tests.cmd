@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
for /f "tokens=1 delims==" %%V in ('set QD_ 2^>nul') do set "%%V="
set "QD_ENV_FILE=NUL"
set "QD_DB_PATH="
set "QD_LLM_PROVIDER=none"
set "QD_MARKET_DATA=synthetic"
set "ANTHROPIC_API_KEY="
set "QD_LLM_API_KEY="
set "ALPACA_API_KEY_ID="
set "ALPACA_API_SECRET_KEY="
title QuantDesk - Automated Tests
if not exist ".venv\Scripts\python.exe" (
    echo The project's Python environment is missing.
    echo Please ask for help setting up .venv in this folder.
    if /i not "%~1"=="--no-pause" pause
    exit /b 1
)
echo Running tests with temporary databases and no AI provider...
echo.
".venv\Scripts\python.exe" -m pytest -q
set "result=%errorlevel%"
echo.
if "%result%"=="0" (
    echo Tests passed. Linux-only sandbox checks may be skipped on Windows.
) else (
    echo Tests did not pass. Keep this window open and share the error.
)
if /i not "%~1"=="--no-pause" pause
exit /b %result%
