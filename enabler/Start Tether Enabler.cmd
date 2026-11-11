@echo off
setlocal
where uv >nul 2>nul
if errorlevel 1 (
    echo Install uv first in PowerShell: winget install --id astral-sh.uv -e
    echo Then reopen this launcher. See the Windows setup guide in README.md.
    pause
    exit /b 1
)
uv run --python 3.12 "%~dp0utility.py" %*
set "tether_exit=%errorlevel%"
if not "%tether_exit%"=="0" pause
exit /b %tether_exit%
