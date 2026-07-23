"""Admin analytics + security-monitoring aggregations over the audit ring buffer.

These are read-only helpers (plus one intentionally-ephemeral alert-status map).
Everything reads `audit.recent(...)` + the policy store and derives its answers at
view time -- nothing here is a system of record; the audit log is.

Scope note: `audit.recent()` reads the in-memory ring (max 1000 events), so these
aggregations cover the recent window only. Longer horizons would need to read the
durable sink (Cosmos/JSONL) directly -- a deliberate follow-up, flagged where it
matters.
"""
from __future__ import annotations

import asyncio
import os
import time

import audit
import backends
import safety
from policy import manifest
from policy.resolve import resolve as resolve_grant
from store import get_store

_CALL_TYPES = ("call", "denied")
_SENSITIVE_LEVELS = {manifest.PII, manifest.SENSITIVE, manifest.PCI}

# Ranges the UI offers, in seconds.
RANGES = {"1h": 3600, "24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}

# Windows for the fixed-scope views (env-overridable).
ALERTS_WINDOW_SEC = int(os.getenv("GOVERNANCE_ALERTS_WINDOW_SEC") or 7 * 86400)
PROFILE_WINDOW_SEC = int(os.getenv("GOVERNANCE_PROFILE_WINDOW_SEC") or 30 * 86400)
HYGIENE_WINDOW_SEC = int(os.getenv("GOVERNANCE_HYGIENE_WINDOW_SEC") or 90 * 86400)
HEALTH_WINDOW_SEC = int(os.getenv("GOVERNANCE_HEALTH_WINDOW_SEC") or 86400)


# ── request-level caching ─────────────────────────────────────────────────────
# The dashboard endpoints recompute over the whole audit window (read + classify)
# and, for health, probe live backends. Cache results briefly so several admins
# polling the dashboard don't repeatedly do that work or hammer the backends.
# Staleness is bounded and fine for a near-real-time monitor; an ack/resolve
# invalidates the alerts entry so triage stays responsive.
_CACHE_TTL = {"overview": int(os.getenv("GOVERNANCE_CACHE_OVERVIEW_SEC") or 15),
              "alerts": int(os.getenv("GOVERNANCE_CACHE_ALERTS_SEC") or 8),
              "hygiene": int(os.getenv("GOVERNANCE_CACHE_HYGIENE_SEC") or 60),
              "health": int(os.getenv("GOVERNANCE_CACHE_HEALTH_SEC") or 30)}
_cache: dict = {}


def _cache_get(key):
    hit = _cache.get(key)
    return hit[1] if hit and hit[0] > time.monotonic() else None


def _cache_put(key, kind, value):
    _cache[key] = (time.monotonic() + _CACHE_TTL[kind], value)
    return value


def invalidate_cache(prefix: str = "") -> None:
    for k in [k for k in _cache if k.startswith(prefix)]:
        _cache.pop(k, None)


def overview_cached(range_sec: float = 86400) -> dict:
    key = f"overview:{int(range_sec)}"
    hit = _cache_get(key)
    return hit if hit is not None else _cache_put(key, "overview", overview(range_sec))


def alerts_cached() -> list[dict]:
    hit = _cache_get("alerts")
    return hit if hit is not None else _cache_put("alerts", "alerts", alerts())


def credential_hygiene_cached() -> list[dict]:
    hit = _cache_get("hygiene")
    return hit if hit is not None else _cache_put("hygiene", "hygiene", credential_hygiene())


async def backends_health_cached(probe: bool = True) -> list[dict]:
    key = f"health:{int(probe)}"
    hit = _cache_get(key)
    return hit if hit is not None else _cache_put(key, "health", await backends_health(probe=probe))


# ── shared helpers ────────────────────────────────────────────────────────────

def _calls(since_ts: float | None = None, limit: int = 1000) -> list[dict]:
    """Tool-call attempts only (reached, or were denied by, a tool's policy).

    With `since_ts`, reads the durable sink for the whole window (audit.read_since)
    so longer ranges aren't truncated to the in-memory ring; without it, the last
    `limit` ring events.
    """
    src = audit.read_since(since_ts) if since_ts is not None else audit.recent(limit)
    return [c for c in src if c.get("type") in _CALL_TYPES]


def _within(calls: list[dict], range_sec: float | None, now: float) -> list[dict]:
    if not range_sec:
        return calls
    cutoff = now - range_sec
    return [c for c in calls if (c.get("ts") or 0) >= cutoff]


def _tool_backend(namespaced_tool: str) -> str:
    canon = manifest.canonical(namespaced_tool or "")
    pol = manifest.get(canon) if canon else None
    return pol.backend if pol else "unknown"


def _is_sensitive_call(c: dict) -> bool:
    """True if the tool called can return PII/SENSITIVE/PCI-classified fields."""
    canon = manifest.canonical(c.get("tool") or "")
    pol = manifest.get(canon) if canon else None
    if not pol:
        return False
    return bool(set(pol.fields.values()) & _SENSITIVE_LEVELS)


def _redacted(c: dict) -> bool:
    return str(c.get("detail") or "").startswith("redacted:")


def _consumer_dept_map() -> dict[str, str]:
    # Audit records key `consumer` by ConsumerRecord.name.
    return {r.name: (r.department or "—") for r in get_store().consumers()}


def _count_by(calls: list[dict], keyfn) -> list[dict]:
    d: dict[str, int] = {}
    for c in calls:
        k = keyfn(c)
        d[k] = d.get(k, 0) + 1
    return [{"k": k, "n": n} for k, n in sorted(d.items(), key=lambda x: -x[1])]


def _last_key_rotation(consumer_id: str) -> float | None:
    """Latest key-issuance time, derived from policy-change audit events (no key
    timestamp is stored on the consumer record itself)."""
    for ch in audit.recent_policy_changes(1000):  # most-recent-first
        tool = ch.get("tool", "")
        action = tool.split(" ", 1)[0] if tool else ""
        if tool.endswith(" " + consumer_id) and action in ("rotate_key", "rotate_own_key", "create_consumer"):
            return ch.get("ts")
    return None


# ── overview (Monitor KPIs + charts + heatmap) ────────────────────────────────

def _series(calls: list[dict], range_sec: float, now: float) -> list[dict]:
    """Volume over time, bucketed to fit the range (hourly ≤24h, else daily)."""
    if range_sec <= 86400:
        nb, step, hourly = 24, 3600, True
    elif range_sec <= 7 * 86400:
        nb, step, hourly = 7, 86400, False
    else:
        nb, step, hourly = 30, 86400, False
    start = now - nb * step
    buckets = [{"ok": 0, "denied": 0, "error": 0, "_t": start + i * step} for i in range(nb)]
    for c in calls:
        ts = c.get("ts") or 0
        idx = int((ts - start) // step)
        if 0 <= idx < nb:
            b = buckets[idx]
            st = c.get("status")
            if st in ("ok", "denied", "error"):
                b[st] += 1
    for b in buckets:
        lt = time.localtime(b.pop("_t"))
        b["label"] = f"{lt.tm_hour:02d}:00" if hourly else f"{lt.tm_mon}/{lt.tm_mday}"
    return buckets


def _heatmap(calls: list[dict]) -> dict:
    """Calls per weekday(0=Mon) × hour(0-23), to surface off-hours access."""
    grid = [[0] * 24 for _ in range(7)]
    mx = 0
    for c in calls:
        ts = c.get("ts") or 0
        if not ts:
            continue
        lt = time.localtime(ts)
        grid[lt.tm_wday][lt.tm_hour] += 1
        mx = max(mx, grid[lt.tm_wday][lt.tm_hour])
    return {"grid": grid, "max": mx}


def overview(range_sec: float = 86400, now: float | None = None) -> dict:
    now = now or time.time()
    calls = _within(safety.classify(_calls(since_ts=now - range_sec)), range_sec, now)
    total = len(calls)
    ok = sum(1 for c in calls if c.get("status") == "ok")
    denied = sum(1 for c in calls if c.get("status") == "denied")
    error = sum(1 for c in calls if c.get("status") == "error")
    suspicious = sum(1 for c in calls if c.get("suspicious"))
    redactions = sum(1 for c in calls if _redacted(c))
    sensitive = sum(1 for c in calls if _is_sensitive_call(c))
    rows = sum((c.get("rows") or 0) for c in calls)
    lat = [c.get("latency_ms") or 0 for c in calls if c.get("status") == "ok"]
    dept = _consumer_dept_map()
    return {
        "range_sec": range_sec,
        "kpis": {
            "total": total, "ok": ok, "denied": denied, "error": error,
            "suspicious": suspicious, "redactions": redactions, "sensitive": sensitive,
            "rows": rows,
            "avg_latency": round(sum(lat) / len(lat), 1) if lat else 0,
            "auth_rate": round(ok / total * 100, 1) if total else 0,
            "denied_rate": round(denied / total * 100, 1) if total else 0,
        },
        "series": _series(calls, range_sec, now),
        "by_department": _count_by(calls, lambda c: dept.get(c.get("consumer"), "—")),
        "by_tool": _count_by(calls, lambda c: c.get("tool") or "—")[:8],
        "by_user": _count_by(calls, lambda c: c.get("consumer") or "—")[:8],
        "heatmap": _heatmap(calls),
    }


# ── alerts / incidents ────────────────────────────────────────────────────────
# safety.classify flags individual call rows; here we roll a consumer's flagged
# rows up into one triageable incident per (consumer, signal-type). Ack/resolve
# state is DURABLE: each action is written to the audit log as a policy_change
# (alert_ack/alert_resolve <id>), and current status is reconstructed from those
# events -- so triage survives restarts without a new store schema, and the state
# change is itself audited (who acknowledged what, when).

_SEVERITY = {"enumeration": "critical", "burst": "high", "denials": "high",
             "errors": "medium", "other": "low"}
_SEV_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3}


def _reason_type(reason: str) -> str:
    if "distinct customers" in reason:
        return "enumeration"
    if "access denials" in reason:
        return "denials"
    if "calls in" in reason:
        return "burst"
    if "errors on" in reason:
        return "errors"
    return "other"


def _alert_statuses() -> dict[str, dict]:
    """Latest ack/resolve per alert id, reconstructed from policy-change events
    (newest-first, first hit wins)."""
    m: dict[str, dict] = {}
    for ch in audit.recent_policy_changes(1000):
        action, _, target = (ch.get("tool") or "").partition(" ")
        if target and target not in m and action in ("alert_ack", "alert_resolve"):
            m[target] = {"status": "acknowledged" if action == "alert_ack" else "resolved",
                         "actor": ch.get("consumer"), "ts": ch.get("ts")}
    return m


def alerts(now: float | None = None) -> list[dict]:
    now = now or time.time()
    calls = safety.classify(_calls(since_ts=now - ALERTS_WINDOW_SEC))
    statuses = _alert_statuses()
    incidents: dict[str, dict] = {}
    for c in calls:
        if not c.get("suspicious"):
            continue
        consumer = c.get("consumer") or "unknown"
        ts = c.get("ts") or 0
        for reason in c.get("reasons", []):
            typ = _reason_type(reason)
            key = f"{consumer}:{typ}"
            inc = incidents.get(key)
            if inc is None:
                inc = incidents[key] = {
                    "id": key, "consumer": consumer, "type": typ,
                    "severity": _SEVERITY[typ], "events": 0,
                    "first_ts": ts, "last_ts": ts, "reason": reason, "_tools": set(),
                }
            inc["events"] += 1
            inc["first_ts"] = min(inc["first_ts"] or ts, ts)
            inc["last_ts"] = max(inc["last_ts"] or ts, ts)
            inc["reason"] = reason  # keep the latest phrasing (carries current counts)
            if c.get("tool"):
                inc["_tools"].add(c["tool"])
    name_to_id = {r.name: r.consumer_id for r in get_store().consumers()}
    out = []
    for inc in incidents.values():
        st = statuses.get(inc["id"], {})
        inc["tools"] = sorted(inc.pop("_tools"))[:6]
        inc["status"] = st.get("status", "open")
        inc["handled_by"] = st.get("actor")
        inc["handled_at"] = st.get("ts")
        inc["consumer_id"] = name_to_id.get(inc["consumer"])  # for one-click containment
        out.append(inc)
    out.sort(key=lambda i: (i["status"] != "open", _SEV_ORDER.get(i["severity"], 9), -(i["last_ts"] or 0)))
    return out


# ── per-consumer risk profile ─────────────────────────────────────────────────

def consumer_profile(consumer_id: str) -> dict | None:
    store = get_store()
    rec = store.get_consumer(consumer_id)
    if not rec:
        return None
    now = time.time()
    name = rec.name
    mine = [c for c in safety.classify(_calls(since_ts=now - PROFILE_WINDOW_SEC)) if c.get("consumer") == name]

    granted: set[str] = set()
    grant = resolve_grant(rec, store.get_category, store.get_department)
    if grant.all_tools:
        for b in manifest.backends():
            for t in manifest.tools_for_backend(b):
                granted.add(manifest.namespaced(t, b))
    else:
        for b, tools in grant.tools_by_backend.items():
            for t in tools:
                granted.add(manifest.namespaced(t, b))
    used = {c.get("tool") for c in mine if c.get("status") in ("ok", "error") and c.get("tool")}

    return {
        "consumer_id": consumer_id, "name": name, "full_name": rec.full_name,
        "department": rec.department, "role": rec.role, "type": rec.type, "status": rec.status,
        "has_key": bool(rec.key_hash), "rate_limit_per_hour": rec.rate_limit_per_hour,
        "totals": {
            "total": len(mine),
            "ok": sum(1 for c in mine if c.get("status") == "ok"),
            "denied": sum(1 for c in mine if c.get("status") == "denied"),
            "error": sum(1 for c in mine if c.get("status") == "error"),
            "suspicious": sum(1 for c in mine if c.get("suspicious")),
            "last_24h": len(_within(mine, 86400, now)),
            "last_7d": len(_within(mine, 7 * 86400, now)),
        },
        "distinct_customers": len({c.get("customer_id") for c in mine if c.get("customer_id")}),
        "rows_returned": sum((c.get("rows") or 0) for c in mine),
        "sensitive_calls": sum(1 for c in mine if _is_sensitive_call(c)),
        "redactions": sum(1 for c in mine if _redacted(c)),
        "ips": sorted({c.get("client_ip") for c in mine if c.get("client_ip")}),
        "last_used": max((c.get("ts") or 0 for c in mine), default=None),
        "last_key_rotation": rec.key_rotated_at or rec.key_created_at or _last_key_rotation(consumer_id),
        "granted_tools": sorted(granted),
        "used_tools": sorted(t for t in used if t),
        "unused_grants": sorted(granted - used),  # least-privilege cleanup list
        "reasons": sorted({r for c in mine for r in c.get("reasons", [])}),
    }


# ── credential hygiene ────────────────────────────────────────────────────────

def credential_hygiene(now: float | None = None) -> list[dict]:
    now = now or time.time()
    store = get_store()
    last_use: dict[str, float] = {}
    for c in _calls(since_ts=now - HYGIENE_WINDOW_SEC):
        n, ts = c.get("consumer"), c.get("ts") or 0
        if n and ts > last_use.get(n, 0):
            last_use[n] = ts
    out = []
    for r in store.consumers():
        if not r.key_hash:  # login-only / no API credential -> not key hygiene
            continue
        lu = last_use.get(r.name)
        # Prefer the persisted key timestamp; fall back to the audit-derived one
        # for keys minted before key_rotated_at was recorded.
        lr = r.key_rotated_at or r.key_created_at or _last_key_rotation(r.consumer_id)
        flags = []
        if lu is None:
            flags.append("never used")
        elif now - lu > 30 * 86400:
            flags.append("dormant >30d")
        if lr is None:
            flags.append("rotation unknown")
        elif now - lr > 90 * 86400:
            flags.append("key >90d old")
        out.append({
            "consumer_id": r.consumer_id, "name": r.name, "role": r.role, "type": r.type,
            "status": r.status,
            "key_age_days": round((now - lr) / 86400, 1) if lr else None,
            "idle_days": round((now - lu) / 86400, 1) if lu else None,
            "last_used": lu, "last_rotation": lr, "flags": flags,
        })
    out.sort(key=lambda x: (-len(x["flags"]), -(x["idle_days"] or 0)))
    return out


# ── backend health (live probe + recent-traffic error signal) ─────────────────

async def backends_health(now: float | None = None, probe: bool = True) -> list[dict]:
    """Per-backend status combining a live liveness probe with the recent-traffic
    error signal. Probes are deduped by URL (the miniERP domains share one server)
    and run concurrently, so this stays fast even with several backends."""
    now = now or time.time()

    # Recent-traffic error signal.
    by: dict[str, dict] = {}
    for c in _calls(since_ts=now - HEALTH_WINDOW_SEC):
        if c.get("status") not in ("ok", "error"):
            continue
        b = _tool_backend(c.get("tool", ""))
        e = by.setdefault(b, {"calls": 0, "errors": 0, "last_ok": None,
                              "last_error_ts": None, "last_error": None})
        e["calls"] += 1
        ts = c.get("ts") or 0
        if c.get("status") == "error":
            e["errors"] += 1
            if ts > (e["last_error_ts"] or 0):
                e["last_error_ts"], e["last_error"] = ts, c.get("detail")
        elif ts > (e["last_ok"] or 0):
            e["last_ok"] = ts

    names = sorted(manifest.backends())
    url_of: dict[str, str | None] = {}
    for b in names:
        try:
            url_of[b] = backends.backend_url(b)
        except Exception:
            url_of[b] = None

    # Probe each distinct URL once (a representative backend name per URL).
    probes: dict[str, dict] = {}
    if probe:
        rep: dict[str, str] = {}
        for b in names:
            u = url_of[b]
            if u and u not in rep:
                rep[u] = b
        urls = list(rep)
        results = await asyncio.gather(*(backends.ping(rep[u]) for u in urls), return_exceptions=True)
        for u, res in zip(urls, results):
            probes[u] = res if isinstance(res, dict) else {"ok": False, "latency_ms": None, "error": str(res)}

    out = []
    for b in names:
        e = by.get(b, {"calls": 0, "errors": 0, "last_ok": None, "last_error_ts": None, "last_error": None})
        rate = (e["errors"] / e["calls"]) if e["calls"] else 0.0
        recent_error = bool(e["last_error_ts"]) and (not e["last_ok"] or e["last_error_ts"] > e["last_ok"])
        pr = probes.get(url_of[b])
        if pr is not None:  # live probe wins
            status = "down" if not pr["ok"] else ("degraded" if (rate > 0.25 or recent_error) else "healthy")
        elif e["calls"] == 0:
            status = "idle"
        else:
            status = "degraded" if (rate > 0.25 or recent_error) else "healthy"
        out.append({
            "backend": b, "url": url_of[b], "calls": e["calls"], "errors": e["errors"],
            "error_rate": round(rate, 3), "last_ok": e["last_ok"],
            "last_error_ts": e["last_error_ts"], "last_error": e["last_error"],
            "status": status,
            "probe_ok": pr["ok"] if pr else None,
            "probe_latency_ms": pr["latency_ms"] if pr else None,
            "probe_error": pr["error"] if pr else None,
        })
    return out
