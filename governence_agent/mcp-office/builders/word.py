from __future__ import annotations

import io
import zipfile

from builders.common import safe_filename, xml


MIME_DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _paragraph(text: str, style: str = "", bullet: bool = False) -> str:
    style_xml = f'<w:pStyle w:val="{style}"/>' if style else ""
    bullet_xml = '<w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr>' if bullet else ""
    p_pr = f'<w:pPr>{style_xml}{bullet_xml}</w:pPr>' if style_xml or bullet_xml else ""
    return f"<w:p>{p_pr}<w:r><w:t>{xml(text)}</w:t></w:r></w:p>"


def _headers(rows: list[dict], configured: list[str]) -> list[str]:
    if configured:
        return configured
    seen: list[str] = []
    for row in rows:
        for key in row.keys():
            if key not in seen:
                seen.append(key)
    return seen or ["message"]


def _table(table: dict) -> str:
    rows = list(table.get("rows") or []) or [{"message": "No rows supplied."}]
    headers = _headers(rows, list(table.get("columns") or []))
    grid = "".join('<w:gridCol w:w="2400"/>' for _ in headers)
    def cell(value, header=False):
        fill = '<w:shd w:fill="2FC7BA"/>' if header else ''
        bold = '<w:b/>' if header else ''
        color = '<w:color w:val="FFFFFF"/>' if header else ''
        return f'<w:tc><w:tcPr><w:tcW w:w="2400" w:type="dxa"/>{fill}</w:tcPr><w:p><w:r><w:rPr>{bold}{color}</w:rPr><w:t>{xml(value)}</w:t></w:r></w:p></w:tc>'
    body = ['<w:tr>' + "".join(cell(h, True) for h in headers) + '</w:tr>']
    for row in rows[:200]:
        body.append('<w:tr>' + "".join(cell(row.get(h, "")) for h in headers) + '</w:tr>')
    return f'''<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/><w:tblW w:w="0" w:type="auto"/><w:tblLook w:val="04A0"/></w:tblPr><w:tblGrid>{grid}</w:tblGrid>{''.join(body)}</w:tbl>'''


def _styles_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/><w:rPr><w:rFonts w:ascii="Aptos" w:hAnsi="Aptos"/><w:sz w:val="22"/><w:color w:val="1B1B1B"/></w:rPr></w:style>
 <w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:rPr><w:b/><w:sz w:val="38"/><w:color w:val="1B1B1B"/></w:rPr><w:pPr><w:spacing w:after="240"/></w:pPr></w:style>
 <w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:rPr><w:b/><w:sz w:val="28"/><w:color w:val="1A9C90"/></w:rPr><w:pPr><w:spacing w:before="300" w:after="120"/></w:pPr></w:style>
 <w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/><w:tblPr><w:tblBorders><w:top w:val="single" w:sz="4" w:color="E0E0E0"/><w:left w:val="single" w:sz="4" w:color="E0E0E0"/><w:bottom w:val="single" w:sz="4" w:color="E0E0E0"/><w:right w:val="single" w:sz="4" w:color="E0E0E0"/><w:insideH w:val="single" w:sz="4" w:color="E0E0E0"/><w:insideV w:val="single" w:sz="4" w:color="E0E0E0"/></w:tblBorders></w:tblPr></w:style>
</w:styles>'''


def _numbering_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="bullet"/><w:lvlText w:val="?"/><w:pPr><w:ind w:left="720" w:hanging="360"/></w:pPr></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="1"/></w:num></w:numbering>'''


def build_docx(title: str, sections: list[dict], tables: list[dict] | None = None) -> tuple[str, bytes, str]:
    filename = safe_filename(title, "office-report", "docx")
    body = [_paragraph(title or "Generated Report", "Title")]
    for section in sections or []:
        body.append(_paragraph(section.get("heading") or "Section", "Heading1"))
        if section.get("text"):
            body.append(_paragraph(section["text"]))
        for item in section.get("bullets") or []:
            body.append(_paragraph(str(item), bullet=True))
    for table in tables or []:
        body.append(_paragraph(table.get("name") or "Table", "Heading1"))
        body.append(_table(table))
    document = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>{''.join(body)}<w:sectPr><w:pgSz w:w="12240" w:h="15840"/><w:pgMar w:top="1080" w:right="1080" w:bottom="1080" w:left="1080"/></w:sectPr></w:body>
</w:document>'''
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
 <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
 <Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
 <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
 <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    doc_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/numbering" Target="numbering.xml"/>
</Relationships>'''
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{xml(title or "Office Report")}</dc:title><dc:creator>Governance Office Assistant</dc:creator></cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Governance Office Assistant</Application></Properties>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("word/document.xml", document)
        z.writestr("word/_rels/document.xml.rels", doc_rels)
        z.writestr("word/styles.xml", _styles_xml())
        z.writestr("word/numbering.xml", _numbering_xml())
    return filename, buf.getvalue(), MIME_DOCX
