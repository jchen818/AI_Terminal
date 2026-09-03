@echo off
REM First run creates a virtual environment and installs dependencies.
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv || goto :error
    ".venv\Scripts\python.exe" -m pip install --upgrade pip
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

".venv\Scripts\pythonw.exe" main.py
goto :eof

:error
echo.
echo Setup failed. Check that Python 3.9+ is on your PATH.
pause
