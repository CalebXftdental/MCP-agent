from __future__ import annotations

import io
import zipfile

from builders.common import safe_filename, xml


MIME_PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
_ACCENT = "2FC7BA"
_TEXT = "1B1B1B"
_MUTED = "575C62"


def _text_run(text: str, size: int = 2400, bold: bool = False, color: str = _TEXT) -> str:
    b = '<a:b/>' if bold else ''
    return f'<a:r><a:rPr lang="en-US" sz="{size}">{b}<a:solidFill><a:srgbClr val="{color}"/></a:solidFill></a:rPr><a:t>{xml(text)}</a:t></a:r>'


def _shape(shape_id: int, name: str, x: int, y: int, cx: int, cy: int, paragraphs: str, fill: str = "FFFFFF", line: str = "E0E0E0") -> str:
    return f'''<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="{xml(name)}"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
 <p:spPr><a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom><a:solidFill><a:srgbClr val="{fill}"/></a:solidFill><a:ln><a:solidFill><a:srgbClr val="{line}"/></a:solidFill></a:ln></p:spPr>
 <p:txBody><a:bodyPr wrap="square" anchor="t"/><a:lstStyle/>{paragraphs}</p:txBody></p:sp>'''


def _paragraph(text: str, size: int = 2200, bold: bool = False, color: str = _TEXT, bullet: bool = False) -> str:
    bu = '<a:buChar char="?"/>' if bullet else ''
    margin = ' marL="285750" indent="-171450"' if bullet else ''
    return f'<a:p><a:pPr{margin}>{bu}</a:pPr>{_text_run(text, size=size, bold=bold, color=color)}</a:p>'


def _slide_xml(title: str, bullets: list[str], slide_no: int) -> str:
    bullet_paras = "".join(_paragraph(str(b), size=2100, bullet=True) for b in bullets[:8]) or _paragraph("No summary points supplied.", size=2100, color=_MUTED)
    kicker = _paragraph("Governed office assistant", size=1200, bold=True, color=_ACCENT)
    title_para = _paragraph(title, size=3400, bold=True)
    footer = _paragraph(f"Slide {slide_no}", size=1100, color=_MUTED)
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:bg><p:bgPr><a:solidFill><a:srgbClr val="F6F7F8"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
  {_shape(2, "Header", 548640, 420000, 11100000, 1350000, kicker + title_para, fill="FFFFFF", line="FFFFFF")}
  {_shape(3, "Accent", 548640, 1760000, 3500000, 140000, _paragraph("", size=100), fill=_ACCENT, line=_ACCENT)}
  {_shape(4, "Body", 548640, 2180000, 11100000, 3550000, bullet_paras, fill="FFFFFF", line="E7E7E7")}
  {_shape(5, "Footer", 10100000, 6300000, 1500000, 260000, footer, fill="F6F7F8", line="F6F7F8")}
 </p:spTree></p:cSld><p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sld>'''


def _slide_master_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:bg><p:bgPr><a:solidFill><a:schemeClr val="bg1"/></a:solidFill><a:effectLst/></p:bgPr></p:bg><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
 </p:spTree></p:cSld>
 <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2" accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6" hlink="hlink" folHlink="folHlink"/>
 <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rIdLayout"/></p:sldLayoutIdLst>
</p:sldMaster>'''


def _slide_layout_xml() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" type="blank" preserve="1">
 <p:cSld name="Blank"><p:spTree>
  <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/>
 </p:spTree></p:cSld>
 <p:clrMapOvr><a:masterClrMapping/></p:clrMapOvr>
</p:sldLayout>'''


def _theme_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Governance Office">
 <a:themeElements><a:clrScheme name="Governance"><a:dk1><a:srgbClr val="{_TEXT}"/></a:dk1><a:lt1><a:srgbClr val="FFFFFF"/></a:lt1><a:dk2><a:srgbClr val="{_MUTED}"/></a:dk2><a:lt2><a:srgbClr val="F6F7F8"/></a:lt2><a:accent1><a:srgbClr val="{_ACCENT}"/></a:accent1><a:accent2><a:srgbClr val="1A9C90"/></a:accent2><a:accent3><a:srgbClr val="767C84"/></a:accent3><a:accent4><a:srgbClr val="E7E7E7"/></a:accent4><a:accent5><a:srgbClr val="F0512A"/></a:accent5><a:accent6><a:srgbClr val="D7EFEC"/></a:accent6><a:hlink><a:srgbClr val="1A9C90"/></a:hlink><a:folHlink><a:srgbClr val="575C62"/></a:folHlink></a:clrScheme><a:fontScheme name="Office"><a:majorFont><a:latin typeface="Aptos Display"/></a:majorFont><a:minorFont><a:latin typeface="Aptos"/></a:minorFont></a:fontScheme><a:fmtScheme name="Office"><a:fillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:fillStyleLst><a:lnStyleLst><a:ln w="6350"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln></a:lnStyleLst><a:effectStyleLst><a:effectStyle><a:effectLst/></a:effectStyle></a:effectStyleLst><a:bgFillStyleLst><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:bgFillStyleLst></a:fmtScheme></a:themeElements>
</a:theme>'''


def build_pptx(title: str, sections: list[dict]) -> tuple[str, bytes, str]:
    filename = safe_filename(title, "office-deck", "pptx")
    slide_defs = [{"heading": title or "Generated Deck", "bullets": ["Generated by the governed office assistant."]}]
    slide_defs.extend(sections or [])
    slides = [_slide_xml(s.get("heading") or "Slide", list(s.get("bullets") or []), i) for i, s in enumerate(slide_defs[:12], start=1)]
    slide_ids = "".join(f'<p:sldId id="{256+i}" r:id="rId{i}"/>' for i in range(1, len(slides) + 1))
    presentation = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rIdMaster"/></p:sldMasterIdLst>
 <p:sldIdLst>{slide_ids}</p:sldIdLst>
 <p:sldSz cx="12192000" cy="6858000" type="screen16x9"/>
 <p:notesSz cx="6858000" cy="9144000"/>
 <p:defaultTextStyle><a:defPPr><a:defRPr lang="en-US"/></a:defPPr></p:defaultTextStyle>
</p:presentation>'''
    rels = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">']
    rels.append('<Relationship Id="rIdMaster" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>')
    for i in range(1, len(slides) + 1):
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i}.xml"/>')
    rels.append('<Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="theme/theme1.xml"/>')
    rels.append("</Relationships>")
    slide_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdLayout" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>'''
    slide_master_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdLayout" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
 <Relationship Id="rIdTheme" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>'''
    slide_layout_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rIdMaster" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
 <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
 <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
 <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''
    overrides = "".join(
        f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        for i in range(1, len(slides) + 1)
    )
    content_types = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="xml" ContentType="application/xml"/>
 <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
 <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
 <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
 <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
 <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
 <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
 {overrides}
</Types>'''
    core = f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>{xml(title or "Office Deck")}</dc:title><dc:creator>Governance Office Assistant</dc:creator></cp:coreProperties>'''
    app = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>Governance Office Assistant</Application></Properties>'''
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", content_types)
        z.writestr("_rels/.rels", root_rels)
        z.writestr("docProps/core.xml", core)
        z.writestr("docProps/app.xml", app)
        z.writestr("ppt/presentation.xml", presentation)
        z.writestr("ppt/_rels/presentation.xml.rels", "".join(rels))
        z.writestr("ppt/theme/theme1.xml", _theme_xml())
        z.writestr("ppt/slideMasters/slideMaster1.xml", _slide_master_xml())
        z.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", slide_master_rels)
        z.writestr("ppt/slideLayouts/slideLayout1.xml", _slide_layout_xml())
        z.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", slide_layout_rels)
        for i, slide in enumerate(slides, start=1):
            z.writestr(f"ppt/slides/slide{i}.xml", slide)
            z.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", slide_rels)
    return filename, buf.getvalue(), MIME_PPTX
