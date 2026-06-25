from __future__ import annotations

import argparse
import html
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
import xml.etree.ElementTree as ET


DELETE_COLORS = {"#ff0000", "#c00000", "red"}
ADD_COLORS = {"#ffff00", "#fff6c6", "#00ff00", "yellow", "green"}
DEFAULT_TEXT_COLORS = {"#000", "#000000", "#333333", "black", "rgb(0,0,0)", "rgb(51,51,51)"}
ALLOWED_EXTENSIONS = {".doc", ".docx", ".rtf"}
W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"


def w_tag(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def r_tag(name: str) -> str:
    return f"{{{R_NS}}}{name}"


@dataclass
class Node:
    tag: str | None = None
    attrs: dict[str, str] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)
    data: str = ""

    @property
    def is_text(self) -> bool:
        return self.tag is None

    def text_content(self) -> str:
        if self.is_text:
            return self.data
        return "".join(child.text_content() for child in self.children)


class TreeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.root = Node("root")
        self.stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag.lower(), {name.lower(): value or "" for name, value in attrs})
        self.stack[-1].children.append(node)
        if tag.lower() not in {"br", "hr", "img", "meta", "link", "input", "col"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        self.stack[-1].children.append(Node(data=data))

    def handle_entityref(self, name: str) -> None:
        self.stack[-1].children.append(Node(data=f"&{name};"))

    def handle_charref(self, name: str) -> None:
        self.stack[-1].children.append(Node(data=f"&#{name};"))


@dataclass
class ConversionResult:
    source_file: str
    output_file: str | None
    html_fragment: str
    report: dict[str, object]


@dataclass
class NumberingLevel:
    start: int = 1
    fmt: str = "decimal"
    text: str = "%1."


@dataclass
class TextStyle:
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    strike: bool | None = None


@dataclass
class DocxContext:
    rels: dict[str, str]
    numbering: dict[tuple[str, str], NumberingLevel]
    styles: dict[str, TextStyle]
    counters: dict[tuple[str, str], int] = field(default_factory=dict)

    def next_number(self, paragraph: ET.Element) -> str | None:
        num_id, level = paragraph_numbering(paragraph)
        if num_id is None or level is None:
            return None

        numbering_level = self.numbering.get((num_id, level))
        if numbering_level is None or numbering_level.fmt in {"none", "bullet"}:
            return None

        key = (num_id, level)
        current = self.counters.get(key, numbering_level.start - 1) + 1
        self.counters[key] = current

        if numbering_level.fmt != "decimal":
            return None

        return numbering_level.text.replace(f"%{int(level) + 1}", str(current))


def convert_file(input_path: Path, output_path: Path | None = None) -> ConversionResult:
    input_path = input_path.resolve()
    if input_path.suffix.lower() not in ALLOWED_EXTENSIONS:
        raise ValueError(f"Unsupported file type: {input_path.suffix}")

    raw_html, converter_name = word_to_html(input_path)
    fragment, report = clean_for_bitrix(raw_html, converter_name)
    report["source_file"] = str(input_path)
    report["converter"] = converter_name

    if output_path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(fragment, encoding="utf-8")

        report_path = output_path.with_suffix(".report.json")
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    return ConversionResult(
        source_file=str(input_path),
        output_file=str(output_path) if output_path else None,
        html_fragment=fragment,
        report=report,
    )


def word_to_html(input_path: Path) -> tuple[str, str]:
    soffice = find_libreoffice()
    if soffice:
        return convert_with_libreoffice_docx(input_path, soffice), "libreoffice-docx"

    textutil = shutil.which("textutil")
    if textutil:
        return convert_with_textutil(input_path, textutil), "textutil"

    raise RuntimeError("No converter found. Install LibreOffice or run on macOS with textutil.")


def find_libreoffice() -> str | None:
    candidates = [
        shutil.which("soffice"),
        shutil.which("libreoffice"),
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        str(Path.home() / "Applications/LibreOffice.app/Contents/MacOS/soffice"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).exists():
            return candidate
    return None


def convert_with_textutil(input_path: Path, textutil: str) -> str:
    output = subprocess.check_output(
        [textutil, "-stdout", "-convert", "html", str(input_path)],
        stderr=subprocess.STDOUT,
    )
    return output.replace(b"\x00", b"").decode("utf-8", errors="replace")


def convert_with_libreoffice_docx(input_path: Path, soffice: str) -> str:
    if input_path.suffix.lower() == ".docx":
        return docx_to_html(input_path)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        subprocess.check_call(
            [
                soffice,
                "--headless",
                "--convert-to",
                "docx",
                "--outdir",
                str(tmp_path),
                str(input_path),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        docx_files = list(tmp_path.glob("*.docx"))
        if not docx_files:
            raise RuntimeError("LibreOffice did not produce a DOCX file.")
        return docx_to_html(docx_files[0])


def convert_with_libreoffice(input_path: Path, soffice: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        subprocess.check_call(
            [
                soffice,
                "--headless",
                "--convert-to",
                "html",
                "--outdir",
                str(tmp_path),
                str(input_path),
            ]
        )
        html_files = list(tmp_path.glob("*.html")) + list(tmp_path.glob("*.htm"))
        if not html_files:
            raise RuntimeError("LibreOffice did not produce an HTML file.")
        return html_files[0].read_text(encoding="utf-8", errors="replace")


def docx_to_html(docx_path: Path) -> str:
    with zipfile.ZipFile(docx_path) as archive:
        document_xml = archive.read("word/document.xml")
        rels = read_docx_relationships(archive)
        numbering = read_docx_numbering(archive)
        styles = read_docx_styles(archive)

    root = ET.fromstring(document_xml)
    body = root.find(w_tag("body"))
    if body is None:
        return ""

    context = DocxContext(rels=rels, numbering=numbering, styles=styles)
    blocks = []
    for child in body:
        rendered = render_docx_block(child, context)
        if rendered:
            blocks.append(rendered)
    return "\n".join(blocks).strip()


def read_docx_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    rels: dict[str, str] = {}
    try:
        rels_xml = archive.read("word/_rels/document.xml.rels")
    except KeyError:
        return rels

    root = ET.fromstring(rels_xml)
    for rel in root.findall(f"{{{REL_NS}}}Relationship"):
        rel_id = rel.attrib.get("Id")
        target = rel.attrib.get("Target")
        if rel_id and target:
            rels[rel_id] = target
    return rels


def read_docx_numbering(archive: zipfile.ZipFile) -> dict[tuple[str, str], NumberingLevel]:
    try:
        numbering_xml = archive.read("word/numbering.xml")
    except KeyError:
        return {}

    root = ET.fromstring(numbering_xml)
    abstract_levels: dict[tuple[str, str], NumberingLevel] = {}
    for abstract in root.findall(w_tag("abstractNum")):
        abstract_id = abstract.attrib.get(w_tag("abstractNumId"))
        if abstract_id is None:
            continue
        for level in abstract.findall(w_tag("lvl")):
            level_id = level.attrib.get(w_tag("ilvl"), "0")
            abstract_levels[(abstract_id, level_id)] = NumberingLevel(
                start=int(child_attr(level, "start", "val") or "1"),
                fmt=child_attr(level, "numFmt", "val") or "decimal",
                text=child_attr(level, "lvlText", "val") or f"%{int(level_id) + 1}.",
            )

    result: dict[tuple[str, str], NumberingLevel] = {}
    for num in root.findall(w_tag("num")):
        num_id = num.attrib.get(w_tag("numId"))
        abstract_id = child_attr(num, "abstractNumId", "val")
        if num_id is None or abstract_id is None:
            continue

        for (source_abstract_id, level_id), level in abstract_levels.items():
            if source_abstract_id == abstract_id:
                result[(num_id, level_id)] = level

        for override in num.findall(w_tag("lvlOverride")):
            level_id = override.attrib.get(w_tag("ilvl"), "0")
            base = result.get((num_id, level_id), NumberingLevel())
            start_override = child_attr(override, "startOverride", "val")
            result[(num_id, level_id)] = NumberingLevel(
                start=int(start_override or base.start),
                fmt=base.fmt,
                text=base.text,
            )

    return result


def text_style_from_props(props: ET.Element | None) -> TextStyle:
    if props is None:
        return TextStyle()

    strike = merged_toggle_props(props, "strike", "dstrike")
    return TextStyle(
        bold=toggle_prop(props, "b"),
        italic=toggle_prop(props, "i"),
        underline=underline_prop(props),
        strike=strike,
    )


def merge_text_styles(*styles: TextStyle | None) -> TextStyle:
    result = TextStyle()
    for style in styles:
        if style is None:
            continue
        for name in ("bold", "italic", "underline", "strike"):
            value = getattr(style, name)
            if value is not None:
                setattr(result, name, value)
    return result


def toggle_prop(props: ET.Element, name: str) -> bool | None:
    child = props.find(w_tag(name))
    if child is None:
        return None
    value = child.attrib.get(w_tag("val"))
    if value is None:
        return True
    return value.lower() not in {"0", "false", "off"}


def underline_prop(props: ET.Element) -> bool | None:
    child = props.find(w_tag("u"))
    if child is None:
        return None
    value = child.attrib.get(w_tag("val"))
    if value is None:
        return True
    return value.lower() not in {"0", "false", "off", "none"}


def merged_toggle_props(props: ET.Element, *names: str) -> bool | None:
    values = [toggle_prop(props, name) for name in names]
    if any(value is True for value in values):
        return True
    if any(value is False for value in values):
        return False
    return None


def read_docx_styles(archive: zipfile.ZipFile) -> dict[str, TextStyle]:
    try:
        styles_xml = archive.read("word/styles.xml")
    except KeyError:
        return {}

    root = ET.fromstring(styles_xml)
    direct_styles: dict[str, TextStyle] = {}
    based_on: dict[str, str] = {}
    for style in root.findall(w_tag("style")):
        style_id = style.attrib.get(w_tag("styleId"))
        if not style_id:
            continue
        direct_styles[style_id] = text_style_from_props(style.find(w_tag("rPr")))
        base_style = child_attr(style, "basedOn", "val")
        if base_style:
            based_on[style_id] = base_style

    resolved: dict[str, TextStyle] = {}

    def resolve(style_id: str, stack: set[str] | None = None) -> TextStyle:
        if style_id in resolved:
            return resolved[style_id]
        stack = stack or set()
        if style_id in stack:
            return TextStyle()
        stack.add(style_id)

        base = resolve(based_on[style_id], stack) if style_id in based_on else TextStyle()
        result = merge_text_styles(base, direct_styles.get(style_id))
        resolved[style_id] = result
        return result

    for style_id in direct_styles:
        resolve(style_id)
    return resolved


def render_docx_block(element: ET.Element, context: DocxContext) -> str:
    if element.tag == w_tag("p"):
        return render_docx_paragraph(element, context)
    if element.tag == w_tag("tbl"):
        return render_docx_table(element, context)
    return ""


def render_docx_paragraph(paragraph: ET.Element, context: DocxContext) -> str:
    attrs: dict[str, str] = {}
    props = paragraph.find(w_tag("pPr"))
    if props is not None:
        align = props.find(w_tag("jc"))
        if align is not None:
            value = align.attrib.get(w_tag("val"))
            if value:
                attrs["align"] = map_horizontal_align(value)

    paragraph_style_id = child_attr(props, "pStyle", "val") if props is not None else None
    paragraph_style = context.styles.get(paragraph_style_id or "", TextStyle())
    content = render_docx_children(paragraph, context, paragraph_style)
    number_label = context.next_number(paragraph)
    if number_label and compact_text(text_after_deletions(paragraph)):
        escaped_label = html.escape(number_label)
        if not compact_text(content).startswith(number_label):
            content = f"<b>{escaped_label}</b>&nbsp;&nbsp;&nbsp;&nbsp;{content}"
    if not compact_text(content):
        content = "<br>"

    return f"<p{render_attrs(attrs)}>{content}</p>"


def render_docx_children(
    element: ET.Element,
    context: DocxContext,
    base_style: TextStyle,
    initial_carry_style: TextStyle | None = None,
) -> str:
    parts = []
    carry_style = initial_carry_style or TextStyle()
    for child in element:
        if child.tag == w_tag("r"):
            rendered, carry_style = render_docx_run(child, context, base_style, carry_style)
        else:
            rendered = render_docx_inline(child, context, base_style, carry_style)
        if rendered:
            parts.append(rendered)
    return "".join(parts)


def render_docx_inline(
    element: ET.Element,
    context: DocxContext,
    base_style: TextStyle,
    carry_style: TextStyle | None = None,
) -> str:
    if element.tag == w_tag("r"):
        rendered, _ = render_docx_run(element, context, base_style, carry_style or TextStyle())
        return rendered
    if element.tag == w_tag("hyperlink"):
        rel_id = element.attrib.get(r_tag("id"))
        anchor = element.attrib.get(w_tag("anchor"))
        href = context.rels.get(rel_id or "", f"#{anchor}" if anchor else "#")
        content = render_docx_children(element, context, base_style, carry_style)
        return f"<a href={html.escape(href, quote=True)!r}>{content}</a>"
    if element.tag == w_tag("fldSimple"):
        return render_docx_children(element, context, base_style, carry_style)
    return ""


def render_docx_run(
    run: ET.Element,
    context: DocxContext,
    base_style: TextStyle,
    carry_style: TextStyle,
) -> tuple[str, TextStyle]:
    parts: list[str] = []
    has_break = False
    for child in run:
        if child.tag == w_tag("t"):
            parts.append(html.escape(child.text or ""))
        elif child.tag == w_tag("br"):
            has_break = True
            parts.append("<br>")
        elif child.tag == w_tag("tab"):
            parts.append("&nbsp;&nbsp;&nbsp;&nbsp;")
        elif child.tag == w_tag("drawing"):
            continue
        elif child.tag == w_tag("pict"):
            continue

    content = "".join(parts)
    if not content:
        return "", carry_style

    props = run.find(w_tag("rPr"))
    run_style_id = child_attr(props, "rStyle", "val") if props is not None else None
    effective_style = merge_text_styles(
        base_style,
        carry_style,
        context.styles.get(run_style_id or ""),
        text_style_from_props(props),
    )
    styles: list[str] = []
    if props is not None:
        color = child_attr(props, "color", "val")
        if color and color.lower() != "auto":
            styles.append(f"color: #{color}")

        highlight = child_attr(props, "highlight", "val")
        if highlight:
            styles.append(f"background-color: {highlight_to_css(highlight)}")

        shading = props.find(w_tag("shd"))
        fill = shading.attrib.get(w_tag("fill")) if shading is not None else None
        if fill and fill.lower() not in {"auto", "ffffff"}:
            styles.append(f"background-color: #{fill}")

    if effective_style.strike:
        styles.append("text-decoration: line-through")

    if effective_style.bold:
        content = f"<b>{content}</b>"
    if effective_style.italic:
        content = f"<i>{content}</i>"
    if effective_style.underline:
        content = f"<u>{content}</u>"

    if styles:
        content = f"<span style={'; '.join(styles)!r}>{content}</span>"

    return content, effective_style if has_break else carry_style


def render_docx_table(table: ET.Element, context: DocxContext) -> str:
    rows, removed_fragments = build_table_rows(table)
    colgroup = render_table_colgroup(table, rows)
    rendered_rows = []
    for row in rows:
        cells = []
        for cell in row:
            if cell["skip"]:
                continue
            assert isinstance(cell["element"], ET.Element)
            attrs = cell_attrs(cell)
            content = render_docx_cell(cell["element"], context)
            cells.append(f"<td{render_attrs(attrs)}>{content}</td>")
        if cells:
            rendered_rows.append("<tr>" + "".join(cells) + "</tr>")

    deleted_report_markers = "".join(
        f"<span style=\"background-color: #ff0000\">{html.escape(fragment)}</span>"
        for fragment in removed_fragments
    )
    table_attrs = {
        "cellspacing": "1",
        "cellpadding": "1",
        "border": "1",
        "width": "100%",
        "style": "table-layout: fixed",
    }
    table_html = f"<table{render_attrs(table_attrs)}>" + colgroup + "<tbody>" + "".join(rendered_rows) + "</tbody></table>"
    return deleted_report_markers + table_html


def render_table_colgroup(table: ET.Element, rows: list[list[dict[str, object]]]) -> str:
    widths = table_grid_widths(table) or inferred_table_widths(rows)
    widths = normalize_table_widths(widths, rows)
    if not widths:
        return ""

    total = sum(widths)
    if total <= 0:
        return ""

    columns = "".join(f'<col width="{format_percent(width / total * 100)}%">' for width in widths)
    return f"<colgroup>{columns}</colgroup>"


def table_grid_widths(table: ET.Element) -> list[int]:
    grid = table.find(w_tag("tblGrid"))
    if grid is None:
        return []

    widths = []
    for column in grid.findall(w_tag("gridCol")):
        width = column.attrib.get(w_tag("w"))
        if width and width.isdigit():
            widths.append(int(width))
    return widths


def normalize_table_widths(widths: list[int | float], rows: list[list[dict[str, object]]]) -> list[float]:
    total = sum(widths)
    if total <= 0:
        return [float(width) for width in widths]

    normalized = [float(width) for width in widths]
    for index, width in enumerate(widths):
        if width / total >= 0.03:
            continue
        if column_has_single_cell_content(rows, index):
            continue
        normalized[index] = 0.0
    return normalized


def column_has_single_cell_content(rows: list[list[dict[str, object]]], column_index: int) -> bool:
    for row in rows:
        for cell in row:
            if cell["skip"]:
                continue
            if int(cell["col"]) == column_index and int(cell["grid_span"]) == 1 and cell["visible_after_delete"]:
                return True
    return False


def inferred_table_widths(rows: list[list[dict[str, object]]]) -> list[float]:
    columns_count = 0
    for row in rows:
        for cell in row:
            columns_count = max(columns_count, int(cell["col"]) + int(cell["grid_span"]))
    if columns_count == 0:
        return []

    widths = [0.0] * columns_count
    for row in rows:
        for cell in row:
            element = cell["element"]
            assert isinstance(element, ET.Element)
            width = child_attr(element.find(w_tag("tcPr")), "tcW", "w")
            if not width or not width.isdigit():
                continue
            span = int(cell["grid_span"])
            share = int(width) / span
            for index in range(int(cell["col"]), int(cell["col"]) + span):
                widths[index] = max(widths[index], share)

    if not any(widths):
        return [1.0] * columns_count
    return [width or 1.0 for width in widths]


def format_percent(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".")


def build_table_rows(table: ET.Element) -> tuple[list[list[dict[str, object]]], list[str]]:
    rows: list[list[dict[str, object]]] = []
    for tr in table.findall(w_tag("tr")):
        col = 0
        cells = []
        for tc in tr.findall(w_tag("tc")):
            grid_span = int(child_attr(tc.find(w_tag("tcPr")), "gridSpan", "val") or "1")
            vmerge_el = tc.find(f"{w_tag('tcPr')}/{w_tag('vMerge')}")
            vmerge = None
            if vmerge_el is not None:
                vmerge = vmerge_el.attrib.get(w_tag("val"), "continue")
            cells.append({
                "element": tc,
                "col": col,
                "grid_span": grid_span,
                "vmerge": vmerge,
                "rowspan": 1,
                "skip": False,
                "visible_after_delete": cell_has_visible_content_after_delete(tc),
            })
            col += grid_span
        rows.append(cells)

    removed_fragments = []
    filtered_rows = []
    for row in rows:
        if row_has_visible_content_after_delete(row):
            filtered_rows.append(row)
            continue

        removed_text = compact_text(" ".join(cell_text_for_removed_row(cell) for cell in row))
        if removed_text:
            removed_fragments.append(removed_text)

    rows = filtered_rows

    for row_index, row in enumerate(rows):
        for cell in row:
            if cell["vmerge"] != "restart":
                continue
            span = 1
            col = cell["col"]
            for next_row in rows[row_index + 1:]:
                next_cell = next((item for item in next_row if item["col"] == col), None)
                if next_cell and next_cell["vmerge"] == "continue":
                    span += 1
                    next_cell["skip"] = True
                else:
                    break
            cell["rowspan"] = span

    return rows, removed_fragments


def cell_text_for_removed_row(cell: dict[str, object]) -> str:
    if cell["vmerge"] == "continue":
        return ""
    element = cell["element"]
    assert isinstance(element, ET.Element)
    return element_all_text(element)


def element_all_text(element: ET.Element) -> str:
    parts: list[str] = []
    for child in element:
        if child.tag == w_tag("t") and child.text:
            parts.append(child.text)
        elif child.tag in {w_tag("tab"), w_tag("br")}:
            parts.append(" ")
        else:
            parts.append(element_all_text(child))
    return "".join(parts)


def row_has_visible_content_after_delete(row: list[dict[str, object]]) -> bool:
    for cell in row:
        if cell["vmerge"] == "continue":
            continue
        if cell["visible_after_delete"]:
            return True
    return False


def cell_has_visible_content_after_delete(cell: ET.Element) -> bool:
    if cell.find(f".//{w_tag('tbl')}") is not None:
        return True
    return bool(compact_text(text_after_deletions(cell)))


def text_after_deletions(element: ET.Element) -> str:
    parts: list[str] = []
    for child in element:
        if child.tag == w_tag("r"):
            if run_is_delete_marker(child):
                continue
            for node in child:
                if node.tag == w_tag("t") and node.text:
                    parts.append(node.text)
                elif node.tag in {w_tag("tab"), w_tag("br")}:
                    parts.append(" ")
        else:
            parts.append(text_after_deletions(child))
    return "".join(parts)


def run_is_delete_marker(run: ET.Element) -> bool:
    props = run.find(w_tag("rPr"))
    if props is None:
        return False

    highlight = child_attr(props, "highlight", "val")
    if highlight and is_color_match(highlight_to_css(highlight), DELETE_COLORS):
        return True

    shading = props.find(w_tag("shd"))
    fill = shading.attrib.get(w_tag("fill")) if shading is not None else None
    if fill and is_color_match(f"#{fill}", DELETE_COLORS):
        return True

    return False


def cell_attrs(cell: dict[str, object]) -> dict[str, str]:
    element = cell["element"]
    assert isinstance(element, ET.Element)
    props = element.find(w_tag("tcPr"))
    attrs = {"valign": "middle"}
    if int(cell["grid_span"]) > 1:
        attrs["colspan"] = str(cell["grid_span"])
    if int(cell["rowspan"]) > 1:
        attrs["rowspan"] = str(cell["rowspan"])
    if date_like_text(compact_text(text_after_deletions(element))):
        attrs["align"] = "center"
    if props is not None:
        valign = child_attr(props, "vAlign", "val")
        if valign:
            attrs["valign"] = map_vertical_align(valign)
        shading = props.find(w_tag("shd"))
        fill = shading.attrib.get(w_tag("fill")) if shading is not None else None
        if fill and fill.lower() not in {"auto", "ffffff"}:
            attrs["style"] = f"background-color: #{fill}"
    attrs["style"] = cell_style(attrs["valign"], attrs.get("style"), attrs.get("align"))
    return attrs


def cell_style(valign: str, extra_style: str | None = None, text_align: str | None = None) -> str:
    styles = [
        "border: 1px solid #bfbfbf",
        "padding: 4px",
        f"vertical-align: {valign}",
        "overflow-wrap: anywhere",
        "word-break: break-word",
    ]
    if text_align:
        styles.append(f"text-align: {text_align}")
    if extra_style:
        styles.append(extra_style)
    return "; ".join(styles)


def render_docx_cell(cell: ET.Element, context: DocxContext) -> str:
    parts = []
    for child in cell:
        rendered = render_docx_block(child, context)
        if rendered:
            parts.append(rendered)
    return "".join(parts)


def paragraph_numbering(paragraph: ET.Element) -> tuple[str | None, str | None]:
    props = paragraph.find(w_tag("pPr"))
    if props is None:
        return None, None
    num_pr = props.find(w_tag("numPr"))
    if num_pr is None:
        return None, None
    num_id = child_attr(num_pr, "numId", "val")
    level = child_attr(num_pr, "ilvl", "val") or "0"
    return num_id, level


def render_attrs(attrs: dict[str, str]) -> str:
    return "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in attrs.items() if value)


def child_attr(parent: ET.Element | None, child_name: str, attr_name: str) -> str | None:
    if parent is None:
        return None
    child = parent.find(w_tag(child_name))
    if child is None:
        return None
    return child.attrib.get(w_tag(attr_name))


def highlight_to_css(value: str) -> str:
    value = value.lower()
    if value == "yellow":
        return "#ffff00"
    if value == "red":
        return "#ff0000"
    if value == "green":
        return "#00ff00"
    if re.fullmatch(r"[0-9a-f]{6}", value):
        return f"#{value}"
    return value


def map_horizontal_align(value: str) -> str:
    return {
        "both": "justify",
        "start": "left",
        "end": "right",
    }.get(value, value)


def map_vertical_align(value: str) -> str:
    return {
        "center": "middle",
    }.get(value, value)


def clean_for_bitrix(raw_html: str, converter_name: str = "unknown") -> tuple[str, dict[str, object]]:
    raw_html = raw_html.replace("\x00", "")
    css_rules = parse_css_rules(raw_html)
    parser = TreeParser()
    parser.feed(raw_html)

    body = find_first(parser.root, "body") or parser.root
    report = {
        "removed_fragments": [],
        "added_fragments": [],
        "warnings": [],
        "stats": {},
    }

    raw_table_issues = table_integrity_issues(raw_html)
    if raw_table_issues["unmatched_closing"]:
        report["warnings"].append(
            "Системный конвертер отдал HTML с поврежденной структурой таблиц. "
            "Для этого файла нужен LibreOffice как основной движок конвертации."
        )
    if converter_name == "textutil":
        report["warnings"].append(
            "Файл обработан через macOS textutil. Для сложных .doc с таблицами надежнее установить LibreOffice."
        )

    cleaned = transform_node(body, css_rules, report)
    cleaned = simplify_html_tree(cleaned)
    fragment = "".join(render_node(child) for child in cleaned.children)
    fragment = normalize_fragment(fragment)
    fragment = format_html_fragment(fragment)
    table_warnings = suspicious_table_warnings(fragment)
    report["warnings"].extend(table_warnings)

    report["stats"] = {
        "removed_count": len(report["removed_fragments"]),
        "added_count": len(report["added_fragments"]),
        "html_length": len(fragment),
        "tables_count": fragment.lower().count("<table"),
        "raw_table_unmatched_closing_tags": raw_table_issues["unmatched_closing"],
        "table_warnings_count": len(table_warnings),
    }

    output_table_issues = table_integrity_issues(fragment)
    if output_table_issues["unmatched_closing"] or output_table_issues["orphan_table_tags"]:
        report["warnings"].append(
            "В итоговом HTML обнаружены признаки поврежденной таблицы. Не вставляйте результат в Bitrix без ручной проверки."
        )
        report["stats"]["output_table_unmatched_closing_tags"] = output_table_issues["unmatched_closing"]
        report["stats"]["output_orphan_table_tags"] = output_table_issues["orphan_table_tags"]

    if not report["removed_fragments"]:
        report["warnings"].append("Фрагменты с красной заливкой не найдены.")
    if not report["added_fragments"]:
        report["warnings"].append("Желтые фрагменты не найдены.")

    return fragment, report


def parse_css_rules(raw_html: str) -> dict[str, dict[str, str]]:
    rules: dict[str, dict[str, str]] = {}
    for style_block in re.findall(r"<style[^>]*>(.*?)</style>", raw_html, flags=re.I | re.S):
        for selector, body in re.findall(r"([^{}]+)\{([^{}]+)\}", style_block):
            declarations = parse_style(body)
            for part in selector.split(","):
                part = part.strip()
                match = re.search(r"\.([a-zA-Z0-9_-]+)$", part)
                if match:
                    rules[match.group(1)] = declarations
    return rules


def parse_style(style: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in style.split(";"):
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        result[key.strip().lower()] = normalize_css_value(value)
    return result


def normalize_css_value(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def transform_node(node: Node, css_rules: dict[str, dict[str, str]], report: dict[str, object]) -> Node:
    if node.is_text:
        return Node(data=node.data)

    if node.tag in {"head", "style", "script", "meta", "link", "title"}:
        return Node("removed")

    props = effective_props(node, css_rules)
    marker = classify_props(props)
    text = compact_text(node.text_content())

    if marker == "delete" and node.tag not in {"table", "tbody", "thead", "tfoot", "tr", "td", "th"}:
        add_report_item(report, "removed_fragments", text)
        return Node("removed")

    new_node = Node(node.tag, dict(node.attrs))
    new_node.attrs.pop("class", None)

    if marker == "delete" and node.tag in {"td", "th"}:
        add_report_item(report, "removed_fragments", text)
        new_node.children = []
    else:
        for child in node.children:
            transformed = transform_node(child, css_rules, report)
            if transformed.tag != "removed":
                new_node.children.append(transformed)

    if new_node.tag == "tr" and not compact_text(new_node.text_content()) and not contains_tag(new_node, {"img", "table"}):
        return Node("removed")

    if new_node.tag in {"span", "b", "strong", "i", "em", "u", "p"} and not compact_text(new_node.text_content()) and not contains_tag(new_node, {"img", "table", "br"}):
        return Node("removed")

    if marker == "add":
        add_report_item(report, "added_fragments", text)

    new_node.attrs = normalize_attrs(new_node, props, marker)
    return new_node


def simplify_html_tree(root: Node) -> Node:
    root.children = simplify_children(root.children)
    return root


def simplify_children(children: list[Node]) -> list[Node]:
    simplified: list[Node] = []
    for child in children:
        simplified.extend(simplify_node(child))
    return merge_adjacent_nodes(simplified)


def simplify_node(node: Node) -> list[Node]:
    if node.is_text:
        return [node]

    tag = {"strong": "b", "em": "i"}.get(node.tag or "", node.tag)
    new_node = Node(tag, dict(node.attrs), simplify_children(node.children))

    if new_node.tag == "span" and not new_node.attrs:
        return new_node.children
    if new_node.tag in {"b", "i", "u"} and only_line_breaks(new_node):
        return new_node.children
    if new_node.tag in INLINE_TAGS and not compact_text(new_node.text_content()) and not contains_tag(new_node, {"br", "img", "table"}):
        return []

    return [new_node]


def merge_adjacent_nodes(nodes: list[Node]) -> list[Node]:
    merged: list[Node] = []
    for node in nodes:
        if node.is_text:
            if merged and merged[-1].is_text:
                merged[-1].data += node.data
            else:
                merged.append(node)
            continue

        if merged and can_merge_nodes(merged[-1], node):
            merged[-1].children.extend(node.children)
            merged[-1].children = merge_adjacent_nodes(merged[-1].children)
            continue

        merged.append(node)
    return merged


def can_merge_nodes(left: Node, right: Node) -> bool:
    return (
        not left.is_text
        and not right.is_text
        and left.tag == right.tag
        and left.attrs == right.attrs
        and left.tag in INLINE_TAGS
    )


def only_line_breaks(node: Node) -> bool:
    return all(child.tag == "br" or (child.is_text and not child.data.strip()) for child in node.children)


def effective_props(node: Node, css_rules: dict[str, dict[str, str]]) -> dict[str, str]:
    props: dict[str, str] = {}
    for cls in node.attrs.get("class", "").split():
        props.update(css_rules.get(cls, {}))
    props.update(parse_style(node.attrs.get("style", "")))
    return props


def classify_props(props: dict[str, str]) -> str | None:
    background = props.get("background-color", "") or props.get("background", "")

    if is_color_match(background, DELETE_COLORS):
        return "delete"
    if is_color_match(background, ADD_COLORS):
        return "add"
    return None


def is_color_match(value: str, colors: set[str]) -> bool:
    value = value.lower().replace(" ", "")
    return any(color in value for color in colors)


def normalize_attrs(node: Node, props: dict[str, str], marker: str | None) -> dict[str, str]:
    attrs = {key: value for key, value in node.attrs.items() if key not in {"style", "class"}}

    if node.tag == "table":
        attrs.setdefault("cellspacing", "1")
        attrs.setdefault("cellpadding", "1")
        attrs.setdefault("border", "1")

    if node.tag in {"td", "th"}:
        attrs.setdefault("valign", "middle")
        if "colspan" in node.attrs:
            attrs["colspan"] = node.attrs["colspan"]
        if "rowspan" in node.attrs:
            attrs["rowspan"] = node.attrs["rowspan"]

    style = filtered_style(node.tag or "", props, marker)
    if style:
        attrs["style"] = style

    if node.tag == "p" and "text-align" in props:
        align = props["text-align"]
        if align in {"left", "center", "right", "justify"}:
            attrs["align"] = align

    if node.tag == "a":
        href = attrs.get("href", "").strip()
        if href.startswith("x-apple") or href.startswith("file:"):
            attrs.pop("href", None)
        else:
            attrs["target"] = "_blank"

    return attrs


def filtered_style(tag: str, props: dict[str, str], marker: str | None) -> str:
    allowed = []
    base_keys = ["text-align", "color", "font-weight", "font-style", "text-decoration", "border", "border-collapse"]
    table_keys = ["width", "min-width", "height", "min-height", "padding", "vertical-align", "table-layout", "overflow-wrap", "word-break"]
    keys = base_keys + (table_keys if tag in {"table", "td", "th", "col"} else [])
    for key in keys:
        value = props.get(key)
        if not value:
            continue
        if key == "color" and is_default_text_color(value):
            continue
        allowed.append((key, value))

    if tag in {"td", "th"}:
        border = props.get("border") or props.get("border-style")
        if border and not any(key == "border" for key, _ in allowed):
            allowed.append(("border", "1px solid #bfbfbf"))

    if marker != "add":
        background = props.get("background-color")
        if background and not is_color_match(background, DELETE_COLORS | ADD_COLORS):
            allowed.append(("background-color", background))

    return "; ".join(f"{key}: {value}" for key, value in allowed)


def is_default_text_color(value: str) -> bool:
    return normalize_css_color(value) in DEFAULT_TEXT_COLORS


def normalize_css_color(value: str) -> str:
    return value.lower().replace(" ", "")


def suspicious_table_warnings(fragment: str) -> list[str]:
    parser = TreeParser()
    parser.feed(fragment)

    empty_colspan_count = 0
    date_rowspan_examples: list[str] = []
    date_followed_by_empty_examples: list[str] = []
    for table in iter_nodes(parser.root, "table"):
        rows = direct_children(table, "tr")
        for row_index, row in enumerate(rows):
            cells = [child for child in row.children if child.tag in {"td", "th"}]
            if len(cells) <= 1:
                continue

            for cell in cells:
                colspan = int_attr(cell, "colspan", 1)
                rowspan = int_attr(cell, "rowspan", 1)
                text = compact_text(cell.text_content())
                if colspan > 1 and not text:
                    empty_colspan_count += 1
                if rowspan > 1 and date_like_text(text):
                    context = action_number_summary(rows[row_index:row_index + rowspan])
                    date_rowspan_examples.append(f"{text} на {rowspan} строк{context}")

        date_followed_by_empty_examples.extend(date_cells_followed_by_empty_rows(rows))

    warnings = []
    if empty_colspan_count:
        warnings.append(
            "В таблицах найдены пустые объединенные ячейки внутри строк: "
            f"{empty_colspan_count}. Конвертер сохраняет их как в Word, но это может быть ошибкой исходного файла: "
            "если по смыслу там должны быть отдельные пустые ячейки, поправьте Word у инициатора."
        )
    if date_rowspan_examples:
        examples = "; ".join(date_rowspan_examples[:3])
        warnings.append(
            "В таблицах найдены ячейки с датой, растянутые на несколько строк: "
            f"{examples}. Конвертер сохраняет объединение как в Word, но проверьте, должна ли дата быть указана отдельно для каждой акции."
        )
    if date_followed_by_empty_examples:
        examples = "; ".join(date_followed_by_empty_examples[:3])
        warnings.append(
            "В таблицах найдены даты, после которых в том же столбце идут пустые ячейки: "
            f"{examples}. Конвертер сохраняет структуру Word, но проверьте у инициатора, относится ли дата к этим строкам или должна быть проставлена отдельно."
        )
    return warnings


def date_cells_followed_by_empty_rows(rows: list[Node]) -> list[str]:
    examples = []
    for index, row in enumerate(rows):
        cells = [child for child in row.children if child.tag in {"td", "th"}]
        if len(cells) <= 1:
            continue

        date_cell = cells[-1]
        date_text = compact_text(date_cell.text_content())
        if int_attr(date_cell, "rowspan", 1) > 1 or not date_like_text(date_text):
            continue

        empty_count = 0
        empty_rows = []
        for next_row in rows[index + 1:]:
            next_cells = [child for child in next_row.children if child.tag in {"td", "th"}]
            if len(next_cells) <= 1:
                break
            next_date_text = compact_text(next_cells[-1].text_content())
            next_main_text = compact_text(" ".join(cell.text_content() for cell in next_cells[:-1]))
            if not next_main_text:
                break
            if not next_date_text:
                empty_count += 1
                empty_rows.append(next_row)
                continue
            break

        if empty_count >= 2:
            context = action_number_summary(empty_rows)
            examples.append(f"{date_text}, затем {empty_count} пустые строки{context}")
    return examples


def action_number_summary(rows: list[Node]) -> str:
    numbers = []
    seen = set()
    for row in rows:
        for number in re.findall(r"\b\d{6,8}\b", compact_text(row.text_content())):
            if number in seen:
                continue
            seen.add(number)
            numbers.append(number)

    if not numbers:
        return ""
    if len(numbers) == 1:
        return f" (номер {numbers[0]})"
    return f" (номера {numbers[0]}...{numbers[-1]})"


def iter_nodes(node: Node, tag: str) -> Iterable[Node]:
    if node.tag == tag:
        yield node
    for child in node.children:
        yield from iter_nodes(child, tag)


def direct_children(node: Node, tag: str) -> list[Node]:
    result = []
    for child in node.children:
        if child.tag == tag:
            result.append(child)
        elif child.tag in {"tbody", "thead", "tfoot"}:
            result.extend(direct_children(child, tag))
    return result


def int_attr(node: Node, name: str, default: int) -> int:
    try:
        return int(node.attrs.get(name, str(default)))
    except ValueError:
        return default


def date_like_text(text: str) -> bool:
    text = compact_text(text)
    if len(text) > 80:
        return False
    return bool(re.search(r"\b\d{1,2}[./]\d{1,2}[./]\d{2,4}\b", text)) or text.lower().startswith("до ")


def table_integrity_issues(fragment: str) -> dict[str, int]:
    stack: list[str] = []
    unmatched_closing = 0
    orphan_table_tags = 0
    table_tags = {"table", "tbody", "thead", "tfoot", "tr", "td", "th"}
    for match in re.finditer(r"<\s*(/?)\s*(table|tbody|thead|tfoot|tr|td|th)\b[^>]*>", fragment, flags=re.I):
        closing, tag = match.group(1), match.group(2).lower()
        if not closing:
            if tag in {"tr", "td", "th"} and "table" not in stack:
                orphan_table_tags += 1
            stack.append(tag)
            continue

        if tag not in stack:
            unmatched_closing += 1
            continue

        while stack:
            opened = stack.pop()
            if opened == tag:
                break

    orphan_table_tags += sum(1 for tag in stack if tag in table_tags)
    return {"unmatched_closing": unmatched_closing, "orphan_table_tags": orphan_table_tags}


def find_first(node: Node, tag: str) -> Node | None:
    if node.tag == tag:
        return node
    for child in node.children:
        found = find_first(child, tag)
        if found:
            return found
    return None


def contains_tag(node: Node, tags: set[str]) -> bool:
    if node.tag in tags:
        return True
    return any(contains_tag(child, tags) for child in node.children)


def add_report_item(report: dict[str, object], key: str, value: str) -> None:
    if not value:
        return
    items = report[key]
    assert isinstance(items, list)
    if value not in items:
        items.append(value[:500])


def compact_text(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def normalize_fragment(fragment: str) -> str:
    fragment = fragment.replace("<br></br>", "<br>")
    fragment = re.sub(r"<p([^>]*)>\s*</p>", "", fragment)
    fragment = re.sub(r"\n{3,}", "\n\n", fragment)
    fragment = fragment.replace("<span class=\"Apple-converted-space\"> </span>", " ")
    return fragment.strip()


VOID_TAGS = {"br", "hr", "img", "meta", "link", "input", "col"}
INLINE_TAGS = {"a", "span", "b", "strong", "i", "em", "u", "font", "small", "sub", "sup"}
ONE_LINE_TAGS = INLINE_TAGS | {"p", "button"}


def format_html_fragment(fragment: str) -> str:
    parser = TreeParser()
    parser.feed(fragment)

    lines = []
    for child in parser.root.children:
        formatted = format_node(child, 0)
        if formatted:
            lines.append(formatted)

    return "\n".join(lines).strip()


def format_node(node: Node, level: int) -> str:
    indent = "  " * level
    if node.is_text:
        text = node.data.strip()
        return f"{indent}{text}" if text else ""

    if node.tag == "removed":
        return ""

    if node.tag in ONE_LINE_TAGS:
        return f"{indent}{render_node(node)}"

    attrs = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in node.attrs.items() if value != "")
    if node.tag in VOID_TAGS:
        return f"{indent}<{node.tag}{attrs}>"

    children = [format_node(child, level + 1) for child in node.children]
    children = [child for child in children if child]
    if not children:
        return f"{indent}<{node.tag}{attrs}></{node.tag}>"

    return "\n".join([
        f"{indent}<{node.tag}{attrs}>",
        *children,
        f"{indent}</{node.tag}>",
    ])


def render_node(node: Node) -> str:
    if node.is_text:
        return node.data
    if node.tag == "removed":
        return ""
    attrs = "".join(f' {key}="{html.escape(value, quote=True)}"' for key, value in node.attrs.items() if value != "")
    if node.tag in VOID_TAGS:
        return f"<{node.tag}{attrs}>"
    children = "".join(render_node(child) for child in node.children)
    return f"<{node.tag}{attrs}>{children}</{node.tag}>"


def batch_convert(input_paths: Iterable[Path], output_dir: Path) -> list[ConversionResult]:
    output_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for input_path in input_paths:
        safe_name = input_path.stem.replace(" ", "_") + ".html"
        results.append(convert_file(input_path, output_dir / safe_name))
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Convert Word files to Bitrix-compatible HTML.")
    parser.add_argument("input", nargs="+", type=Path, help="Input .doc/.docx files")
    parser.add_argument("--out-dir", type=Path, default=Path("outputs"), help="Output directory")
    args = parser.parse_args()

    results = batch_convert(args.input, args.out_dir)
    for result in results:
        stats = result.report["stats"]
        print(f"OK: {result.source_file} -> {result.output_file}")
        print(json.dumps(stats, ensure_ascii=False))
        warnings = result.report.get("warnings", [])
        if warnings:
            print("WARNINGS:")
            for warning in warnings:
                print(f"- {warning}")


if __name__ == "__main__":
    main()
