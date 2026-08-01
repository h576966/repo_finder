@echo off
setlocal
set "SOURCE_SCOUT_ROOT=%~dp0"
set "SOURCE_SCOUT_PYTHON=%SOURCE_SCOUT_ROOT%.venv\Scripts\python.exe"

if not exist "%SOURCE_SCOUT_PYTHON%" (
    >&2 echo Source Scout virtual environment not found: %SOURCE_SCOUT_PYTHON%
    >&2 echo Run the setup commands in README.md from %SOURCE_SCOUT_ROOT%
    exit /b 1
)

set "PYTHONPATH=%SOURCE_SCOUT_ROOT%src;%PYTHONPATH%"
"%SOURCE_SCOUT_PYTHON%" -m source_scout %*
exit /b %errorlevel%
