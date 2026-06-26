from __future__ import annotations

import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1]
SERVER_PATH = APP_DIR / "server.py"
HOST = "127.0.0.1"
PORTS = range(8080, 8090)
MIN_PYTHON = (3, 10)


def main() -> int:
    print("Конвертер для Bitrix")
    print("====================")

    if sys.version_info < MIN_PYTHON:
        print("Нужен Python 3.10 или новее.")
        print(f"Сейчас найден: {sys.version.split()[0]}")
        return 1

    if not SERVER_PATH.exists():
        print(f"Не найден файл сервера: {SERVER_PATH}")
        return 1

    sys.path.insert(0, str(APP_DIR))
    try:
        from bitrix_converter import find_libreoffice
    except Exception as exc:  # noqa: BLE001 - launcher should show a plain message
        print("Не удалось загрузить модуль конвертера.")
        print(str(exc))
        return 1

    soffice = find_libreoffice()
    if not soffice:
        print("LibreOffice не найден.")
        print("Установите LibreOffice стандартным способом и запустите конвертер снова.")
        print("Скачать: https://www.libreoffice.org/download/download-libreoffice/")
        return 1

    port = find_free_port()
    if port is None:
        print("Не удалось найти свободный порт с 8080 по 8089.")
        print("Закройте другой запущенный конвертер или перезагрузите компьютер.")
        return 1

    url = f"http://{HOST}:{port}"
    print(f"LibreOffice найден: {soffice}")
    print(f"Запускаю сайт: {url}")
    print("Чтобы остановить сайт, закройте это окно или нажмите Ctrl+C.")

    process = subprocess.Popen(
        [sys.executable, str(SERVER_PATH), "--host", HOST, "--port", str(port)],
        cwd=str(APP_DIR),
    )

    if wait_for_server(url):
        webbrowser.open(url)
    else:
        print("Сайт не успел запуститься. Попробуйте открыть адрес вручную:")
        print(url)

    try:
        return process.wait()
    except KeyboardInterrupt:
        print("\nОстанавливаю сайт...")
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        return 0


def find_free_port() -> int | None:
    for port in PORTS:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((HOST, port))
            except OSError:
                continue
            return port
    return None


def wait_for_server(url: str, timeout_seconds: float = 10.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as response:
                return 200 <= response.status < 500
        except Exception:
            time.sleep(0.2)
    return False


if __name__ == "__main__":
    raise SystemExit(main())
