from __future__ import annotations

import re
from html import escape


def safe_filename(value: str, default: str, ext: str) -> str:
    stem = PathName.clean(value or default)
    if not stem.lower().endswith("." + ext):
        stem = f"{stem}.{ext}"
    return stem


def cell_text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        import json
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def xml(value) -> str:
    return escape(cell_text(value), quote=True)


class PathName:
    @staticmethod
    def clean(value: str) -> str:
        value = (value or "").strip()
        value = re.sub(r"[^A-Za-z0-9._ -]+", "", value)
        value = re.sub(r"\s+", "-", value).strip(".- ")
        return value[:80] or "artifact"
