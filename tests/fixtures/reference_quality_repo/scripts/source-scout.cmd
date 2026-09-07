@echo off
set PROJECT_PYTHON=%~dp0..\.venv\Scripts\python.exe
if not exist "%PROJECT_PYTHON%" echo repository virtual environment missing
"%PROJECT_PYTHON%" -m source_scout %*
