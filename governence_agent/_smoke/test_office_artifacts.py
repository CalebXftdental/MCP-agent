"""Offline smoke checks for the office-artifact scaffold.

Run:  python _smoke/test_office_artifacts.py
"""
import json
import os
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "governance_core"))
sys.path.insert(0, str(ROOT / "mcp-office"))

from builders.excel import build_xlsx
from builders.powerpoint import build_pptx
from builders.word import build_docx
import artifact_store
from policy import manifest
from policy.categories import get_category

PASS, FAIL = 0, 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


print("manifest/category")
check("office category exists", get_category("office") is not None)
check("excel tool is namespaced", manifest.namespaced("create_excel_report") == "office_create_excel_report")
check("office backend registered", "office" in manifest.backends())

print("builders")
xlsx_name, xlsx_payload, _ = build_xlsx("Customer 360", [{"name": "Summary", "rows": [{"customer": "ABC", "total": 10}]}])
pptx_name, pptx_payload, _ = build_pptx("Customer 360", [{"heading": "Summary", "bullets": ["Good standing"]}])
docx_name, docx_payload, _ = build_docx("Customer 360", [{"heading": "Summary", "bullets": ["Good standing"]}], [{"name": "Orders", "rows": [{"order": "SO-1", "total": 10}]}])
check("xlsx extension", xlsx_name.endswith(".xlsx"))
check("pptx extension", pptx_name.endswith(".pptx"))
check("docx extension", docx_name.endswith(".docx"))
tmp_root = ROOT / "_smoke" / ".tmp"
tmp_root.mkdir(parents=True, exist_ok=True)
for label, payload, required in [
    ("xlsx", xlsx_payload, "xl/workbook.xml"),
    ("pptx", pptx_payload, "ppt/presentation.xml"),
    ("docx", docx_payload, "word/document.xml"),
]:
    tmp_path = tmp_root / f"office-smoke-{label}.zip"
    tmp_path.write_bytes(payload)
    with zipfile.ZipFile(tmp_path) as z:
        names = set(z.namelist())
        check(f"{label} contains {required}", required in names)
        check(f"{label} has document properties", "docProps/core.xml" in names and "docProps/app.xml" in names)
        if label == "xlsx":
            sheet = z.read("xl/worksheets/sheet1.xml").decode("utf-8")
            check("xlsx has styles", "xl/styles.xml" in names)
            check("xlsx has frozen header and filter", 'state="frozen"' in sheet and "<autoFilter" in sheet)
            check("xlsx writes numeric cells", "<v>10</v>" in sheet)
        if label == "pptx":
            slide = z.read("ppt/slides/slide1.xml").decode("utf-8")
            check("pptx has theme", "ppt/theme/theme1.xml" in names)
            check("pptx has styled slide layout", "Governed office assistant" in slide and "2FC7BA" in slide)
        if label == "docx":
            doc = z.read("word/document.xml").decode("utf-8")
            check("docx has styles and numbering", "word/styles.xml" in names and "word/numbering.xml" in names)
            check("docx includes real table", "<w:tbl>" in doc and "SO-1" in doc)

print("artifact store")
artifact_root = tmp_root / "artifacts-smoke"
artifact_root.mkdir(parents=True, exist_ok=True)
os.environ["GOVERNANCE_ARTIFACT_DIR"] = str(artifact_root)
rec = artifact_store.create_artifact(
    owner="tester", title="Smoke", filename="smoke.xlsx", payload=xlsx_payload,
    artifact_type="xlsx", mime_type="application/test", classification=["INTERNAL"],
)
loaded = artifact_store.get_artifact(rec.artifact_id)
listed = artifact_store.list_artifacts(owner="tester")
check("artifact persisted", loaded is not None and loaded.artifact_id == rec.artifact_id)
check("artifact listed by owner", any(r.artifact_id == rec.artifact_id for r in listed))
check("public dict has download url", loaded.public_dict()["downloadUrl"].endswith("/download"))
meta = Path(loaded.storage_path).parent / "metadata.json"
check("metadata is json", json.loads(meta.read_text(encoding="utf-8"))["artifact_id"] == rec.artifact_id)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
