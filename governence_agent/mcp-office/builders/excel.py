from __future__ import annotations

import io
import re
import zipfile

from builders.common import safe_filename, xml


MIME_XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _col_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name


def _sheet_name(value: str, fallback: str) -> str:
    value = re.sub(r"[\\/*?:\[\]]+", "", (value or fallback).strip())[:31]
    return value or fallback


def _headers(rows: list[dict], configured: list[str]) -> list[str]:
    if configured:
        return configured
    seen: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.append(key)
    return seen or ["message"]


def _is_number(value) -> bool:
    if isinstance(value, bool) or value is None:
        return False
    if isinstance(value, (int, float)):
        return True
    if isinstance(value, str):
        try:
            float(value.replace(",", ""))
            return bool(value.strip())
        except ValueError:
            return False
    return False


def _cell(ref: str, value, style: int = 0) -> str:
    s_attr = f' s="{style}"' if style else ""
    if _is_number(value):
        numeric = str(value).replace(",", "")
        return f'<c r="{ref}"{s_attr}><v>{xml(numeric)}</v></c>'
    text = "" if value is None else str(value)
    return f'<c r="{ref}" t="inlineStr"{s_attr}><is><t>{xml(text)}</t></is></c>'


def _rows_xml(headers: list[str], rows: list[dict]) -> tuple[str, int]:
    data_rows = rows or [{"message": "No rows supplied."}]
    out = []
    out.append('<row r="1" ht="22" customHeight="1">' + "".join(
        _cell(f"{_col_name(c_idx)}1", header, 1) for c_idx, header in enumerate(headers, start=1)
    ) + "</row>")
    for r_idx, row in enumerate(data_rows, start=2):
        cells = []
        for c_idx, header in enumerate(headers, start=1):
            value = row.get(header, "")
            style = 3 if _is_number(value) else 2
            cells.append(_cell(f"{_col_name(c_idx)}{r_idx}", value, style))
        out.append(f'<row r="{r_idx}">{"".join(cells)}</row>')
    return "".join(out), len(data_rows) + 1


def _sheet_xml(table: dict) -> str:
    rows = list(table.get("rows") or [])
    headers = _headers(rows, list(table.get("columns") or []))
    rows_xml, row_count = _rows_xml(headers, rows)
    last_col = _col_name(max(1, len(headers)))
    dimension = f"A1:{last_col}{max(1, row_count)}"
    col_defs = "".join(f'<col min="{i}" max="{i}" width="{min(42, max(12, len(str(h)) + 4))}" customWidth="1"/>' for i, h in enumerate(headers, start=1))
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <dimension ref="{dimension}"/>
 <sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/><selection pane="bottomLeft"/></sheetView></sheetViews>
 <cols>{col_defs}</cols>
 <sheetData>{rows_xml}</sheetData>
 <autoFilter ref="{dimension}"/>
 <pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>
</worksheet>'''


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
 <fonts count="3"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><color rgb="FFFFFFFF"/><name val="Calibri"/></font><font><sz val="11"/><color rgb="FF1B1B1B"/><name val="Calibri"/></font></fonts>
 <fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF2FC7BA"/><bgColor indexed="64"/></patternFill></fill></fills>
 <borders count="2"><border/><border><left style="thin"><color rgb="FFE0E0E0"/></left><right style="thin"><color rgb="FFE0E0E0"/></right><top style="thin"><color rgb="FFE0E0E0"/></top><bottom style="thin"><color rgb="FFE0E0E0"/></bottom></border></borders>
 <cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
 <cellXfs count="4"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="1" xfId="0" applyFont="1" applyFill="1" applyBorder="1"/><xf numFmtId="0" fontId="2" fillId="0" borderId="1" xfId="0" applyBorder="1"/><xf numFmtId="4" fontId="2" fillId="0" borderId="1" xfId="0" applyNumberFormat="1" applyBorder="1"/></cellXfs>
 <cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>'''


def build_xlsx(title: str, tables: list[dict]) -> tuple[str, bytes, str]:
    filename = safe_filename(title, "office-report", "xlsx")
    normalized = list(tables or []) or [{"name": "Report", "rows": []}]
    sheets_xml = []
    sheet_entries = []
    rel_entries = ['<Relationship Id="rIdStyle" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>']
    overrides = ['<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>']
    for i, table in enumerate(normalized[:12], start=1):
        name = _sheet_name(str(table.get("name") or ""), f"Sheet{i}")
        sheets_xml.append(_sheet_xml(table))
        sheet_entries.append(f'<sheet name="{xml(name)}" sheetId="{i}" r:id="rId{i}"/>')
        rel_entries.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
        overrides.append(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
    workbook = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
 <bookViews><workbookView xWindow="0" yWindow="0" windowWidth="21000" windowHeight="12000"/></bookViews>
 <sheets>{''.join(sheet_entries)}</sheets>
</workbook>'''
    rels = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 {''.join(rel_entries)}
</Relationships>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
 <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
 <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
 {''.join(overrides)}
</Types>'''
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{xml(title or "Office Report")}</dc:title><dc:creator>Governance Office Assistant</dc:creator></cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Governance Office Assistant</Application></Properties>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("xl/workbook.xml", workbook)
        z.writestr("xl/styles.xml", _styles_xml())
        z.writestr("xl/_rels/workbook.xml.rels", rels)
        for i, sheet in enumerate(sheets_xml, start=1):
            z.writestr(f"xl/worksheets/sheet{i}.xml", sheet)
    return filename, buf.getvalue(), MIME_XLSX
