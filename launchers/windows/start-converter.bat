@echo off
setlocal

set "APP_DIR=%~dp0..\.."
set "LAUNCHER=%APP_DIR%\launchers\start_converter.py"

py -3 "%LAUNCHER%"
if not errorlevel 9009 goto done

python "%LAUNCHER%"
if not errorlevel 9009 goto done

echo Python 3 was not found.
echo Install Python 3.10 or newer and run this file again.
echo Download: https://www.python.org/downloads/

:done
echo.
pause
