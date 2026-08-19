# Optional docx-template-merge document generation (Word/PDF)

## Context

Today, the "Templates" admin page and the office-document-generation tools
(`create_excel_report`, `create_word_report`, `create_powerpoint_deck`,
`create_pdf_packet` in `mcp-office/app.py`) never touch a real uploaded file.
Templates are JSON "shape" records (headings/bullets/tables), and every
document is hand-built from scratch as raw OOXML XML, with the LLM inventing
the whole structure each run. There is no way for a user to upload their
actual branded Excel/Word/PPTX file and have the agent fill it in — so output
formatting/branding is never guaranteed to match a real corporate template.

The user wants a **reliable** path to that: upload a real reference `.docx`,
and have generation actually merge data into that file's real placeholders
(not an LLM style-guide approximation). This must be **additive** — the
existing four `create_*` tools/builders stay exactly as they are; we add new,
separate tools alongside them. Scope for this pass: **Word and PDF only**
(via `docxtpl`, a real Jinja2-merge-field engine for `.docx`). Excel/PPTX
template-merge equivalents (openpyxl/python-pptx) are explicitly future work
— this plan should make that extension straightforward but does not build it.

## Approach

Reuse the existing template registry (`template_store.py`) and artifact
store (`artifact_store.py`) rather than inventing new storage: add a new
`template_type` (`"docx_merge"`) whose `content` dict (already a free-form
JSON blob — no dataclass changes needed) holds a `templateArtifactId`
pointing at the raw uploaded `.docx` bytes in `artifact_store`, plus
auto-discovered `mergeFields`. Two new MCP tools
(`create_word_report_from_template`, `create_pdf_from_template`) render that
template with LLM-supplied field values via `docxtpl`, mirroring the
existing tools' `artifact_store.create_artifact` output pattern. PDF reuses
the existing ONLYOFFICE Document Builder conversion mechanism
`edit_office_document` already depends on — no new PDF engine.

Because tool schemas for both chat and the workflow-builder step-config
panel are auto-derived from the `@mcp.tool()` function signatures
(`gateway/orchestrator.py:build_tool_specs`,
`gateway/backend/workflow_graphs.py:_workflow_graph_catalog`), the two new
tools need zero separate schema wiring — just the function + a
`ToolPolicy` manifest entry.

## Changes

### 1. `governance_core/docx_merge.py` (new)
Shared by both the gateway (field discovery) and mcp-office (rendering) —
this is the only directory both processes already add to `sys.path`.
```python
from io import BytesIO
from docxtpl import DocxTemplate

def discover_merge_fields(payload: bytes) -> list[str]:
    return sorted(DocxTemplate(BytesIO(payload)).get_undeclared_template_variables())

def render_docx(payload: bytes, context: dict) -> bytes:
    doc = DocxTemplate(BytesIO(payload))
    doc.render(context or {})
    out = BytesIO()
    doc.save(out)
    return out.getvalue()
```

### 2. `governance_core/template_store.py`
- Add `"docx_merge"` to `_VALID_TYPES` (line 18).
- `_validate()`: new branch requiring `content["templateArtifactId"]` for
  `docx_merge`.
- New `_augment_docx_merge_content(content)`: resolves
  `templateArtifactId` via `artifact_store.get_artifact` +
  `Path(record.storage_path).read_bytes()`, runs
  `docx_merge.discover_merge_fields`, and returns `content` merged with
  `mergeFields` + `sourceFilename`. Call this from `create_template()` and
  `add_version()` before `_validate(...)` whenever `template_type ==
  "docx_merge"`, so uploading/re-uploading a file always keeps the
  discovered field list current.

### 3. Upload path — no new routes
Reuse what exists, in two calls (both already admin-gated):
1. `POST /artifacts` (`gateway/backend/artifacts.py`) with the `.docx`
   base64, `artifact_type: "docx_template_source"` → returns `artifactId`.
2. `POST /templates` with `template_type: "docx_merge"`,
   `content: {"templateArtifactId": "<id from step 1>"}` →
   `template_store.create_template` auto-discovers and returns
   `content.mergeFields` in the same response.
Re-uploading a replacement file = repeat step 1, then
`POST /templates/{tid}/versions` with the new artifact id — existing route,
unchanged.

### 4. `mcp-office/builders/word_template.py` (new)
```python
import docx_merge
_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

def build_docx_from_template(filename, template_payload, context):
    payload = docx_merge.render_docx(template_payload, context)
    out_name = filename if filename.lower().endswith(".docx") else f"{filename}.docx"
    return out_name, payload, _MIME
```

### 5. `mcp-office/app.py` — two new tools
- `_resolve_docx_merge_template(owner, template_id)` helper: looks up the
  `TemplateRecord` via `template_store.get_template`, validates
  `template_type == "docx_merge"` and not disabled, resolves the latest
  version's `templateArtifactId` via `artifact_store.get_artifact`. Returns
  a governed error dict (`template_not_found` / `template_artifact_missing`)
  on failure, matching the existing `_artifact_or_error` error-shape
  convention.
- `create_word_report_from_template(owner, template_id, context: dict,
  title="", classification=None, filename="")`: resolves the template,
  calls `build_docx_from_template`, persists via `artifact_store.create_artifact`
  (`source_artifact_ids=[template_artifact.artifact_id]`), returns `_result(record)`.
- `create_pdf_from_template(owner, template_id, context: dict, title="",
  classification=None, filename="")`: same resolve + render to a **temporary**
  docx artifact (`retention_days=1`), then converts to PDF using the same
  ONLYOFFICE Document Builder flow `edit_office_document` uses
  (`onlyoffice_jwt.scoped_download_url`, `onlyoffice_builder.build_script`-style
  script, POST to `{server}/docbuilder`, download result, `artifact_store.create_artifact`
  for the final PDF, `delete_artifact` on the temp docx + script). Returns
  `onlyoffice_not_configured` if `ONLYOFFICE_DOCUMENT_SERVER_URL`/
  `GATEWAY_PUBLIC_URL` aren't set, exactly like `edit_office_document` does today.
- Add imports: `import template_store`, `from builders.word_template import build_docx_from_template`.

### 6. `mcp-office/onlyoffice_builder.py`
Add `build_convert_script(source_url, target_format, out_filename)` — same
`OpenFile`/`SaveFile`/`CloseFile` shape as `build_script`, but for a plain
format conversion with no edit ops (verified against `build_script`'s
existing structure at lines 74-90).

### 7. `governance_core/policy/manifest.py`
Two new `ToolPolicy` entries (`create_word_report_from_template`,
`create_pdf_from_template`), inserted next to the existing four office
entries, `backend="office"`, `risk=EXPORT`, `required_args=("template_id",)`,
same `fields={artifactId, filename, downloadUrl, classification, sizeBytes}`
shape. No changes needed to `policy/categories.py` (category is keyed on
`backend="office"` broadly).

### 8. Frontend — `gateway/frontend/src/pages/TemplatesPage.tsx`
- Add `docx_merge` to `TEMPLATE_TYPE_OPTIONS`.
- Extend the draft state with `docxTemplateArtifactId`, `docxMergeFields`,
  `docxSourceFilename`; wire through `draftFromContent`/`contentFromDraft`
  (mirroring the existing per-type branches).
- New `docx_merge` branch in the content-fields renderer: a real
  `<input type="file" accept=".docx">` that reads the file via
  `FileReader.readAsDataURL`, calls the existing `createArtifact(...)` API
  helper with `artifact_type: "docx_template_source"`, stores the returned
  `artifactId`, then read-only displays `docxSourceFilename` and the
  discovered `docxMergeFields` list once the template record comes back
  from `POST /templates`/`.../versions`.
- `gateway/frontend/src/lib/api.ts`: widen the `TemplateType` union to
  include `'docx_merge'`.
- **Out of scope for this pass**: a template-picker dropdown widget for the
  new tools' `template_id` argument in `StepConfigFields.tsx` — the default
  generic string-input field is acceptable for v1.

### 9. Dependencies
Add to **all three** requirements files (root `governence_agent/requirements.txt`
is what production's Oryx build actually installs; `mcp-office/` and
`gateway/` copies are for local dev parity — repo convention per `DEPLOY.md`
is to keep pins identical everywhere they appear):
```
docxtpl==0.19.1
python-docx==1.1.2
Jinja2==3.1.4
```
Pins are a starting point — verify they resolve together in a local venv
before deploying (no Dockerfile exists; Oryx's `pip install -r requirements.txt`
is the only build step to worry about).

## Verification

- New `_smoke/test_docx_merge.py`, following the existing
  `_smoke/test_templates.py` (HTTP `TestClient`) and
  `_smoke/test_office_artifacts.py` (direct builder calls) patterns:
  1. Synthesize a minimal `.docx` in-memory with `python-docx`
     (`doc.add_paragraph("Dear {{ customer_name }}, your total is {{ total }}.")`).
  2. Through the gateway `TestClient`: `POST /artifacts` (base64 docx) →
     `POST /templates` (`docx_merge`) → assert response
     `content.mergeFields == ["customer_name", "total"]`.
  3. Directly call `builders/word_template.build_docx_from_template` with a
     sample context, unzip the result, assert `word/document.xml` contains
     the merged text and no raw `{{ ... }}` tokens remain.
  4. Unit-test the new pure `onlyoffice_builder.build_convert_script()`
     string output (no network needed).
- PDF conversion (`create_pdf_from_template`) depends on a running
  ONLYOFFICE Document Server, which no existing test in this repo exercises
  either (`edit_office_document` has no offline coverage today) — verify
  manually via a local `docker run onlyoffice/documentserver` (see
  `ONLYOFFICE.md`) and a real chat/workflow call before shipping.
- Run the full `_smoke/` suite after changes to confirm no regression to
  the existing `test_templates.py`/`test_office_artifacts.py`/
  `test_tool_registration_consistency.py`.
