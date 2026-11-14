"""Dependency-light document text extraction.

The agent often receives attachments that are not plain text.  This module
extracts readable text from PDF, Word, Excel, PowerPoint, HTML and common text
formats without importing heavyweight document libraries.  Office Open XML
formats are ZIP archives, so their XML can be read with the standard library.

PDF extraction prefers the system ``pdftotext`` utility (which understands
compressed streams, encodings and CJK fonts).  If it is unavailable, a small
fallback parser handles simple text PDFs.  Image OCR is deliberately not
implemented here: images are sent to a vision-capable model by the chat
runtime, which is more accurate than a local OCR binary and works for the
packaged desktop app.
"""

from __future__ import annotations

import html
import re
import shutil
import subprocess
import zipfile
import zlib
from pathlib import Path
from typing import Any, Dict, List
from xml.etree import ElementTree

from omni_agent.config import get_settings

TEXT_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".csv",
    ".tsv",
    ".json",
    ".jsonl",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".log",
    ".xml",
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".html",
    ".htm",
    ".css",
    ".sh",
    ".sql",
}

DOCUMENT_EXTENSIONS = {
    ".pdf",
    ".docx",
    ".xlsx",
    ".xlsm",
    ".pptx",
    ".html",
    ".htm",
    ".txt",
    *TEXT_EXTENSIONS,
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}

SUPPORTED_EXTENSIONS = DOCUMENT_EXTENSIONS | IMAGE_EXTENSIONS

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_S = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_PKG_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"


class DocumentError(RuntimeError):
    """Raised when a document cannot be read."""


def _limit(value: int | None = None) -> int:
    settings = get_settings()
    configured = int(getattr(settings, "document_max_chars", 30000) or 30000)
    if value is None or int(value) <= 0:
        return configured
    return max(100, min(int(value), configured))


def _read_office_xml(archive: zipfile.ZipFile, name: str) -> ElementTree.Element:
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise DocumentError(f"文档缺少 {name}") from exc
    try:
        return ElementTree.fromstring(raw)
    except ElementTree.ParseError as exc:
        raise DocumentError(f"无法解析 {name}: {exc}") from exc


def _docx_text(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        root = _read_office_xml(archive, "word/document.xml")
        lines: List[str] = []
        for paragraph in root.iter(f"{_W}p"):
            parts: List[str] = []
            for node in paragraph.iter():
                if node.tag == f"{_W}t" and node.text:
                    parts.append(node.text)
                elif node.tag == f"{_W}tab":
                    parts.append("\t")
                elif node.tag in {f"{_W}br", f"{_W}cr"}:
                    parts.append("\n")
            text = "".join(parts).strip()
            if text:
                lines.append(text)
        return {"kind": "docx", "text": "\n".join(lines), "page_count": None}


def _pptx_text(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        slides = sorted(
            (
                name
                for name in archive.namelist()
                if re.match(r"ppt/slides/slide\d+\.xml$", name)
            ),
            key=lambda name: int(re.search(r"(\d+)", name.split("/")[-1]).group(1)),
        )
        blocks: List[str] = []
        for index, name in enumerate(slides, start=1):
            root = _read_office_xml(archive, name)
            lines: List[str] = []
            for paragraph in root.iter(f"{_A}p"):
                text = "".join(
                    node.text or "" for node in paragraph.iter(f"{_A}t")
                ).strip()
                if text:
                    lines.append(text)
            if lines:
                blocks.append(f"--- 幻灯片 {index} ---\n" + "\n".join(lines))
        return {"kind": "pptx", "text": "\n\n".join(blocks), "page_count": len(slides)}


def _xlsx_text(path: Path) -> Dict[str, Any]:
    with zipfile.ZipFile(path) as archive:
        shared: List[str] = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = _read_office_xml(archive, "xl/sharedStrings.xml")
            for item in root.iter(f"{_S}si"):
                shared.append(
                    "".join(node.text or "" for node in item.iter(f"{_S}t"))
                )

        workbook = _read_office_xml(archive, "xl/workbook.xml")
        sheet_names = [
            sheet.get("name") or ""
            for sheet in workbook.iter(f"{_S}sheet")
        ]
        rels: List[str] = []
        if "xl/_rels/workbook.xml.rels" in archive.namelist():
            rel_root = _read_office_xml(archive, "xl/_rels/workbook.xml.rels")
            rels = [
                rel.get("Target") or ""
                for rel in rel_root.iter(f"{_PKG_REL}Relationship")
            ]
        sheet_files = sorted(
            (
                name
                for name in archive.namelist()
                if re.match(r"xl/worksheets/sheet\d+\.xml$", name)
            ),
            key=lambda name: int(re.search(r"(\d+)", name.split("/")[-1]).group(1)),
        )
        # Prefer relationship order; fall back to worksheet numeric order.
        ordered: List[str] = []
        for target in rels:
            candidate = target.lstrip("/")
            if candidate.startswith("xl/"):
                ordered.append(candidate)
            else:
                ordered.append(f"xl/{candidate}")
        ordered = [name for name in ordered if name in sheet_files]
        for name in sheet_files:
            if name not in ordered:
                ordered.append(name)

        blocks: List[str] = []
        for index, name in enumerate(ordered):
            root = _read_office_xml(archive, name)
            rows: List[str] = []
            for row in root.iter(f"{_S}row"):
                values: Dict[int, str] = {}
                for cell in row.iter(f"{_S}c"):
                    ref = cell.get("r") or ""
                    match = re.match(r"([A-Z]+)", ref)
                    col = 0
                    if match:
                        for char in match.group(1):
                            col = col * 26 + (ord(char) - 64)
                    cell_type = cell.get("t") or ""
                    value = ""
                    if cell_type == "inlineStr":
                        value = "".join(
                            node.text or "" for node in cell.iter(f"{_S}t")
                        )
                    else:
                        node = next(iter(cell.iter(f"{_S}v")), None)
                        raw = node.text if node is not None else ""
                        if cell_type == "s" and raw.isdigit():
                            idx = int(raw)
                            value = shared[idx] if idx < len(shared) else ""
                        else:
                            value = raw or ""
                    if value:
                        values[max(1, col)] = value
                if values:
                    last = max(values)
                    cells = [values.get(col, "") for col in range(1, last + 1)]
                    rows.append("\t".join(cells))
                if len(rows) >= 2000:
                    rows.append("... [行数过多，已截断]")
                    break
            sheet_name = sheet_names[index] if index < len(sheet_names) else f"Sheet{index + 1}"
            if rows:
                blocks.append(f"--- 工作表: {sheet_name} ---\n" + "\n".join(rows))
        return {"kind": "xlsx", "text": "\n\n".join(blocks), "page_count": len(ordered)}


def _pdf_simple_text(data: bytes) -> str:
    """Best-effort fallback for simple PDFs without pdftotext."""
    chunks: List[str] = []
    for match in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", data, re.S):
        raw = match.group(1)
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            pass
        text = raw.decode("latin-1", errors="ignore")
        if "BT" not in text:
            continue
        for token in re.finditer(r"\((?:[^()\\]|\\.)*\)\s*Tj|\[(?:[^\[\]]*)\]\s*TJ", text):
            value = token.group(0)
            if value.endswith("TJ"):
                inner = value[1 : value.rfind("]")]
                parts = re.findall(r"\((?:[^()\\]|\\.)*\)", inner)
                string = "".join(part[1:-1] for part in parts)
            else:
                string = value[1 : value.rfind(")")]
            string = re.sub(r"\\([nrtbf()\\])", lambda m: {
                "n": "\n", "r": "\r", "t": "\t", "b": "\b", "f": "\f",
                "(": "(", ")": ")", "\\": "\\",
            }.get(m.group(1), m.group(1)), string)
            chunks.append(string)
    return "\n".join(chunks)


def _pdf_text(path: Path) -> Dict[str, Any]:
    binary = shutil.which("pdftotext")
    if binary:
        try:
            proc = subprocess.run(
                [binary, "-layout", "-enc", "UTF-8", str(path), "-"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if proc.returncode == 0:
                return {
                    "kind": "pdf",
                    "text": proc.stdout,
                    "page_count": None,
                    "extractor": "pdftotext",
                }
        except (OSError, subprocess.TimeoutExpired):
            pass

    # PyMuPDF is optional; a packaged build may not include it.
    try:
        import fitz  # type: ignore

        document = fitz.open(path)
        try:
            pages = [page.get_text("text") for page in document]
            return {
                "kind": "pdf",
                "text": "\n\n".join(pages),
                "page_count": len(pages),
                "extractor": "pymupdf",
            }
        finally:
            document.close()
    except Exception:
        pass

    data = path.read_bytes()
    return {
        "kind": "pdf",
        "text": _pdf_simple_text(data),
        "page_count": None,
        "extractor": "fallback",
    }


def _html_text(path: Path, raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", "", raw)
    raw = re.sub(r"(?is)<br\s*/?>", "\n", raw)
    raw = re.sub(r"(?is)</(p|div|li|tr|h[1-6])>", "\n", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    text = html.unescape(text)
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def _ocr_image(path: Path) -> Dict[str, Any]:
    binary = shutil.which("tesseract")
    if not binary:
        raise DocumentError(
            "本机没有安装 tesseract，无法在 document_read 中做图片 OCR。"
            "请把图片作为对话附件发送，视觉模型可以直接识别图片内容。"
        )
    for language in ("chi_sim+eng", "eng"):
        try:
            proc = subprocess.run(
                [binary, str(path), "stdout", "-l", language],
                capture_output=True,
                text=True,
                timeout=60,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return {
                "kind": "image-ocr",
                "text": proc.stdout,
                "page_count": 1,
                "extractor": f"tesseract:{language}",
            }
    raise DocumentError(
        "图片 OCR 失败。请把图片作为对话附件发送，视觉模型可以直接识别图片内容。"
    )


def read_document(path: Path, max_chars: int | None = None) -> Dict[str, Any]:
    """Extract text from a supported document.

    The caller is responsible for path containment and existence checks.
    """
    path = Path(path)
    if not path.exists() or not path.is_file():
        raise DocumentError(f"文件不存在: {path.name}")
    settings = get_settings()
    max_bytes = int(getattr(settings, "document_max_bytes", 30 * 1024 * 1024) or 0)
    if max_bytes and path.stat().st_size > max_bytes:
        raise DocumentError(
            f"文档过大（{path.stat().st_size} bytes，上限 {max_bytes // (1024 * 1024)} MB）"
        )

    suffix = path.suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise DocumentError(f"不支持的文档格式: {suffix or '无扩展名'}")

    if suffix == ".pdf":
        result = _pdf_text(path)
    elif suffix == ".docx":
        result = _docx_text(path)
    elif suffix == ".pptx":
        result = _pptx_text(path)
    elif suffix in {".xlsx", ".xlsm"}:
        result = _xlsx_text(path)
    elif suffix in {".html", ".htm"}:
        raw = path.read_text(encoding="utf-8", errors="replace")
        result = {"kind": "html", "text": _html_text(path, raw), "page_count": None}
    elif suffix in IMAGE_EXTENSIONS:
        result = _ocr_image(path)
    else:
        result = {
            "kind": "text",
            "text": path.read_text(encoding="utf-8", errors="replace"),
            "page_count": None,
        }

    text = str(result.get("text") or "").strip()
    if not text:
        if suffix == ".pdf":
            raise DocumentError(
                "PDF 没有提取到文本，可能是扫描版。请把 PDF 页面截图或导出为图片，"
                "作为附件发送给视觉模型。"
            )
        raise DocumentError("文档中没有可提取的文本")

    limit = _limit(max_chars)
    truncated = len(text) > limit
    if truncated:
        text = text[:limit] + f"\n\n... [文档内容已截断，仅显示前 {limit} 字符]"
    return {
        "kind": result.get("kind", suffix.lstrip(".")),
        "text": text,
        "page_count": result.get("page_count"),
        "extractor": result.get("extractor", ""),
        "truncated": truncated,
        "size": path.stat().st_size,
        "name": path.name,
    }
