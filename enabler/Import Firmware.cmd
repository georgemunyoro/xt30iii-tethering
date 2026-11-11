@echo off
setlocal
where uv >nul 2>nul
if errorlevel 1 (
    echo Install uv first in PowerShell: winget install --id astral-sh.uv -e
    pause
    exit /b 1
)
if "%~1"=="" (
    echo Drag your original FWUP0037.DAT onto this file to import it.
    echo Only the unchanged X-T30 III firmware 1.10 file is accepted.
    pause
    exit /b 1
)
uv run --python 3.12 "%~dp0import_firmware.py" "%~1"
set "tether_exit=%errorlevel%"
pause
exit /b %tether_exit%
