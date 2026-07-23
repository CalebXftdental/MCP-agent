"""Minimal PDF packet builder for governed office artifacts.

The goal is dependable local generation without adding a heavyweight rendering
stack. It produces simple text-first PDF packets suitable for review/approval
bundles; richer HTML/Docx rendering can replace this later behind the same API.
"""
from __future__ import annotations

import re
from textwrap import wrap

from builders.common import safe_filename


def _clean(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", text)
    return text.replace("\r", " ").strip()


def _escape_pdf(value: str) -> str:
    return value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def _packet_lines(title: str, sections: list[dict], tables: list[dict]) -> list[str]:
    lines = [_clean(title or "Office Packet"), ""]
    for section in sections or []:
        heading = _clean(section.get("heading") or section.get("title") or "Section")
        lines.extend([heading, ""])
        body = _clean(section.get("body") or section.get("summary") or "")
        if body:
            lines.extend(wrap(body, width=92) or [body])
        for bullet in section.get("bullets") or []:
            lines.extend(wrap("- " + _clean(bullet), width=92) or ["- " + _clean(bullet)])
        lines.append("")
    for table in tables or []:
        name = _clean(table.get("name") or "Table")
        rows = list(table.get("rows") or [])
        lines.extend([name, ""])
        if rows:
            headers = list(rows[0].keys()) if isinstance(rows[0], dict) else []
            if headers:
                lines.append(" | ".join(_clean(h) for h in headers))
                lines.append("-" * min(92, max(8, len(lines[-1]))))
                for row in rows[:80]:
                    if isinstance(row, dict):
                        lines.extend(wrap(" | ".join(_clean(row.get(h, "")) for h in headers), width=92) or [""])
            else:
                for row in rows[:80]:
                    lines.extend(wrap(_clean(row), width=92) or [""])
        else:
            lines.append("No rows")
        lines.append("")
    return lines or ["Office Packet"]


def _page_stream(lines: list[str]) -> str:
    y = 760
    ops = ["BT", "/F1 10 Tf", "72 760 Td"]
    first = True
    for line in lines:
        if not first:
            ops.append("0 -14 Td")
        first = False
        ops.append(f"({_escape_pdf(line)}) Tj")
        y -= 14
    ops.append("ET")
    return "\n".join(ops)


def build_pdf_packet(name: str, title: str, sections: list[dict], tables: list[dict] | None = None) -> tuple[str, bytes, str]:
    lines = _packet_lines(title, sections or [], tables or [])
    pages = [lines[i:i + 48] for i in range(0, len(lines), 48)] or [[title or "Office Packet"]]
    objects: list[str] = []
    objects.append("<< /Type /Catalog /Pages 2 0 R >>")
    page_count = len(pages)
    kids = " ".join(f"{3 + i * 2} 0 R" for i in range(page_count))
    objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {page_count} >>")
    for i, page in enumerate(pages):
        page_obj = 3 + i * 2
        content_obj = page_obj + 1
        objects.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {3 + page_count * 2} 0 R >> >> /Contents {content_obj} 0 R >>")
        stream = _page_stream(page)
        objects.append(f"<< /Length {len(stream.encode('latin-1', 'replace'))} >>\nstream\n{stream}\nendstream")
    objects.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    out = bytearray(b"%PDF-1.4\n% governed-office-packet\n")
    offsets = [0]
    for idx, obj in enumerate(objects, start=1):
        offsets.append(len(out))
        out.extend(f"{idx} 0 obj\n{obj}\nendobj\n".encode("latin-1", "replace"))
    xref = len(out)
    out.extend(f"xref\n0 {len(objects)+1}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(objects)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode("ascii"))
    return safe_filename(name or title or "office-packet", "office-packet", "pdf"), bytes(out), "application/pdf"
