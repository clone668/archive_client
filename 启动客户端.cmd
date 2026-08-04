@echo off
setlocal
cd /d "%~dp0"

set "SMSI_PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
set "SMSI_PYTHON=%~dp0.venv\Scripts\python.exe"

if exist "%SMSI_PYTHONW%" (
    start "" "%SMSI_PYTHONW%" "%~dp0main.py"
    exit /b 0
)

if exist "%SMSI_PYTHON%" (
    start "" "%SMSI_PYTHON%" "%~dp0main.py"
    exit /b 0
)

echo SMSI client environment is missing.
echo Run setup.ps1 first, then double-click this file again.
pause
exit /b 1
