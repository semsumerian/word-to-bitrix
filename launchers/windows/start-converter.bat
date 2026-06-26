@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0..\.."

where py >nul 2>nul
if %errorlevel%==0 (
    py -3 "launchers\start_converter.py"
    goto done
)

where python >nul 2>nul
if %errorlevel%==0 (
    python "launchers\start_converter.py"
    goto done
)

echo Python 3 не найден.
echo Установите Python 3.10 или новее и запустите файл снова.
echo Скачать: https://www.python.org/downloads/

:done
echo.
pause
