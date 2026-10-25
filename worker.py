"""Subscription Usage worker for Agent of Empires.

JSON-RPC 2.0 over newline-delimited JSON on stdio. The host sends requests and
notifications on stdin; this worker sends host RPCs (config.get, sessions.list,
ui.state.set/remove) on stdout and reads their responses from stdin.

Sources:
- OpenUsage local API (plan, progress windows, today/30-day cost, trend).
- CodexBar CLI `codexbar usage --json --provider <p>` (per-account windows and
  pace). Refreshed on its own slower timer in a background thread.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from i18n import set_language, t as tr, weekday

RPC_TIMEOUT = 10.0
HTTP_TIMEOUT = 5.0
CODEXBAR_TIMEOUT = 60
TOOL_PROVIDER = {"claude": "claude", "codex": "codex", "grok": "grok", "gemini": "antigravity",
                 "antigravity": "antigravity", "agy": "antigravity", "opencode": "opencode"}
DISPLAY = {"codex": "Codex", "claude": "Claude", "grok": "Grok", "antigravity": "Antigravity",
           "opencode": "OpenCode", "cursor": "Cursor", "copilot": "Copilot"}
STATUS_IDS = {"codex": "a-codex", "claude": "b-claude", "grok": "c-grok", "antigravity": "d-antigravity",
              "opencode": "e-opencode"}
NO_DATA_ID = "z-none"
OU_LABEL = {"Session": "5시간 세션", "Weekly": "주간", "Weekly limit": "주간", "Daily": "일간", "Monthly": "월간"}

_ids = itertools.count(1)
_pending: dict[int, dict[str, Any]] = {}
_pending_lock = threading.Lock()
_out_lock = threading.Lock()
_wake = threading.Event()
_cb_lock = threading.Lock()
_cb_cache: dict[str, Any] = {"data": {}, "at": None, "errors": {}}


# ---------------------------------------------------------------- transport
def log(msg: str) -> None:
    sys.stderr.write(f"[usage] {msg}\n")
    sys.stderr.flush()


def send(obj: dict[str, Any]) -> None:
    with _out_lock:
        sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def call(method: str, params: dict[str, Any]) -> Any:
    msg_id = next(_ids)
    slot: dict[str, Any] = {"event": threading.Event()}
    with _pending_lock:
        _pending[msg_id] = slot
    send({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params})
    if not slot["event"].wait(RPC_TIMEOUT):
        with _pending_lock:
            _pending.pop(msg_id, None)
        raise TimeoutError(f"{method} timed out")
    if "error" in slot:
        raise RuntimeError(f"{method}: {slot['error']}")
    return slot.get("result")


def reader() -> None:
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        if "method" not in msg:
            with _pending_lock:
                slot = _pending.pop(msg.get("id"), None)
            if slot is not None:
                slot["error" if "error" in msg else "result"] = msg.get("error", msg.get("result"))
                slot["event"].set()
            continue
        tail = str(msg.get("method", "")).rsplit(".", 1)[-1]
        if tail == "refresh":
            _cb_cache["at"] = None  # force CodexBar too
        if tail in {"refresh", "changed"}:
            _wake.set()
        if msg.get("id") is not None:
            if tail in {"refresh", "status"}:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {"ok": True}})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32601, "message": f"unknown method {msg.get('method')}"}})
    os._exit(0)


def setting(key: str, default: Any) -> Any:
    try:
        value = (call("config.get", {"key": key}) or {}).get("value")
    except Exception as exc:  # noqa: BLE001
        log(f"config.get {key}: {exc}")
        return default
    return default if value is None else value


# ---------------------------------------------------------------- formatting
def parse_time(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except ValueError:
        return None


def human_span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return tr("{p0}일 {p1}시간", p0=days, p1=hours) if hours else tr("{p0}일", p0=days)
    if hours:
        return tr("{p0}시간 {p1}분", p0=hours, p1=minutes) if minutes else tr("{p0}시간", p0=hours)
    return tr("{p0}분", p0=minutes)


def reset_text(iso: str | None) -> str:
    dt = parse_time(iso)
    if not dt:
        return ""
    left = (dt - datetime.now(timezone.utc).astimezone()).total_seconds()
    when = f"{dt.month}/{dt.day}({weekday(dt.weekday())}) {dt:%H:%M}"
    return tr("{p0} 초기화 · {p1} 남음", p0=when, p1=human_span(left)) if left > 0 else tr("{p0} 초기화됨", p0=when)


def window_label(minutes: int | None, fallback: str = "") -> str:
    if minutes == 300:  # noqa: PLR2004
        return tr("5시간 세션")
    if minutes == 10080:  # noqa: PLR2004
        return tr("주간")
    if minutes == 1440:  # noqa: PLR2004
        return tr("일간")
    if minutes and minutes % 1440 == 0:
        return tr("{p0}일", p0=minutes // 1440)
    if minutes and minutes % 60 == 0:
        return tr("{p0}시간", p0=minutes // 60)
    return fallback or (tr("{p0}분", p0=minutes) if minutes else tr("한도"))


def mask(email: str | None, on: bool) -> str:
    if not email:
        return ""
    if not on or "@" not in email:
        return email
    local, domain = email.split("@", 1)
    return f"{local[:3]}…@{domain}"


# ---------------------------------------------------------------- sources
def fetch_openusage(url: str) -> tuple[list[dict[str, Any]], str | None]:
    if not url:
        return [], None
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
        return (data if isinstance(data, list) else []), None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return [], str(getattr(exc, "reason", None) or exc)


def codexbar_bin() -> str | None:
    path = os.environ.get("PATH", "") + ":/opt/homebrew/bin:/usr/local/bin"
    return shutil.which("codexbar", path=path)


def run_codexbar(provider: str) -> tuple[list[dict[str, Any]], str | None]:
    exe = codexbar_bin()
    if not exe:
        return [], "codexbar 없음"
    args = [exe, "usage", "--json", "--provider", provider]
    if provider == "codex":
        args.append("--all-accounts")
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=CODEXBAR_TIMEOUT, check=False)  # noqa: S603
        data = json.loads(out.stdout or "[]")
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        return [], str(exc)
    rows = [d for d in data if isinstance(d, dict)]
    err = next((d["error"].get("message") for d in rows if isinstance(d.get("error"), dict)), None)
    return [d for d in rows if d.get("usage")], err


def codexbar_loop(get_providers: Any, get_interval: Any) -> None:
    while True:
        providers = get_providers()
        data, errors = {}, {}
        for p in providers:
            rows, err = run_codexbar(p)
            if rows:
                data[p] = rows
            if err:
                errors[p] = err
        with _cb_lock:
            _cb_cache.update({"data": data, "errors": errors, "at": datetime.now(timezone.utc)})
        _wake.set()
        interval = get_interval()
        for _ in range(interval):
            if _cb_cache["at"] is None:  # refresh requested
                break
            threading.Event().wait(1)


# ---------------------------------------------------------------- model
def ou_text(provider: dict[str, Any], label: str) -> str | None:
    return next((ln.get("value") for ln in provider.get("lines", []) if ln.get("label") == label), None)


def build_model(ou: list[dict[str, Any]], cb: dict[str, list[dict[str, Any]]], mask_on: bool) -> list[dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for p in ou:
        pid = p.get("providerId", "")
        windows = []
        for ln in p.get("lines", []):
            if ln.get("type") == "progress" and ln.get("limit"):
                windows.append({"label": tr(OU_LABEL.get(ln.get("label", ""), ln.get("label", ""))),
                                "pct": round(100 * float(ln.get("used", 0)) / float(ln["limit"])),
                                "resets_at": ln.get("resetsAt")})
        trend = next((ln.get("points") for ln in p.get("lines", []) if ln.get("points")), None)
        by_id[pid] = {"id": pid, "name": p.get("displayName") or DISPLAY.get(pid, pid), "plan": p.get("plan"),
                      "today": ou_text(p, "Today"), "month": ou_text(p, "Last 30 Days"), "trend": trend,
                      "accounts": [{"label": "", "windows": windows, "pace": None}]}
    for pid, rows in cb.items():
        entry = by_id.setdefault(pid, {"id": pid, "name": DISPLAY.get(pid, pid), "plan": None, "today": None,
                                       "month": None, "trend": None, "accounts": []})
        accounts = []
        for r in rows:
            u = r.get("usage") or {}
            windows = []
            for key in ("primary", "secondary", "tertiary"):
                w = u.get(key)
                if w and w.get("usedPercent") is not None:
                    windows.append({"key": key, "label": window_label(w.get("windowMinutes"), w.get("label", "")),
                                    "pct": round(float(w["usedPercent"])), "resets_at": w.get("resetsAt")})
            for extra in u.get("extraRateWindows") or []:
                w = extra.get("window") or {}
                if w.get("usedPercent") is not None:
                    title = str(extra.get("title", "")).replace(" only", tr(" 전용"))
                    windows.append({"key": extra.get("id"), "pct": round(float(w["usedPercent"])),
                                    "label": f"{window_label(w.get('windowMinutes'))} · {title}" if title else window_label(w.get("windowMinutes")),
                                    "resets_at": w.get("resetsAt")})
            ou_windows = [w for a in entry["accounts"] for w in a["windows"]]
            for i, w in enumerate(windows):  # CodexBar may omit windowMinutes; borrow OpenUsage's label
                if w["label"] == tr("한도") and i < len(ou_windows):
                    w["label"] = ou_windows[i]["label"]
            pace = None
            for key, pv in (r.get("pace") or {}).items():
                if isinstance(pv, dict):
                    pace = {"window": key, "will_last": pv.get("willLastToReset"), "eta": pv.get("etaSeconds"),
                            "expected": pv.get("expectedUsedPercent")}
            email = r.get("account") or u.get("accountEmail")
            accounts.append({"label": mask(email, mask_on), "windows": windows, "pace": pace})
        if accounts:
            entry["accounts"] = accounts
    model = [m for m in by_id.values() if any(a["windows"] for a in m["accounts"])]
    for m in model:
        m["top"] = max((w["pct"] for a in m["accounts"] for w in a["windows"]), default=0)
    return sorted(model, key=lambda m: -m["top"])


def tone(pct: int, warn: int, danger: int) -> str:
    if pct >= danger:
        return "danger"
    return "warn" if pct >= warn else "success"


# ---------------------------------------------------------------- UI
def split_cost(text: str | None) -> tuple[str, str]:
    if not text:
        return "", ""
    parts = [x.strip() for x in text.replace("tokens", tr("토큰")).split("·")]
    return parts[0], (parts[1] if len(parts) > 1 else "")


def provider_section(m: dict[str, Any], warn: int, danger: int) -> dict[str, Any]:
    children: list[dict[str, Any]] = []
    multi = len(m["accounts"]) > 1
    for acc in m["accounts"]:
        if multi or acc["label"]:
            children.append({"kind": "row", "icon": "user", "label": acc["label"] or tr("계정"), "mono": True,
                             "value": f"{max((w['pct'] for w in acc['windows']), default=0)}%",
                             "value_tone": tone(max((w["pct"] for w in acc["windows"]), default=0), warn, danger)})
        for w in acc["windows"]:
            t = tone(w["pct"], warn, danger)
            children.append({"kind": "row", "label": w["label"], "value": tr("{p0}% 사용", p0=w['pct']), "value_tone": t,
                             "sublabel": reset_text(w.get("resets_at"))})
            children.append({"kind": "bar", "segments": [
                {"value": max(w["pct"], 0.5), "tone": t, "label": tr("사용 {p0}%", p0=w['pct'])},
                {"value": max(100 - w["pct"], 0), "tone": "neutral", "label": tr("남음 {p0}%", p0=100 - w['pct'])}]})
        pace = acc.get("pace")
        if pace and pace.get("will_last") is False and pace.get("eta"):
            children.append({"kind": "note", "tone": "warn",
                             "text": tr("지금 속도면 초기화 전에 소진 · 약 {p0} 후", p0=human_span(pace['eta']))})
        elif pace and pace.get("will_last") is True:
            children.append({"kind": "note", "tone": "success", "text": tr("지금 속도면 초기화까지 여유 있음")})
    cost_cards = []
    for label, text in ((tr("오늘"), m.get("today")), (tr("최근 30일"), m.get("month"))):
        value, sub = split_cost(text)
        if value:
            cost_cards.append({"kind": "row", "label": label, "value": value, "sublabel": sub, "mono": True})
    if cost_cards:
        children.append({"kind": "columns", "children": cost_cards})
    trend = m.get("trend")
    if trend:
        values = [float(p.get("value") or 0) for p in trend][-30:]
        if any(values):
            children.append({"kind": "sparkline", "values": values, "tone": "info", "caption": tr("최근 30일 사용 추이")})
    badges = [{"text": m["plan"]}] if m.get("plan") else []
    return {"kind": "section", "title": m["name"], "boxed": True, "value": f"{m['top']}%",
            "value_tone": tone(m["top"], warn, danger), "badges": badges, "collapsible": True, "children": children}


def risk_callout(model: list[dict[str, Any]], danger: int) -> dict[str, Any] | None:
    items = []
    for m in model:
        for acc in m["accounts"]:
            pace = acc.get("pace") or {}
            if pace.get("will_last") is False and pace.get("eta"):
                items.append(tr("{p0}{p1}: 약 {p2} 후 소진 예상", p0=m['name'], p1=' ' + acc['label'] if acc['label'] else '', p2=human_span(pace['eta'])))
            for w in acc["windows"]:
                if w["pct"] >= danger:
                    items.append(f"{m['name']} {w['label']} {w['pct']}%")
    if not items:
        return None
    return {"kind": "callout", "tone": "warn", "icon": "alert-triangle", "title": tr("주의가 필요한 구독이 있습니다"),
            "detail": " · ".join(dict.fromkeys(items))}


def chip_tooltip(m: dict[str, Any]) -> str:
    parts = []
    for acc in m["accounts"]:
        prefix = f"{acc['label']} " if acc["label"] else ""
        for w in acc["windows"]:
            reset = reset_text(w.get("resets_at"))
            parts.append(f"{prefix}{w['label']} {w['pct']}%" + (f" ({reset})" if reset else ""))
        pace = acc.get("pace") or {}
        if pace.get("will_last") is False and pace.get("eta"):
            parts.append(tr("지금 속도면 약 {p0} 후 소진", p0=human_span(pace['eta'])))
    plan = f"{m['name']} · {m['plan']}" if m.get("plan") else m["name"]
    return plan + " — " + " / ".join(parts)


def chip_tone(m: dict[str, Any], warn: int, danger: int) -> str:
    t = tone(m["top"], warn, danger)
    pace_risk = any((a.get("pace") or {}).get("will_last") is False for a in m["accounts"])
    return "warn" if t == "success" and pace_risk else t


def push_status_chips(model: list[dict[str, Any]], warn: int, danger: int) -> None:
    shown = set()
    for m in model:
        chip = STATUS_IDS.get(m["id"])
        if not chip:
            continue
        call("ui.state.set", {"slot": "status-bar", "id": chip, "payload": {
            "text": f"{m['name']} {m['top']}%", "tone": chip_tone(m, warn, danger), "icon": "gauge",
            "tooltip": chip_tooltip(m)}})
        shown.add(chip)
    if not shown:
        call("ui.state.set", {"slot": "status-bar", "id": NO_DATA_ID, "payload": {
            "text": tr("사용량 없음"), "tone": "neutral", "icon": "gauge",
            "tooltip": tr("OpenUsage 앱이나 CodexBar 로그인을 확인하세요")}})
        shown.add(NO_DATA_ID)
    for chip in [*STATUS_IDS.values(), NO_DATA_ID]:
        if chip not in shown:
            try:
                call("ui.state.remove", {"slot": "status-bar", "id": chip})
            except Exception:  # noqa: BLE001, S110
                pass


_badged: set[str] = set()


def push_row_badges(model: list[dict[str, Any]], warn: int, danger: int, enabled: bool) -> None:
    global _badged  # noqa: PLW0603
    top = {m["id"]: m for m in model}
    seen: set[str] = set()
    if enabled:
        try:
            sessions = (call("sessions.list", {"exclude": ["archived", "trashed"]}) or {}).get("sessions", [])
        except Exception as exc:  # noqa: BLE001
            log(f"sessions.list: {exc}")
            sessions = []
        for s in sessions:
            m = top.get(TOOL_PROVIDER.get(str(s.get("tool", "")).lower(), ""))
            if not m:
                continue
            payload = {"text": f"{m['top']}%", "tone": tone(m["top"], warn, danger), "icon": "gauge",
                       "tooltip": tr("{p0} 사용량 {p1}% (가장 높은 한도 기준)", p0=m['name'], p1=m['top'])}
            try:
                call("ui.state.set", {"slot": "row-badge", "id": "usage", "session_id": s["id"], "payload": payload})
                seen.add(s["id"])
            except Exception as exc:  # noqa: BLE001
                log(f"row-badge {s.get('id')}: {exc}")
    for sid in _badged - seen:
        try:
            call("ui.state.remove", {"slot": "row-badge", "id": "usage", "session_id": sid})
        except Exception:  # noqa: BLE001, S110
            pass
    _badged = seen


def refresh() -> int:
    set_language(setting("language", "ko"))
    warn = int(setting("warn_percent", 75))
    danger = int(setting("danger_percent", 90))
    ou, ou_err = fetch_openusage(str(setting("openusage_url", "http://127.0.0.1:6736/v1/usage")))
    with _cb_lock:
        cb = dict(_cb_cache["data"])
        cb_errors = dict(_cb_cache["errors"])
        cb_at = _cb_cache["at"]
    model = build_model(ou, cb, bool(setting("mask_accounts", True)))

    blocks: list[dict[str, Any]] = []
    callout = risk_callout(model, danger)
    if callout:
        blocks.append(callout)
    if model:
        blocks.append({"kind": "columns", "children": [
            {"kind": "row", "label": m["name"], "value": f"{m['top']}%", "value_tone": tone(m["top"], warn, danger),
             "sublabel": m.get("plan") or ""} for m in model]})
    blocks.extend(provider_section(m, warn, danger) for m in model)
    notes = []
    if ou_err:
        notes.append(tr("OpenUsage 연결 실패: {p0}", p0=ou_err))
    notes.extend(f"CodexBar {p}: {tr(e)}" for p, e in cb_errors.items())
    if not model:
        notes.append(tr("표시할 사용량이 없습니다. OpenUsage 앱을 켜거나 CodexBar 로그인을 확인하세요."))
    blocks.extend({"kind": "note", "tone": "warn", "text": n} for n in notes)
    blocks.append({"kind": "action", "label": tr("지금 새로고침"), "method": "refresh", "icon": "refresh-cw"})

    now = datetime.now(timezone.utc).astimezone()
    cb_part = f" · CodexBar {cb_at.astimezone():%H:%M}" if cb_at else ""
    worst = max((m["top"] for m in model), default=0)
    call("ui.state.set", {"slot": "home-pane", "id": "usage", "payload": {
        "title": tr("구독 사용량"), "icon": "gauge", "blocks": blocks,
        "footer": {"text": f"OpenUsage {now:%H:%M:%S}{cb_part}", "value": tr("최고 {p0}%", p0=worst),
                   "tone": tone(worst, warn, danger) if model else "neutral", "icon": "gauge"},
    }})
    push_status_chips(model, warn, danger)
    push_row_badges(model, warn, danger, bool(setting("session_badges", True)))
    return max(10, int(setting("refresh_secs", 30)))


def main() -> None:
    threading.Thread(target=reader, daemon=True).start()
    providers = lambda: [p.strip() for p in str(setting("codexbar_providers", "codex,claude,grok")).split(",") if p.strip()]  # noqa: E731
    interval = lambda: max(60, int(setting("codexbar_refresh_secs", 300)))  # noqa: E731
    threading.Thread(target=codexbar_loop, args=(providers, interval), daemon=True).start()
    while True:
        try:
            delay = refresh()
        except Exception as exc:  # noqa: BLE001
            log(f"refresh failed: {exc}")
            delay = 30
        _wake.wait(delay)
        _wake.clear()


if __name__ == "__main__":
    main()
