"""Document extraction tests using dependency-light fixtures."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

import documents


def _docx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "word/document.xml",
            """<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
            <w:body>
              <w:p><w:r><w:t>项目周报</w:t></w:r></w:p>
              <w:p><w:r><w:t>完成文档解析</w:t></w:r></w:p>
            </w:body></w:document>""",
        )


def _xlsx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "xl/sharedStrings.xml",
            """<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <si><t>姓名</t></si><si><t>张三</t></si></sst>""",
        )
        archive.writestr(
            "xl/workbook.xml",
            """<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheets><sheet name="成员"/></sheets></workbook>""",
        )
        archive.writestr(
            "xl/worksheets/sheet1.xml",
            """<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
            <sheetData>
              <row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="inlineStr"><is><t>部门</t></is></c></row>
              <row r="2"><c r="A2" t="s"><v>1</v></c><c r="B2"><v>42</v></c></row>
            </sheetData></worksheet>""",
        )


def _pptx(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(
            "ppt/slides/slide1.xml",
            """<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <p:cSld><p:spTree><p:sp><p:txBody>
                <a:p><a:r><a:t>项目汇报</a:t></a:r></a:p>
                <a:p><a:r><a:t>下一步计划</a:t></a:r></a:p>
              </p:txBody></p:sp></p:spTree></p:cSld></p:sld>""",
        )


def test_docx_extraction(tmp_path: Path) -> None:
    path = tmp_path / "report.docx"
    _docx(path)

    result = documents.read_document(path)
    assert result["kind"] == "docx"
    assert "项目周报" in result["text"]
    assert "完成文档解析" in result["text"]

    cached = documents.read_document(path)
    assert cached["text"] == result["text"]
    key = (str(path.resolve()), path.stat().st_mtime_ns, path.stat().st_size, 0)
    assert key in documents._CACHE


def test_xlsx_extraction_preserves_sheet_and_cells(tmp_path: Path) -> None:
    path = tmp_path / "members.xlsx"
    _xlsx(path)

    result = documents.read_document(path)
    assert result["kind"] == "xlsx"
    assert "工作表: 成员" in result["text"]
    assert "姓名\t部门" in result["text"]
    assert "张三\t42" in result["text"]


def test_pptx_extraction_splits_slides(tmp_path: Path) -> None:
    path = tmp_path / "deck.pptx"
    _pptx(path)

    result = documents.read_document(path)
    assert result["kind"] == "pptx"
    assert "幻灯片 1" in result["text"]
    assert "项目汇报" in result["text"]


def test_simple_pdf_fallback(tmp_path: Path) -> None:
    path = tmp_path / "simple.pdf"
    path.write_bytes(
        b"%PDF-1.4\n1 0 obj\n<<>>\nstream\nBT (Hello PDF) Tj ET\nendstream\nendobj\n%%EOF"
    )

    result = documents.read_document(path)
    assert result["kind"] == "pdf"
    assert "Hello PDF" in result["text"]


def test_truncation_and_unsupported_format(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("a" * 500, encoding="utf-8")

    result = documents.read_document(path, max_chars=100)
    assert result["truncated"] is True
    assert "内容已截断" in result["text"]

    unsupported = tmp_path / "archive.zip"
    unsupported.write_bytes(b"PK\x03\x04")
    with pytest.raises(documents.DocumentError):
        documents.read_document(unsupported)
