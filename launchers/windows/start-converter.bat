@echo off
setlocal

set "APP_DIR=%~dp0..\.."
for %%I in ("%APP_DIR%") do set "APP_DIR=%%~fI"
set "LAUNCHER=%APP_DIR%\launchers\start_converter.py"

if not exist "%LAUNCHER%" (
    echo Launcher was not found:
    echo %LAUNCHER%
    goto done
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3 "%LAUNCHER%"
    goto done
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%LAUNCHER%"
    goto done
)

echo Python 3 was not found.
echo Install Python 3.10 or newer and run this file again.
echo Download: https://www.python.org/downloads/

:done
echo.
pause
