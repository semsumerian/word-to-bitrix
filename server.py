from __future__ import annotations

import html
import json
import mimetypes
import re
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from bitrix_converter import ALLOWED_EXTENSIONS, convert_file


BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"


class App(BaseHTTPRequestHandler):
    server_version = "WordToBitrix/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self.send_html(index_page())
            return
        if path.startswith("/outputs/"):
            self.serve_output(path.removeprefix("/outputs/"))
            return
        self.send_error(404)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path != "/convert":
            self.send_error(404)
            return

        try:
            upload = self.read_upload()
            result = handle_upload(upload["filename"], upload["content"])
            self.send_html(result_page(result))
        except Exception as exc:  # noqa: BLE001 - user-facing MVP error page
            self.send_html(error_page(str(exc)), status=400)

    def read_upload(self) -> dict[str, object]:
        content_type = self.headers.get("Content-Type", "")
        match = re.search(r"boundary=(.+)$", content_type)
        if not match:
            raise ValueError("Файл не найден в форме загрузки.")

        boundary = match.group(1).strip().strip('"').encode()
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)

        for part in body.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            header_bytes, content = part.split(b"\r\n\r\n", 1)
            headers = header_bytes.decode("utf-8", errors="replace")
            if 'name="file"' not in headers:
                continue
            filename_match = re.search(r'filename="([^"]+)"', headers)
            if not filename_match:
                continue
            content = content.rstrip(b"\r\n-")
            filename = filename_match.group(1)
            return {"filename": filename, "content": content}

        raise ValueError("Не удалось прочитать загруженный файл.")

    def serve_output(self, name: str) -> None:
        safe_name = Path(unquote(name)).name
        path = (OUTPUT_DIR / safe_name).resolve()
        if OUTPUT_DIR.resolve() not in path.parents or not path.exists():
            self.send_error(404)
            return
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_html(self, body: str, status: int = 200) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        print(f"{self.address_string()} - {format % args}")


def handle_upload(filename: str, content: bytes) -> dict[str, object]:
    original_name = Path(filename).name
    suffix = Path(original_name).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Поддерживаются только: {', '.join(sorted(ALLOWED_EXTENSIONS))}")
    if not content:
        raise ValueError("Загруженный файл пустой.")

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    token = uuid.uuid4().hex[:10]
    upload_path = UPLOAD_DIR / f"{token}{suffix}"
    output_path = OUTPUT_DIR / f"{token}.html"
    upload_path.write_bytes(content)

    conversion = convert_file(upload_path, output_path)
    return {
        "original_name": original_name,
        "output_name": output_path.name,
        "report_name": output_path.with_suffix(".report.json").name,
        "fragment": conversion.html_fragment,
        "report": conversion.report,
    }


def index_page() -> str:
    return layout(
        """
        <section class="hero">
          <div>
            <p class="eyebrow">MVP</p>
            <h1>Word -> Bitrix HTML</h1>
            <p class="lead">Загрузите .doc или .docx. Инструмент удалит фрагменты с красной заливкой, оставит желтые добавления и подготовит HTML для поля DETAIL_TEXT в Bitrix Learning.</p>
          </div>
          <form class="upload" method="post" action="/convert" enctype="multipart/form-data">
            <label>Word-файл</label>
            <input required type="file" name="file" accept=".doc,.docx,.rtf">
            <button type="submit">Сконвертировать</button>
          </form>
        </section>
        <section class="cards">
          <article><b>1. Загрузка</b><span>Берем исходный Word от бизнеса.</span></article>
          <article><b>2. Очистка</b><span>Красное удаляем, желтое публикуем без подсветки.</span></article>
          <article><b>3. Экспорт</b><span>Копируем HTML в Bitrix или скачиваем файл.</span></article>
        </section>
        """
    )


def result_page(result: dict[str, object]) -> str:
    report = result["report"]
    assert isinstance(report, dict)
    stats = report.get("stats", {})
    removed = report.get("removed_fragments", [])
    added = report.get("added_fragments", [])
    warnings = report.get("warnings", [])
    fragment = str(result["fragment"])
    escaped_fragment = html.escape(fragment)
    preview = html.escape(preview_document(fragment), quote=True)

    return layout(
        f"""
        <section class="result-head">
          <div>
            <p class="eyebrow">Готово</p>
            <h1>{html.escape(str(result['original_name']))}</h1>
          </div>
          <div class="actions">
            <a class="button secondary" href="/outputs/{html.escape(str(result['output_name']))}" download>Скачать HTML</a>
            <a class="button secondary" href="/outputs/{html.escape(str(result['report_name']))}" download>Скачать отчет</a>
            <a class="button" href="/">Новый файл</a>
          </div>
        </section>
        <section class="stats">
          <div><b>{stats.get('removed_count', 0)}</b><span>удалено</span></div>
          <div><b>{stats.get('added_count', 0)}</b><span>добавлено</span></div>
          <div><b>{stats.get('tables_count', 0)}</b><span>таблиц</span></div>
          <div><b>{stats.get('html_length', 0)}</b><span>символов HTML</span></div>
        </section>
        {warnings_html(warnings)}
        <section class="split">
          <div class="panel">
            <div class="panel-title"><h2>HTML для Bitrix</h2><button onclick="copyHtml()">Скопировать</button></div>
            <textarea id="html-output" spellcheck="false" wrap="off">{escaped_fragment}</textarea>
          </div>
          <div class="panel">
            <div class="panel-title"><h2>Предпросмотр</h2></div>
            <iframe srcdoc="{preview}"></iframe>
          </div>
        </section>
        <section class="split small">
          <div class="panel">
            <h2>Удаленные красные фрагменты</h2>
            {items_html(removed)}
          </div>
          <div class="panel">
            <h2>Добавленные желтые фрагменты</h2>
            {items_html(added)}
          </div>
        </section>
        <script>
          async function copyHtml() {{
            const textarea = document.getElementById('html-output');
            await navigator.clipboard.writeText(textarea.value);
          }}
        </script>
        """
    )


def error_page(message: str) -> str:
    return layout(
        f"""
        <section class="hero error">
          <div>
            <p class="eyebrow">Ошибка</p>
            <h1>Конвертация не выполнена</h1>
            <p class="lead">{html.escape(message)}</p>
            <a class="button" href="/">Вернуться</a>
          </div>
        </section>
        """
    )


def items_html(items: object) -> str:
    if not isinstance(items, list) or not items:
        return '<p class="muted">Нет данных.</p>'
    return "<ol>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items[:50]) + "</ol>"


def warnings_html(items: object) -> str:
    if not isinstance(items, list) or not items:
        return ""
    return '<section class="warnings">' + "".join(f"<p>{html.escape(str(item))}</p>" for item in items) + "</section>"


def preview_document(fragment: str) -> str:
    return f"""
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    body {{ font-family: Arial, sans-serif; color: #1f2933; padding: 24px; line-height: 1.45; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; }}
    td, th {{ border: 1px solid #bfbfbf; padding: 6px; vertical-align: top; }}
    .hide {{ display: none; }}
    .js-fancyMyText {{ border: 2px solid #ff0000; padding: 8px; margin: 8px 0; }}
    .js-fancyLink, button {{ cursor: pointer; }}
    </style></head><body>{fragment}</body></html>
    """


def layout(content: str) -> str:
    return f"""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Word -> Bitrix HTML</title>
      <style>{styles()}</style>
    </head>
    <body>
      <main>{content}</main>
    </body>
    </html>
    """


def styles() -> str:
    return """
    :root { color-scheme: light; --bg: #eef3f8; --ink: #152033; --muted: #667085; --card: #ffffff; --line: #d8e0ea; --accent: #185adb; --accent2: #0f3f9f; }
    * { box-sizing: border-box; }
    body { margin: 0; background: linear-gradient(135deg, #eef3f8, #f8fbff); color: var(--ink); font-family: Arial, sans-serif; }
    main { width: min(1280px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0; }
    h1 { margin: 0; font-size: clamp(30px, 5vw, 56px); line-height: .95; letter-spacing: -0.04em; }
    h2 { margin: 0; font-size: 18px; }
    .eyebrow { margin: 0 0 12px; color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: .12em; }
    .lead { max-width: 760px; color: var(--muted); font-size: 20px; line-height: 1.5; }
    .hero, .result-head { display: grid; grid-template-columns: 1.2fr .8fr; gap: 24px; align-items: stretch; margin-bottom: 24px; }
    .upload, .panel, .cards article, .stats div, .warnings { background: rgba(255,255,255,.88); border: 1px solid var(--line); border-radius: 24px; box-shadow: 0 20px 50px rgba(25,42,70,.08); }
    .upload { padding: 24px; display: grid; align-content: center; gap: 14px; }
    label { font-weight: 700; }
    input[type=file] { width: 100%; padding: 14px; border: 1px dashed #9db0c7; border-radius: 14px; background: #f7faff; }
    button, .button { appearance: none; border: 0; border-radius: 14px; background: var(--accent); color: #fff; padding: 13px 18px; font-weight: 700; text-decoration: none; display: inline-flex; justify-content: center; cursor: pointer; }
    button:hover, .button:hover { background: var(--accent2); }
    .button.secondary { background: #e6eefb; color: var(--accent2); }
    .cards, .stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
    .cards { grid-template-columns: repeat(3, 1fr); }
    .cards article, .stats div { padding: 20px; }
    .cards span, .stats span, .muted { color: var(--muted); display: block; margin-top: 8px; }
    .stats b { font-size: 28px; }
    .actions { display: flex; align-items: start; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
    .split.small { align-items: start; }
    .panel { overflow: hidden; }
    .panel-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px; border-bottom: 1px solid var(--line); }
    .panel > h2 { padding: 16px 16px 0; }
    textarea { width: 100%; min-height: 620px; border: 0; padding: 16px; resize: vertical; font-family: Menlo, Consolas, monospace; font-size: 13px; line-height: 1.45; outline: none; tab-size: 2; white-space: pre; overflow: auto; }
    iframe { width: 100%; min-height: 620px; border: 0; background: #fff; }
    ol { margin: 0; padding: 16px 16px 16px 38px; max-height: 360px; overflow: auto; }
    li { margin-bottom: 10px; color: #344054; }
    .warnings { padding: 12px 16px; margin-bottom: 16px; border-color: #f8d486; background: #fff9e8; }
    .warnings p { margin: 6px 0; color: #8a5a00; }
    .error { grid-template-columns: 1fr; }
    @media (max-width: 860px) { .hero, .result-head, .split, .cards, .stats { grid-template-columns: 1fr; } .actions { justify-content: flex-start; } textarea, iframe { min-height: 420px; } }
    """


def main() -> None:
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", 8080), App)
    print("Word -> Bitrix HTML: http://127.0.0.1:8080")
    server.serve_forever()


if __name__ == "__main__":
    main()
