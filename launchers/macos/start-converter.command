#!/bin/zsh

set -u

SCRIPT_DIR="${0:A:h}"
APP_DIR="${SCRIPT_DIR}/../.."
cd "${APP_DIR}" || exit 1

if command -v python3 >/dev/null 2>&1; then
  PYTHON_BIN="python3"
elif command -v python >/dev/null 2>&1; then
  PYTHON_BIN="python"
else
  echo "Python 3 не найден."
  echo "Установите Python 3.10 или новее и запустите файл снова."
  echo "Скачать: https://www.python.org/downloads/"
  echo
  read "?Нажмите Enter, чтобы закрыть окно..."
  exit 1
fi

"${PYTHON_BIN}" "launchers/start_converter.py"
STATUS=$?

echo
read "?Нажмите Enter, чтобы закрыть окно..."
exit "${STATUS}"
