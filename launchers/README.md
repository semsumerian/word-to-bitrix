# Запускатели локального сайта

В проекте один локальный сайт и несколько запускателей под разные системы.

## macOS

Файл:

```text
launchers/macos/start-converter.command
```

Что должно быть установлено:

- Python 3.10 или новее;
- LibreOffice в стандартной папке `/Applications`.

Запуск: двойной клик по `start-converter.command`.

## Windows

Файлы:

```text
launchers/windows/start-converter.bat
launchers/windows/start-converter.ps1
```

Что должно быть установлено:

- Python 3.10 или новее;
- LibreOffice в стандартной папке `C:\Program Files\LibreOffice`.

Обычно достаточно запускать `start-converter.bat` двойным кликом.

## Что делает запускатель

1. Проверяет Python.
2. Проверяет LibreOffice.
3. Находит свободный порт с `8080` по `8089`.
4. Запускает `server.py`.
5. Открывает браузер на локальном адресе.

Чтобы остановить сайт, закройте окно запуска или нажмите `Ctrl+C`.
