from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from planner_api.materials.parsers import MaterialParseError, parse_material


def test_builtin_formats_normalize_content_and_report_empty_or_oversized_inputs(
    tmp_path: Path,
) -> None:
    markdown = tmp_path / "brief.markdown"
    markdown.write_text("# 范围\n\n保持本地处理，不上传文件。", encoding="utf-8")
    parsed = parse_material(markdown)
    assert parsed.parser_name == "builtin-text"
    assert parsed.parser_version != "1"
    assert "保持本地处理" in parsed.text

    text_file = tmp_path / "note.txt"
    text_file.write_text("确认后的目标事实", encoding="utf-8")
    assert "确认后的目标事实" in parse_material(text_file).text

    html = tmp_path / "page.html"
    html.write_text(
        "<h1>标题</h1><p>用户要求 A&amp;B。</p><script>hidden secret</script>",
        encoding="utf-8",
    )
    parsed_html = parse_material(html)
    assert "用户要求 A&B。" in parsed_html.text
    assert "hidden" not in parsed_html.text

    xml_file = tmp_path / "feed.xml"
    xml_file.write_text("<root><entry>稳定事实</entry></root>", encoding="utf-8")
    assert "稳定事实" in parse_material(xml_file).text

    json_file = tmp_path / "state.json"
    json_file.write_text('{"status":"ready","count":3}', encoding="utf-8")
    assert '"status": "ready"' in parse_material(json_file).text

    csv_file = tmp_path / "table.csv"
    csv_file.write_text("项目,状态\n检索,进行中\n", encoding="utf-8")
    parsed_csv = parse_material(csv_file)
    assert "项目 | 状态" in parsed_csv.text
    assert parsed_csv.locations[0].detail == {"kind": "csv_row", "row": 1}

    empty = tmp_path / "empty.txt"
    empty.write_text(" \n\t", encoding="utf-8")
    with pytest.raises(MaterialParseError, match="没有可读取正文") as error:
        parse_material(empty)
    assert error.value.code == "EMPTY_DOCUMENT"

    with pytest.raises(MaterialParseError) as error:
        parse_material(markdown, max_chars=5)
    assert error.value.code == "EXTRACTED_TEXT_TOO_LARGE"


def test_docx_extracts_heading_paragraph_and_table_row_locations(tmp_path: Path) -> None:
    Document = pytest.importorskip("docx").Document
    path = tmp_path / "requirements.docx"
    document = Document()
    document.add_heading("硬约束", level=1)
    document.add_paragraph("每天最多投入三小时。")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "验收项"
    table.cell(0, 1).text = "100 页检索"
    document.save(path)

    parsed = parse_material(path)
    assert "每天最多投入三小时" in parsed.text
    assert "100 页检索" in parsed.text
    paragraph = next(
        item
        for item in parsed.locations
        if item.detail["kind"] == "docx_paragraph"
        and parsed.text[item.char_start : item.char_end] == "每天最多投入三小时。"
    )
    row = next(
        item for item in parsed.locations if item.detail["kind"] == "docx_table_row"
    )
    assert paragraph.detail["heading_path"] == ["硬约束"]
    assert row.detail["table"] == 1
    assert row.detail["row"] == 1
    assert parsed.text[paragraph.char_start : paragraph.char_end] == "每天最多投入三小时。"


def test_pptx_and_xlsx_keep_slide_sheet_and_cell_locations(tmp_path: Path) -> None:
    pptx = pytest.importorskip("pptx")
    presentation = pptx.Presentation()
    slide = presentation.slides.add_slide(presentation.slide_layouts[6])
    shape = slide.shapes.add_textbox(0, 0, 3000000, 500000)
    shape.text = "风险：浏览器缺失时回退文本读取"
    slide_path = tmp_path / "risks.pptx"
    presentation.save(slide_path)

    parsed_slide = parse_material(slide_path)
    assert "浏览器缺失" in parsed_slide.text
    slide_location = parsed_slide.locations[0]
    assert slide_location.detail["kind"] == "pptx_text_shape"
    assert slide_location.detail["slide"] == 1

    openpyxl = pytest.importorskip("openpyxl")
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.title = "实施约束"
    sheet.append(["事项", "结论"])
    sheet.append(["依赖", "默认不依赖 Chrome"])
    sheet_path = tmp_path / "constraints.xlsx"
    workbook.save(sheet_path)

    parsed_sheet = parse_material(sheet_path)
    assert "默认不依赖 Chrome" in parsed_sheet.text
    row_location = next(
        item
        for item in parsed_sheet.locations
        if item.detail["kind"] == "xlsx_row" and item.detail["row_start"] == 2
    )
    assert row_location.detail["sheet"] == "实施约束"
    assert row_location.detail["row_start"] == 2
    assert "B2" in row_location.detail["cell_refs"]


def test_pdf_extracts_page_numbers_and_rejects_corrupt_file(
    tmp_path: Path, pdf_bytes_factory
) -> None:
    pytest.importorskip("pdfplumber")
    path = tmp_path / "pages.pdf"
    path.write_bytes(
        pdf_bytes_factory(
            ["PAGE ONE ALPHA", "PAGE TWO BETA"],
            tables={2: [["Requirement", "Value"], ["Wait time", "45 minutes"]]},
        )
    )
    parsed = parse_material(path)
    assert parsed.metadata["page_count"] == 2
    assert "PAGE TWO BETA" in parsed.text
    assert [item.detail for item in parsed.locations] == [
        {"kind": "pdf_page", "page": 1},
        {"kind": "pdf_page", "page": 2},
        {"kind": "pdf_table_row", "page": 2, "table": 1, "row": 1},
        {"kind": "pdf_table_row", "page": 2, "table": 1, "row": 2},
    ]
    assert "45 minutes" in parsed.text

    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"not a valid PDF")
    with pytest.raises(MaterialParseError):
        parse_material(broken)


def test_xls_uses_markitdown_adapter(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    class FakeMarkItDown:
        def convert_local(self, path: Path) -> SimpleNamespace:
            calls.append(str(path))
            return SimpleNamespace(text_content="legacy spreadsheet cell")

    module = ModuleType("markitdown")
    module.MarkItDown = FakeMarkItDown
    monkeypatch.setitem(__import__("sys").modules, "markitdown", module)
    path = tmp_path / "legacy.xls"
    path.write_bytes(b"fixture placeholder")

    parsed = parse_material(path)
    assert parsed.parser_name == "markitdown"
    assert parsed.text == "legacy spreadsheet cell"
    assert calls == [str(path)]


def test_office_archive_expansion_limit_is_checked_before_parsing(tmp_path: Path) -> None:
    Document = pytest.importorskip("docx").Document
    path = tmp_path / "compressed.docx"
    document = Document()
    document.add_paragraph("small document")
    document.save(path)

    with pytest.raises(MaterialParseError) as error:
        parse_material(path, max_archive_uncompressed_bytes=1024)
    assert error.value.code == "ARCHIVE_TOO_LARGE"

    broken = tmp_path / "broken.docx"
    broken.write_text("not a ZIP archive", encoding="utf-8")
    with pytest.raises(MaterialParseError) as error:
        parse_material(broken)
    assert error.value.code == "INVALID_ARCHIVE"


def test_empty_docx_pptx_xlsx_and_pdf_are_not_indexed_as_content(
    tmp_path: Path, pdf_bytes_factory
) -> None:
    Document = pytest.importorskip("docx").Document
    empty_docx = tmp_path / "empty.docx"
    Document().save(empty_docx)

    pptx = pytest.importorskip("pptx")
    empty_pptx = tmp_path / "empty.pptx"
    presentation = pptx.Presentation()
    presentation.slides.add_slide(presentation.slide_layouts[6])
    presentation.save(empty_pptx)

    openpyxl = pytest.importorskip("openpyxl")
    empty_xlsx = tmp_path / "empty.xlsx"
    openpyxl.Workbook().save(empty_xlsx)

    pytest.importorskip("pdfplumber")
    empty_pdf = tmp_path / "empty.pdf"
    empty_pdf.write_bytes(pdf_bytes_factory([""]))

    for path in (empty_docx, empty_pptx, empty_xlsx, empty_pdf):
        with pytest.raises(MaterialParseError) as error:
            parse_material(path)
        assert error.value.code == "EMPTY_DOCUMENT", path.suffix
