"""Generated artifacts: upload, preview, versions, reviews, shares, downloads."""
from __future__ import annotations

from auth import onlyoffice_jwt
from pathlib import Path
from policy import manifest
from starlette.responses import FileResponse
from starlette.responses import JSONResponse
from store import get_store
import approval_store
import artifact_share_store
import artifact_store
import audit
import base64
import document_review_store
import httpx
import json
import os
import time
import zipfile

from .deps import _effective_category_ids, _require_admin, _session, _unauthorized


def _artifact_expired(record) -> bool:
    return bool(record and record.expires_at is not None and record.expires_at <= time.time())


def _artifact_allowed(record, claims, permission: str = "view") -> bool:
    if not record or not claims:
        return False
    if record.owner == claims.get("name") or claims.get("role") == "admin":
        return True
    return artifact_share_store.active_share(record.artifact_id, claims.get("name", ""), permission) is not None


def _artifact_owner_or_admin(record, claims) -> bool:
    return bool(record and claims and (record.owner == claims.get("name") or claims.get("role") == "admin"))


def _artifact_public_for(record, claims) -> dict:
    data = record.public_dict()
    data["shared"] = bool(claims and record.owner != claims.get("name"))
    data["expired"] = _artifact_expired(record)
    return data


def _artifact_preview(record) -> dict:
    path = Path(record.storage_path)
    preview = {
        "kind": record.type,
        "filename": record.filename,
        "summary": [],
        "packageParts": [],
        "signals": {},
    }
    if not path.exists():
        return {**preview, "error": "artifact file is missing"}
    if record.type in ("xlsx", "pptx", "docx"):
        try:
            with zipfile.ZipFile(path) as z:
                names = sorted(z.namelist())
                preview["packageParts"] = names[:80]
                preview["signals"] = {
                    "hasCoreProperties": "docProps/core.xml" in names,
                    "hasAppProperties": "docProps/app.xml" in names,
                    "hasStyles": any(n.endswith("/styles.xml") or n == "xl/styles.xml" for n in names),
                    "hasTheme": any("/theme/" in n for n in names),
                }
                if record.type == "xlsx":
                    sheets = [n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
                    preview["signals"].update({"sheetCount": len(sheets), "hasAutoFilter": False, "hasFrozenPane": False})
                    for sheet_name in sheets[:3]:
                        sheet = z.read(sheet_name).decode("utf-8", "ignore")
                        preview["signals"]["hasAutoFilter"] = preview["signals"]["hasAutoFilter"] or "<autoFilter" in sheet
                        preview["signals"]["hasFrozenPane"] = preview["signals"]["hasFrozenPane"] or "state=\"frozen\"" in sheet
                    preview["summary"].append(f"Workbook with {len(sheets)} worksheet(s).")
                elif record.type == "pptx":
                    slides = [n for n in names if n.startswith("ppt/slides/slide") and n.endswith(".xml")]
                    preview["signals"]["slideCount"] = len(slides)
                    preview["summary"].append(f"Deck with {len(slides)} slide(s).")
                elif record.type == "docx":
                    doc = z.read("word/document.xml").decode("utf-8", "ignore") if "word/document.xml" in names else ""
                    preview["signals"].update({"hasTables": "<w:tbl" in doc, "paragraphCount": doc.count("<w:p")})
                    preview["summary"].append(f"Document with approximately {doc.count('<w:p')} paragraph(s).")
        except (OSError, zipfile.BadZipFile, KeyError) as exc:
            preview["error"] = str(exc)
    elif record.type == "email_draft" or record.filename.endswith(".json"):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            preview["signals"] = {
                "subject": raw.get("subject", ""),
                "toCount": len(raw.get("to") or []),
                "attachmentCount": len(raw.get("attachment_artifact_ids") or []),
            }
            preview["summary"].append(f"Email draft to {len(raw.get('to') or [])} recipient(s).")
        except (OSError, ValueError) as exc:
            preview["error"] = str(exc)
    elif record.type == "calendar_invite":
        try:
            sidecar = Path(record.storage_path).with_suffix(Path(record.storage_path).suffix + ".json")
            raw = json.loads(sidecar.read_text(encoding="utf-8")) if sidecar.exists() else {}
            preview["signals"] = {
                "title": raw.get("title", record.title),
                "start": raw.get("start", ""),
                "end": raw.get("end", ""),
                "timezone": raw.get("timezone", "UTC"),
                "attendeeCount": len(raw.get("attendees") or []),
                "attendees": ", ".join(raw.get("attendees") or []),
                "location": raw.get("location", ""),
            }
            preview["summary"].append(f"Calendar invite for {len(raw.get('attendees') or [])} attendee(s).")
        except (OSError, ValueError) as exc:
            preview["error"] = str(exc)
    else:
        preview["summary"].append("Binary artifact available for download.")
    return preview


def _onlyoffice_config(record, claims, request) -> dict | None:
    base = (os.getenv("ONLYOFFICE_DOCUMENT_SERVER_URL") or os.getenv("ONLYOFFICE_DOCSERVER_URL") or "").rstrip("/")
    if not base or record.type not in ("docx", "xlsx", "pptx", "pdf"):
        return None
    ext = record.type
    mode = "view" if str(os.getenv("ONLYOFFICE_EDIT_MODE") or "view").lower() != "edit" else "edit"
    # Document Server fetches/posts these URLs itself (it's a separate service, not
    # the browser) so they must be absolute -- request.base_url reflects whatever
    # host the caller actually used, same as the TrustedHostMiddleware host guard.
    gateway_base = str(request.base_url).rstrip("/")
    editor_config = {
        "mode": mode,
        "user": {"id": claims.get("sub", claims.get("name", "user")), "name": claims.get("name", "user")},
    }
    if mode == "edit":
        editor_config["callbackUrl"] = f"{gateway_base}/artifacts/{record.artifact_id}/onlyoffice/callback"
    config = {
        "document": {
            "fileType": ext,
            "key": f"{record.artifact_id}-{record.checksum[-16:]}",
            "title": record.filename,
            "url": onlyoffice_jwt.scoped_download_url(gateway_base, record.artifact_id),
            "permissions": {"download": True, "edit": mode == "edit", "print": True},
        },
        "editorConfig": editor_config,
    }
    token = onlyoffice_jwt.sign(config)
    if token:
        config["token"] = token
    return {
        "enabled": True,
        "documentServerUrl": base,
        "documentType": {"docx": "word", "xlsx": "cell", "pptx": "slide", "pdf": "pdf"}.get(ext, "word"),
        "config": config,
    }


def _decode_artifact_payload(body: dict, default_filename: str = "uploaded.txt") -> tuple[str, bytes, str | None]:
    filename = Path(str(body.get("filename") or default_filename)).name or default_filename
    text = body.get("text")
    content_b64 = body.get("content_base64")
    if content_b64:
        try:
            payload = base64.b64decode(str(content_b64), validate=True)
        except Exception:
            raise ValueError("content_base64 is invalid")
    elif text is not None:
        payload = str(text).encode("utf-8")
    else:
        raise ValueError("text or content_base64 is required")
    if len(payload) > 25 * 1024 * 1024:
        raise OverflowError("artifact upload limit is 25 MB")
    return filename, payload, body.get("mime_type")


async def _artifacts(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    try:
        limit = max(1, min(500, int(request.query_params.get("limit", "100"))))
    except ValueError:
        limit = 100
    if request.method == "POST":
        if claims.get("role") != "admin":
            store = get_store()
            record = store.get_consumer(claims["sub"])
            if record is None or "files" not in _effective_category_ids(store, record):
                return JSONResponse({"error": "your access does not include raw file uploads (files category)"}, status_code=403)
        try:
            body = await request.json() if request.headers.get("content-length") else {}
        except Exception:
            body = {}
        try:
            filename, payload, mime_override = _decode_artifact_payload(body)
        except OverflowError as exc:
            return JSONResponse({"error": str(exc)}, status_code=413)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        artifact_type = str(body.get("artifact_type") or Path(filename).suffix.lstrip(".") or "txt").lower()
        mime_type = str(mime_override or ("text/plain; charset=utf-8" if artifact_type in ("txt", "md", "csv") else "application/octet-stream"))
        retention_days = body.get("retention_days")
        try:
            retention_days = None if retention_days in (None, "") else int(retention_days)
        except (TypeError, ValueError):
            return JSONResponse({"error": "retention_days must be an integer"}, status_code=400)
        record = artifact_store.create_artifact(
            owner=claims["name"],
            title=str(body.get("title") or filename).strip() or filename,
            filename=filename,
            payload=payload,
            artifact_type=artifact_type,
            mime_type=mime_type,
            classification=list(body.get("classification") or [manifest.INTERNAL]),
            source_tool_calls=["artifact_upload"],
            retention_days=retention_days,
        )
        audit.log_policy_change(actor=claims["name"], action="upload_artifact", target=record.artifact_id, detail=record.filename)
        return JSONResponse(_artifact_public_for(record, claims), status_code=201)

    if claims.get("role") == "admin" and request.query_params.get("all") == "1":
        records = artifact_store.list_artifacts(owner=None, limit=limit)
    else:
        seen: dict[str, object] = {r.artifact_id: r for r in artifact_store.list_artifacts(owner=claims["name"], limit=limit)}
        for share in artifact_share_store.list_shares(shared_with=claims["name"]):
            if not artifact_share_store.is_active(share):
                continue
            record = artifact_store.get_artifact(share.artifact_id)
            if record is not None:
                seen[record.artifact_id] = record
        records = sorted(seen.values(), key=lambda r: r.created_at, reverse=True)[:limit]
    return JSONResponse({"artifacts": [_artifact_public_for(r, claims) for r in records]})


async def _artifact_metadata(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if request.method == "DELETE":
        if not _artifact_owner_or_admin(record, claims):
            return _unauthorized(is_admin=True)
        ok = artifact_store.delete_artifact(record.artifact_id)
        audit.log_policy_change(actor=claims["name"], action="delete_artifact", target=record.artifact_id, detail=record.filename)
        return JSONResponse({"ok": ok, "artifactId": record.artifact_id})
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    return JSONResponse(_artifact_public_for(record, claims))


async def _artifact_workbench(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    artifact = _artifact_public_for(record, claims)
    preview = _artifact_preview(record)
    approvals = [a.public_dict() for a in approval_store.list_approvals(requested_by=record.owner) if record.artifact_id in a.artifact_ids]
    shares = [x.public_dict() for x in artifact_share_store.list_shares(artifact_id=record.artifact_id, include_revoked=True)] if _artifact_owner_or_admin(record, claims) else []
    versions = [v.public_dict() for v in artifact_store.list_versions(record.artifact_id)]
    reviews = [r.public_dict() for r in document_review_store.list_reviews(artifact_id=record.artifact_id)]
    onlyoffice = _onlyoffice_config(record, claims, request)
    audit.log_policy_change(
        actor=claims["name"], action="inspect_artifact", target=record.artifact_id,
        detail=f"{record.filename}; classification={','.join(record.classification)}",
    )
    return JSONResponse({"artifact": artifact, "preview": preview, "approvals": approvals, "shares": shares, "versions": versions, "reviews": reviews, "onlyoffice": onlyoffice})


async def _artifact_reviews(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    if request.method == "GET":
        reviews = [r.public_dict() for r in document_review_store.list_reviews(artifact_id=record.artifact_id)]
        return JSONResponse({"artifact": _artifact_public_for(record, claims), "reviews": reviews})
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    provider = str(body.get("provider") or "local").strip() or "local"
    editor_url = str(body.get("editor_url") or body.get("editorUrl") or "").strip()
    if not editor_url and provider == "onlyoffice":
        oo = _onlyoffice_config(record, claims)
        editor_url = (oo or {}).get("documentServerUrl", "")
    review = document_review_store.create_review(
        artifact_id=record.artifact_id,
        artifact_version_id=str(body.get("artifact_version_id") or body.get("artifactVersionId") or record.latest_version_id or "latest"),
        requested_by=claims["name"],
        owner=record.owner,
        reason=str(body.get("reason") or f"Review {record.filename}"),
        provider=provider,
        editor_url=editor_url,
    )
    audit.log_policy_change(actor=claims["name"], action="create_document_review", target=review.review_id, detail=record.artifact_id)
    return JSONResponse(review.public_dict(), status_code=201)


async def _artifact_review_item(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    review = document_review_store.get_review(request.path_params["rid"])
    if review is None or review.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(review.public_dict())


async def _artifact_review_comments(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    review = document_review_store.get_review(request.path_params["rid"])
    if review is None or review.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
        updated = document_review_store.add_comment(
            review.review_id,
            author=claims["name"],
            body=str(body.get("body") or ""),
            anchor=str(body.get("anchor") or ""),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:
        return JSONResponse({"error": "invalid request"}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="comment_document_review", target=review.review_id, detail=record.artifact_id)
    return JSONResponse(updated.public_dict())


async def _artifact_review_decision(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    if claims.get("role") != "admin":
        return _unauthorized(is_admin=True)
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    review = document_review_store.get_review(request.path_params["rid"])
    if review is None or review.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    action = request.path_params["action"]
    status = "approved" if action == "approve" else "changes_requested" if action in ("changes", "changes-requested", "request-changes") else "closed" if action == "close" else ""
    if not status:
        return JSONResponse({"error": "unknown action"}, status_code=404)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
        updated = document_review_store.decide_review(review.review_id, status=status, decided_by=claims["name"], decision=str(body.get("decision") or body.get("note") or ""))
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action=f"{status}_document_review", target=review.review_id, detail=record.artifact_id)
    return JSONResponse(updated.public_dict())


async def _artifact_versions(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if request.method == "GET":
        if not _artifact_allowed(record, claims, "view"):
            return _unauthorized(is_admin=True)
        versions = [v.public_dict() for v in artifact_store.list_versions(record.artifact_id)]
        return JSONResponse({"artifact": _artifact_public_for(record, claims), "versions": versions})
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    try:
        filename, payload, mime_override = _decode_artifact_payload(body, default_filename=record.filename)
    except OverflowError as exc:
        return JSONResponse({"error": str(exc)}, status_code=413)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    version = artifact_store.create_version(
        artifact_id=record.artifact_id,
        payload=payload,
        filename=filename,
        mime_type=str(mime_override or record.mime_type),
        created_by=claims["name"],
        note=str(body.get("note") or ""),
    )
    if version is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    audit.log_policy_change(actor=claims["name"], action="create_artifact_version", target=record.artifact_id, detail=version.version_id)
    latest = artifact_store.get_artifact(record.artifact_id) or record
    return JSONResponse({"artifact": _artifact_public_for(latest, claims), "version": version.public_dict()}, status_code=201)


# ONLYOFFICE-only route: Document Server calls this server-to-server (no browser
# session cookie), so it's authenticated via the JWT `_onlyoffice_config` put in
# editorConfig.callbackUrl, not `_session`.
async def _artifact_onlyoffice_callback(request):
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": 1}, status_code=404)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": 1}, status_code=400)
    if onlyoffice_jwt.enabled():
        header_token = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
        # ONLYOFFICE signs either the raw callback body or (newer Document Server
        # versions) wraps it as {"payload": body} -- confirm which shape the
        # deployed Document Server version sends and adjust if verification
        # keeps failing against a real instance.
        if onlyoffice_jwt.verify(header_token or body.get("token")) is None:
            return JSONResponse({"error": 1}, status_code=403)
    # Callback status codes per ONLYOFFICE's editor callback contract: 2 = ready
    # for saving, 6 = force-saved while still being edited. Everything else
    # (editing in progress, closed with no changes, error) needs no action.
    if int(body.get("status") or 0) not in (2, 6):
        return JSONResponse({"error": 0})
    download_url = body.get("url")
    if not download_url:
        return JSONResponse({"error": 1}, status_code=400)
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(download_url)
            resp.raise_for_status()
            payload = resp.content
    except httpx.HTTPError as exc:
        return JSONResponse({"error": 1, "detail": str(exc)}, status_code=502)
    users = body.get("users") or []
    editor = (str(users[0]) if isinstance(users[0], str) else str(users[0].get("id", ""))) if users else "onlyoffice"
    version = artifact_store.create_version(
        artifact_id=record.artifact_id, payload=payload, filename=record.filename,
        mime_type=record.mime_type, created_by=editor, note="Edited via ONLYOFFICE",
    )
    if version is not None:
        audit.log_policy_change(
            actor=editor, action="create_artifact_version", target=record.artifact_id,
            detail=f"{version.version_id} (onlyoffice callback)",
        )
    return JSONResponse({"error": 0})


async def _artifact_version_download(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "download"):
        return _unauthorized(is_admin=True)
    version = artifact_store.get_version(record.artifact_id, request.path_params["vid"])
    if version is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not Path(version.storage_path).exists():
        return JSONResponse({"error": "artifact version file is missing"}, status_code=410)
    audit.log_policy_change(
        actor=claims["name"], action="download_artifact_version", target=record.artifact_id,
        detail=f"{version.version_id}; {version.filename}; classification={','.join(record.classification)}",
    )
    return FileResponse(version.storage_path, media_type=version.mime_type, filename=version.filename)


async def _artifact_shares(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    if request.method == "GET":
        shares = [s.public_dict() for s in artifact_share_store.list_shares(artifact_id=record.artifact_id, include_revoked=True)]
        return JSONResponse({"shares": shares})
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    expires_at = body.get("expires_at")
    if expires_at in (None, "") and body.get("expires_in_days") not in (None, ""):
        try:
            expires_at = time.time() + max(0, int(body.get("expires_in_days"))) * 86400
        except (TypeError, ValueError):
            return JSONResponse({"error": "expires_in_days must be an integer"}, status_code=400)
    elif expires_at not in (None, ""):
        try:
            expires_at = float(expires_at)
        except (TypeError, ValueError):
            return JSONResponse({"error": "expires_at must be a unix timestamp"}, status_code=400)
    else:
        expires_at = None
    try:
        share = artifact_share_store.create_share(
            artifact_id=record.artifact_id,
            owner=record.owner,
            shared_with=str(body.get("shared_with") or "").strip(),
            permissions=list(body.get("permissions") or ["view"]),
            created_by=claims["name"],
            expires_at=expires_at,
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    audit.log_policy_change(actor=claims["name"], action="share_artifact", target=record.artifact_id, detail=share.shared_with)
    return JSONResponse(share.public_dict(), status_code=201)


async def _artifact_share_revoke(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if not _artifact_owner_or_admin(record, claims):
        return _unauthorized(is_admin=True)
    share = artifact_share_store.get_share(request.path_params["sid"])
    if share is None or share.artifact_id != record.artifact_id:
        return JSONResponse({"error": "not found"}, status_code=404)
    updated = artifact_share_store.revoke_share(share.share_id, revoked_by=claims["name"])
    audit.log_policy_change(actor=claims["name"], action="revoke_artifact_share", target=record.artifact_id, detail=share.shared_with)
    return JSONResponse(updated.public_dict())


async def _admin_artifact_purge_expired(request):
    claims, err = _require_admin(request)
    if err:
        return err
    purged = artifact_store.purge_expired()
    audit.log_policy_change(actor=claims["name"], action="purge_expired_artifacts", target="artifact_store", detail=str(len(purged)))
    return JSONResponse({"purged": [r.public_dict() for r in purged], "count": len(purged)})


async def _artifact_request_approval(request):
    claims = _session(request)
    if not claims:
        return _unauthorized()
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not _artifact_allowed(record, claims, "view"):
        return _unauthorized(is_admin=True)
    try:
        body = await request.json() if request.headers.get("content-length") else {}
    except Exception:
        body = {}
    reason = str(body.get("reason") or f"Review artifact {record.filename}").strip()
    approval = approval_store.create_approval(
        requested_by=claims["name"], reason=reason, risk_level=str(body.get("risk_level") or "medium"),
        artifact_ids=[record.artifact_id], workflow_run_id=record.source_workflow_run_id,
    )
    audit.log_policy_change(actor=claims["name"], action="request_artifact_approval", target=approval.approval_id, detail=record.artifact_id)
    return JSONResponse(approval.public_dict(), status_code=201)


async def _artifact_download(request):
    record = artifact_store.get_artifact(request.path_params["aid"])
    if record is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    # ONLYOFFICE's Document Server (and Document Builder) fetch this URL
    # server-to-server with no browser session -- a short-lived, artifact-scoped
    # oo_token (minted into document.url by _onlyoffice_config /
    # onlyoffice_jwt.scoped_download_url) stands in for the session in that case.
    oo_token = request.query_params.get("oo_token")
    actor = "onlyoffice"
    if oo_token and onlyoffice_jwt.verify_access(oo_token, record.artifact_id, "download"):
        pass
    else:
        claims = _session(request)
        if not claims:
            return _unauthorized()
        if not _artifact_allowed(record, claims, "download"):
            return _unauthorized(is_admin=True)
        actor = claims["name"]
    if _artifact_expired(record):
        return JSONResponse({"error": "artifact expired"}, status_code=410)
    if not Path(record.storage_path).exists():
        return JSONResponse({"error": "artifact file is missing"}, status_code=410)
    audit.log_policy_change(
        actor=actor, action="download_artifact", target=record.artifact_id,
        detail=f"{record.filename}; classification={','.join(record.classification)}",
    )
    return FileResponse(record.storage_path, media_type=record.mime_type, filename=record.filename)
