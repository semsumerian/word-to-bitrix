from __future__ import annotations

import argparse
import html
import json
import mimetypes
import re
import sys
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


class ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True


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
          <div class="intro">
            <p class="eyebrow">Конвертер для Bitrix</p>
            <h1>Word в HTML для учебных курсов</h1>
            <p class="lead">Один файл Word на входе, чистый HTML для Bitrix на выходе. Цветовые правки обработаются автоматически.</p>
          </div>
          <form class="upload" method="post" action="/convert" enctype="multipart/form-data">
            <div class="upload-heading">
              <h2>Загрузка документа</h2>
              <span>.doc, .docx</span>
            </div>
            <label class="file-picker" for="file-input">
              <input id="file-input" required type="file" name="file" accept=".doc,.docx">
              <span class="file-picker-main">Выберите Word-файл</span>
              <span class="file-picker-meta" id="file-name">или перетащите .doc/.docx сюда</span>
            </label>
            <button type="submit">Подготовить HTML</button>
          </form>
        </section>
        <script>
          const fileInput = document.getElementById('file-input');
          const fileName = document.getElementById('file-name');
          if (fileInput && fileName) {
            fileInput.addEventListener('change', () => {
              const file = fileInput.files && fileInput.files[0];
              fileName.textContent = file ? file.name : 'или перетащите .doc/.docx сюда';
            });
          }
        </script>
        """,
        page_class="home",
    )


def result_page(result: dict[str, object]) -> str:
    report = result["report"]
    assert isinstance(report, dict)
    stats = report.get("stats", {})
    removed = report.get("removed_fragments", [])
    added = report.get("added_fragments", [])
    warnings = report.get("warnings", [])
    removed_count = stats.get("removed_count", 0)
    added_count = stats.get("added_count", 0)
    tables_count = stats.get("tables_count", 0)
    html_length = stats.get("html_length", 0)
    fragment = str(result["fragment"])
    escaped_fragment = html.escape(fragment)
    preview = html.escape(preview_document(fragment), quote=True)

    return layout(
        f"""
        <section class="result-head">
          <div>
            <p class="eyebrow">Готово</p>
            <h1>{html.escape(str(result['original_name']))}</h1>
            <p class="result-note">HTML подготовлен. Скопируйте его в Bitrix или скачайте файл.</p>
          </div>
          <div class="actions">
            <button type="button" onclick="copyHtml(this)">Скопировать HTML</button>
            <a class="button secondary" href="/outputs/{html.escape(str(result['output_name']))}" download>Скачать HTML</a>
            <a class="button ghost" href="/">Новый файл</a>
          </div>
        </section>
        <section class="stats" aria-label="Статистика обработки">
          <div><span>Удалено</span><b>{html.escape(str(removed_count))}</b></div>
          <div><span>Добавлено</span><b>{html.escape(str(added_count))}</b></div>
          <div><span>Таблиц</span><b>{html.escape(str(tables_count))}</b></div>
          <div><span>HTML</span><b>{html.escape(str(html_length))} символов</b></div>
        </section>
        {warnings_html(warnings)}
        <section class="split workspace" id="workspace" data-view-mode="split">
          <div class="panel html-panel">
            <div class="panel-title">
              <h2>HTML</h2>
              <div class="panel-actions">
                <button type="button" class="small secondary" onclick="copyHtml(this)">Скопировать</button>
                <button type="button" class="small ghost" id="wrap-toggle" aria-pressed="false">Перенос строк</button>
                <button type="button" class="small ghost" data-expand-panel="html" aria-pressed="false">Развернуть</button>
              </div>
            </div>
            <textarea id="html-output" spellcheck="false" wrap="off">{escaped_fragment}</textarea>
          </div>
          <div class="panel preview-panel">
            <div class="panel-title">
              <h2>Предпросмотр</h2>
              <div class="panel-actions">
                <button type="button" class="small ghost" data-expand-panel="preview" aria-pressed="false">Развернуть</button>
              </div>
            </div>
            <iframe srcdoc="{preview}"></iframe>
          </div>
        </section>
        <details class="details-block">
          <summary class="details-summary">
            <span class="details-title">Подробности обработки</span>
            <small>Удалено: {html.escape(str(removed_count))}, добавлено: {html.escape(str(added_count))}</small>
          </summary>
          <section class="split small detail-grid">
            <div class="panel">
              <h2>Удаленные фрагменты ({html.escape(str(removed_count))})</h2>
              {items_html(removed)}
            </div>
            <div class="panel">
              <h2>Добавленные фрагменты ({html.escape(str(added_count))})</h2>
              {items_html(added)}
            </div>
          </section>
        </details>
        <script>
          function setCopyState(button, text) {{
            if (!button) return;
            const originalText = button.dataset.originalText || button.textContent;
            button.dataset.originalText = originalText;
            button.textContent = text;
            window.clearTimeout(button.copyTimer);
            button.copyTimer = window.setTimeout(() => {{
              button.textContent = originalText;
            }}, 1800);
          }}

          async function copyHtml(button) {{
            const textarea = document.getElementById('html-output');
            try {{
              await navigator.clipboard.writeText(textarea.value);
              setCopyState(button, 'Скопировано');
            }} catch (error) {{
              textarea.focus();
              textarea.select();
              setCopyState(button, 'Выделено');
            }}
          }}

          const workspace = document.getElementById('workspace');
          const expandButtons = document.querySelectorAll('[data-expand-panel]');
          const wrapButton = document.getElementById('wrap-toggle');
          const htmlOutput = document.getElementById('html-output');

          function setViewMode(mode) {{
            if (!workspace) return;
            workspace.dataset.viewMode = mode;
            expandButtons.forEach((button) => {{
              const active = button.dataset.expandPanel === mode;
              button.classList.toggle('active', active);
              button.textContent = active ? 'Свернуть' : 'Развернуть';
              button.setAttribute('aria-pressed', String(active));
            }});
          }}

          expandButtons.forEach((button) => {{
            button.addEventListener('click', () => {{
              const panel = button.dataset.expandPanel;
              setViewMode(workspace && workspace.dataset.viewMode === panel ? 'split' : panel);
            }});
          }});

          if (wrapButton && htmlOutput) {{
            wrapButton.addEventListener('click', () => {{
              const wrapped = !htmlOutput.classList.contains('is-wrapped');
              htmlOutput.classList.toggle('is-wrapped', wrapped);
              htmlOutput.setAttribute('wrap', wrapped ? 'soft' : 'off');
              wrapButton.classList.toggle('active', wrapped);
              wrapButton.textContent = wrapped ? 'Без переноса' : 'Перенос строк';
              wrapButton.setAttribute('aria-pressed', String(wrapped));
            }});
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
    return "<ol>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ol>"


def warnings_html(items: object) -> str:
    if not isinstance(items, list) or not items:
        return ""
    return '<section class="warnings">' + "".join(f"<p>{html.escape(str(item))}</p>" for item in items) + "</section>"


def preview_document(fragment: str) -> str:
    return f"""
    <!doctype html>
    <html><head><meta charset="utf-8"><style>
    body {{ font-family: Arial, sans-serif; color: #1f2933; padding: 24px; line-height: 1.45; }}
    table {{ border-collapse: collapse; width: 100%; margin: 12px 0; table-layout: fixed; }}
    td, th {{ border: 1px solid #bfbfbf; padding: 6px; overflow-wrap: anywhere; word-break: break-word; }}
    td p, th p {{ margin: 0 0 6px; }}
    td p:last-child, th p:last-child {{ margin-bottom: 0; }}
    .hide {{ display: none; }}
    .js-fancyMyText {{ border: 2px solid #ff0000; padding: 8px; margin: 8px 0; }}
    .js-fancyLink, button {{ cursor: pointer; }}
    </style></head><body>{fragment}</body></html>
    """


def layout(content: str, page_class: str = "") -> str:
    body_class = f' class="{html.escape(page_class, quote=True)}"' if page_class else ""
    return f"""
    <!doctype html>
    <html lang="ru">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Word в HTML для учебных курсов</title>
      <style>{styles()}</style>
    </head>
    <body{body_class}>
      <main>{content}</main>
    </body>
    </html>
    """


def styles() -> str:
    return """
    :root { color-scheme: light; --bg: #eef3f8; --ink: #152033; --muted: #667085; --card: #ffffff; --line: #d8e0ea; --accent: #185adb; --accent2: #0f3f9f; }
    * { box-sizing: border-box; }
    html { min-height: 100%; }
    body { min-height: 100%; margin: 0; background: linear-gradient(135deg, #f3f7fb, #eef7f3 52%, #f8fbff); color: var(--ink); font-family: Arial, sans-serif; }
    main { width: min(1280px, calc(100% - 32px)); margin: 0 auto; padding: 32px 0; }
    h1 { margin: 0; font-size: 38px; line-height: 1.08; letter-spacing: 0; }
    h2 { margin: 0; font-size: 18px; }
    .eyebrow { margin: 0 0 12px; color: var(--accent); font-weight: 700; text-transform: uppercase; letter-spacing: 0; }
    .lead { max-width: 760px; color: var(--muted); font-size: 20px; line-height: 1.5; margin: 18px 0 0; }
    .result-note { margin: 12px 0 0; color: var(--muted); font-size: 16px; line-height: 1.45; }
    .hero, .result-head { display: grid; grid-template-columns: 1.2fr .8fr; gap: 24px; align-items: stretch; margin-bottom: 24px; }
    .upload, .panel, .cards article, .warnings { background: rgba(255,255,255,.88); border: 1px solid var(--line); border-radius: 24px; box-shadow: 0 20px 50px rgba(25,42,70,.08); }
    .intro { display: grid; align-content: center; }
    .upload { padding: 24px; display: grid; align-content: center; gap: 14px; }
    .upload-heading { display: flex; align-items: baseline; justify-content: space-between; gap: 12px; }
    .upload-heading span { color: var(--muted); }
    label { font-weight: 700; }
    input[type=file] { width: 100%; padding: 14px; border: 1px dashed #9db0c7; border-radius: 14px; background: #f7faff; }
    .file-picker { position: relative; display: grid; gap: 6px; padding: 18px; border: 1px dashed #9db0c7; border-radius: 16px; background: #f7faff; cursor: pointer; }
    .file-picker:hover { border-color: var(--accent); background: #f3f8ff; }
    .file-picker input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
    .file-picker-main { color: var(--ink); }
    .file-picker-meta { color: var(--muted); font-weight: 400; overflow-wrap: anywhere; }
    button, .button { appearance: none; border: 0; border-radius: 14px; background: var(--accent); color: #fff; padding: 13px 18px; font-weight: 700; text-decoration: none; display: inline-flex; justify-content: center; cursor: pointer; }
    button:hover, .button:hover { background: var(--accent2); }
    button.secondary, .button.secondary { background: #e6eefb; color: var(--accent2); }
    button.secondary:hover, .button.secondary:hover { background: #d8e6fb; }
    button.ghost, .button.ghost { background: transparent; color: var(--accent2); border: 1px solid var(--line); }
    button.ghost:hover, .button.ghost:hover { background: #f7faff; }
    button.ghost.active { background: #e6eefb; border-color: #c7d7ef; color: var(--accent2); }
    button.small, .button.small { padding: 9px 12px; border-radius: 12px; font-size: 13px; }
    .cards { display: grid; grid-template-columns: repeat(4, 1fr); gap: 16px; margin-bottom: 24px; }
    .cards { grid-template-columns: repeat(3, 1fr); }
    .cards article { padding: 20px; }
    .cards span, .muted { color: var(--muted); display: block; margin-top: 8px; }
    .stats { display: flex; flex-wrap: wrap; gap: 8px; margin: -8px 0 20px; }
    .stats div { display: inline-flex; align-items: baseline; gap: 6px; padding: 8px 12px; border: 1px solid var(--line); border-radius: 999px; background: rgba(255,255,255,.72); color: var(--muted); }
    .stats span { font-size: 13px; }
    .stats b { color: var(--ink); font-size: 14px; }
    .actions { display: flex; align-items: start; justify-content: flex-end; gap: 10px; flex-wrap: wrap; }
    .split { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; margin-bottom: 16px; }
    .split.small { align-items: start; }
    .workspace[data-view-mode="html"], .workspace[data-view-mode="preview"] { grid-template-columns: 1fr; }
    .workspace[data-view-mode="html"] .preview-panel, .workspace[data-view-mode="preview"] .html-panel { display: none; }
    .workspace[data-view-mode="html"] textarea, .workspace[data-view-mode="preview"] iframe { min-height: 720px; }
    .panel { overflow: hidden; }
    .panel-title { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 16px; border-bottom: 1px solid var(--line); }
    .panel-actions { display: flex; align-items: center; justify-content: flex-end; gap: 8px; flex-wrap: wrap; }
    .panel > h2 { padding: 16px 16px 0; }
    textarea { width: 100%; min-height: 620px; border: 0; padding: 16px; resize: vertical; font-family: Menlo, Consolas, monospace; font-size: 13px; line-height: 1.45; outline: none; tab-size: 2; white-space: pre; overflow: auto; }
    textarea.is-wrapped { white-space: pre-wrap; overflow-wrap: anywhere; }
    iframe { width: 100%; min-height: 620px; border: 0; background: #fff; }
    ol { margin: 0; padding: 16px 16px 16px 38px; max-height: 360px; overflow: auto; }
    li { margin-bottom: 10px; color: #344054; }
    .warnings { padding: 12px 16px; margin-bottom: 16px; border-color: #f8d486; background: #fff9e8; }
    .warnings p { margin: 6px 0; color: #8a5a00; }
    .details-block { margin-top: 4px; }
    .details-summary { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 12px 2px 14px; border-top: 1px solid var(--line); cursor: pointer; }
    .details-summary::-webkit-details-marker { display: none; }
    .details-title { font-weight: 700; }
    .details-title::before { content: "▸"; color: var(--accent); margin-right: 8px; }
    .details-block[open] .details-title::before { content: "▾"; }
    .details-summary small { color: var(--muted); font-size: 13px; line-height: 1.35; }
    .details-block .detail-grid { margin-bottom: 0; }
    .error { grid-template-columns: 1fr; }
    body.home main { display: grid; align-items: center; width: min(960px, calc(100% - 40px)); min-height: 100dvh; padding: clamp(28px, 6vh, 64px) 0; }
    body.home .hero { grid-template-columns: minmax(0, 1fr) minmax(320px, 400px); align-items: center; gap: 48px; margin-bottom: 0; }
    body.home .intro { align-content: center; }
    body.home .intro h1 { max-width: 560px; font-size: 48px; line-height: 1.03; }
    body.home .lead { max-width: 520px; font-size: 18px; line-height: 1.55; }
    body.home .upload { align-content: start; gap: 18px; min-height: 336px; padding: 28px; border-radius: 8px; background: rgba(255,255,255,.94); box-shadow: 0 24px 64px rgba(25,42,70,.10); }
    body.home .upload-heading { padding-bottom: 2px; }
    body.home .file-picker { min-height: 164px; place-content: center; gap: 8px; padding: 24px; text-align: center; border-radius: 8px; background: #f8fbff; }
    body.home .file-picker-main { color: var(--accent2); font-size: 17px; }
    body.home .file-picker-meta { font-size: 14px; }
    body.home .upload button { align-items: center; width: 100%; min-height: 50px; border-radius: 8px; font-size: 15px; }
    @media (max-width: 860px) { .hero, .result-head, .split, .cards { grid-template-columns: 1fr; } .actions { justify-content: flex-start; } .panel-title { align-items: flex-start; flex-direction: column; } .panel-actions { justify-content: flex-start; width: 100%; } textarea, iframe, .workspace[data-view-mode="html"] textarea, .workspace[data-view-mode="preview"] iframe { min-height: 520px; } }
    @media (max-width: 860px) { body.home main { align-items: center; width: min(640px, calc(100% - 32px)); min-height: 100dvh; padding: 32px 0; } body.home .hero { grid-template-columns: 1fr; gap: 18px; } body.home .intro h1 { font-size: 40px; } body.home .lead { font-size: 16px; } body.home .upload { min-height: 0; padding: 20px; } body.home .file-picker { min-height: 132px; } }
    @media (max-width: 420px) { body.home .intro h1 { font-size: 34px; } h1 { font-size: 32px; } }
    """


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Bitrix course HTML converter web UI.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=8080, help="Port to bind. Default: 8080")
    args = parser.parse_args()

    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    try:
        server = ReusableThreadingHTTPServer((args.host, args.port), App)
    except OSError as exc:
        if exc.errno in {48, 98, 10048}:
            print(f"Port {args.port} is already in use. Open http://{args.host}:{args.port} if the server is already running, or start another port:")
            print(f"python3 server.py --port {args.port + 1}")
            sys.exit(1)
        raise

    print(f"Конвертер для Bitrix: http://{args.host}:{args.port}")
    server.serve_forever()


if __name__ == "__main__":
    main()
