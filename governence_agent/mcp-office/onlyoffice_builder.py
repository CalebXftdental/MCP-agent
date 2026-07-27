"""Translates a whitelisted, LLM-supplied edit request into an ONLYOFFICE Document
Builder script (the .js scripting API docbuilder runs headless, distinct from the
interactive editor embedded in the workbench). Kept separate from app.py so the
op-validation/script-generation logic (pure, no network) is testable on its own.

Only a small, explicit op vocabulary is supported -- letting the model emit
arbitrary Document Builder script would be an unaudited code-execution surface.
Whitelisting ops keeps every edit traceable to a specific, reviewable operation,
matching the rest of this backend's governance-first posture.
"""
from __future__ import annotations

import json

_OP_FIELDS = {
    "xlsx": {"set_cell": {"cell", "value"}},
    "docx": {"replace_text": {"find", "replace"}},
}


class InvalidEdits(ValueError):
    def __init__(self, error_code: str, **detail):
        super().__init__(error_code)
        self.error_code = error_code
        self.detail = detail


def parse_ops(artifact_type: str, edits: str) -> list[dict]:
    """Parse+validate the `edits` JSON string against the op whitelist for
    `artifact_type`. Raises InvalidEdits on anything not on the whitelist --
    callers should turn that into a governed tool error, not a 500."""
    allowed = _OP_FIELDS.get(artifact_type)
    if allowed is None:
        raise InvalidEdits("unsupported_artifact_type", artifactType=artifact_type)
    try:
        parsed = json.loads(edits)
    except (TypeError, ValueError) as exc:
        raise InvalidEdits("edits_not_json", detail=str(exc))
    if not isinstance(parsed, list) or not parsed:
        raise InvalidEdits("edits_must_be_a_nonempty_list")
    for index, op in enumerate(parsed):
        if not isinstance(op, dict):
            raise InvalidEdits("invalid_edit_entry", index=index)
        name = op.get("op")
        required = allowed.get(name)
        if required is None:
            raise InvalidEdits("unsupported_op", op=name, index=index, allowed=sorted(allowed))
        missing = required - op.keys()
        if missing:
            raise InvalidEdits("missing_fields", op=name, index=index, missing=sorted(missing))
    return parsed


def _js_str(value) -> str:
    return json.dumps(str(value))


def _xlsx_ops_js(ops: list[dict]) -> str:
    lines = []
    for op in ops:
        sheet = op.get("sheet")
        sheet_expr = f"Api.GetSheet({_js_str(sheet)})" if sheet else "Api.GetActiveSheet()"
        lines.append(f"{sheet_expr}.GetRange({_js_str(op['cell'])}).SetValue({_js_str(op['value'])});")
    return "\n".join(lines)


def _docx_ops_js(ops: list[dict]) -> str:
    return "\n".join(
        f"Api.GetDocument().SearchAndReplace({_js_str(op['find'])}, {_js_str(op['replace'])}, false);"
        for op in ops
    )


def build_script(artifact_type: str, source_url: str, out_filename: str, ops: list[dict]) -> str:
    """A synchronous Document Builder script: open the existing file at
    `source_url` (must itself be an absolute, Document-Server-reachable URL --
    see onlyoffice_jwt.scoped_download_url), apply `ops`, save as `out_filename`.
    """
    if artifact_type == "xlsx":
        body = _xlsx_ops_js(ops)
    elif artifact_type == "docx":
        body = _docx_ops_js(ops)
    else:
        raise InvalidEdits("unsupported_artifact_type", artifactType=artifact_type)
    return (
        f"builder.OpenFile({_js_str(source_url)});\n"
        f"{body}\n"
        f"builder.SaveFile({_js_str(artifact_type)}, {_js_str(out_filename)});\n"
        "builder.CloseFile();\n"
    )
