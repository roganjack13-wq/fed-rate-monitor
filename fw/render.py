"""render.py — 把資料整理成儀表板要的格式，套進範本，產出單一自足的 HTML（資料內嵌，直接點開就能看）。

三種版本：
  local     本機版，含私人內容（Claude 排程補的實際公布值與變動摘要），每 10 分鐘自動重新整理
  artifact  給 claude.ai 私人網頁的內容（只有 body，不含 html／head 外殼），含私人內容
  public    公開版：只有推算的機率、事件名稱與時間、新聞標題連結；不讀任何私人檔案
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
from typing import Dict, List, Optional

from . import attribute, model
from . import timeutil as tu
from .store import write_text_atomic

OFFICIAL_URL = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

WRAPPER = """<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>%(title)s</title>
%(extra)s<style>
:root{color-scheme:light;padding:env(safe-area-inset-top,0px) 0 env(safe-area-inset-bottom,0px)}
body{margin:0}img{max-width:100%%}[hidden]{display:none!important}
</style>
</head>
<body>
%(body)s
</body>
</html>
"""


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _event_view(e: dict, show_numbers: bool) -> dict:
    out = {"t": e.get("t"), "title": e.get("title"), "title_zh": attribute.zh_title(e.get("title") or ""),
           "impact": e.get("impact")}
    if show_numbers:
        if e.get("forecast"):
            out["forecast"] = e["forecast"]
        if e.get("previous"):
            out["previous"] = e["previous"]
        if e.get("actual") is not None:
            out["actual"] = e["actual"]
    return out


def build_payload(ctx, state: dict, daily: dict, rows: List[dict], changes: List[dict], cache: dict,
                  meetings: Dict[model.YM, dt.date], variant: str) -> dict:
    cfg = ctx.cfg
    now = ctx.now
    public = variant == "public"
    show_numbers = (not public) or bool((cfg.get("public") or {}).get("show_forecast"))
    ups = [d.isoformat() for d in model.upcoming(meetings, now)]
    upset = set(ups)
    effr = (cache.get("effr") or {}).get("row") or {}
    last = rows[-1] if rows else None
    base_lo = int(state.get("base_lo") or (last or {}).get("b") or round(float(effr.get("lo") or 0) * 100))

    def keep(m: dict) -> dict:
        return {d: v for d, v in (m or {}).items() if d in upset}

    now_obj: dict = {"m": {}, "x": {}}
    if last:
        t_last = tu.parse_iso(last["t"])
        thin_sec = float(cfg["poll"].get("thin_hours", 6)) * 3600.0
        q = last.get("q") or {}
        thin = [d for d in keep(last["m"]) if tu.cme_open(t_last) and q.get(d)
                and t_last.timestamp() - float(q[d]) > thin_sec]
        now_obj = {"t": last["t"], "m": keep(last["m"]), "x": keep(last.get("x") or {}),
                   "s": [d for d in (last.get("s") or []) if d in upset], "thin": thin}

    days = daily.get("days") or {}
    settle = [{"day": k, "b": days[k].get("b"), "m": keep(days[k].get("m") or {})} for k in sorted(days)[-70:]]

    path_days = sorted((daily.get("path") or {}))[-260:]
    series: Dict[str, List[Optional[float]]] = {d: [] for d in ups}
    for k in path_days:
        entry = daily["path"][k]
        for d in ups:
            series[d].append(entry.get(d))
    path = {"days": path_days, "series": {d: v for d, v in series.items() if any(x is not None for x in v)}}

    intraday: dict = {"t": [], "m": {}}
    if last:
        cut = tu.iso(tu.parse_iso(last["t"]) - dt.timedelta(days=5))
        recent = [r for r in rows if r["t"] >= cut]
        if len(recent) > 900:
            recent = recent[::2] + ([recent[-1]] if (len(recent) - 1) % 2 else [])
        intraday["t"] = [int(tu.parse_iso(r["t"]).timestamp()) for r in recent]
        for d in ups[:4]:
            buckets: Dict[str, float] = {}
            for r in recent:
                for b, p in (r["m"].get(d) or {}).items():
                    buckets[b] = max(buckets.get(b, 0.0), p)
            cols = {}
            for b in sorted((b for b, mx in buckets.items() if mx >= 0.02), key=int):
                cols[b] = [(r["m"].get(d) or {}).get(b, 0.0) if d in r["m"] else None for r in recent]
            if cols:
                intraday["m"][d] = cols

    private = None
    actual_by_key: Dict[str, object] = {}
    if not public:
        enrich = _read_json(os.path.join(ctx.private_dir, "enrich.json")) or {}
        commentary = _read_json(os.path.join(ctx.private_dir, "commentary.json"))
        actuals = []
        for a in enrich.get("actuals") or []:
            if not isinstance(a, dict):
                continue
            item = {"t": a.get("t"), "title": a.get("title"),
                    "title_zh": a.get("title_zh") or attribute.zh_title(a.get("title") or ""),
                    "actual": a.get("actual"), "forecast": a.get("forecast"), "previous": a.get("previous")}
            actuals.append(item)
            if a.get("t") and a.get("title") and a.get("actual") is not None:
                actual_by_key["%s|%s" % (str(a["t"])[:16], a["title"])] = a["actual"]
        if actuals or (commentary and commentary.get("text")):
            private = {"commentary": commentary if commentary and commentary.get("text") else None,
                       "actuals": actuals[-12:]}

    def with_actual(e: dict) -> dict:
        key = "%s|%s" % (str(e.get("t"))[:16], e.get("title"))
        if key in actual_by_key:
            e = dict(e, actual=actual_by_key[key])
        return e

    out_changes = []
    for c in changes[-40:]:
        cz = c.get("causes") or {}
        out_changes.append({
            "t": c["t"], "kind": c.get("kind"), "moved_pp": c.get("moved_pp"), "ref_t": c.get("ref_t"),
            "rows": c.get("rows") or [],
            "causes": {"events": [_event_view(with_actual(e), show_numbers) for e in cz.get("events") or []],
                       "fed": cz.get("fed") or [], "news": cz.get("news") or []}})

    cal = list((cache.get("calendar") or {}).get("items", {}).values())
    lo, hi = tu.iso(now - dt.timedelta(hours=1)), tu.iso(now + dt.timedelta(days=8))
    upcoming = [e for e in cal if lo <= e["t"] <= hi
                and (attribute.IMPACT_RANK.get(e.get("impact"), 0) >= 2 or attribute.speaker_of(e.get("title") or ""))]
    upcoming.sort(key=lambda e: e["t"])

    return {
        "v": 1, "variant": variant, "generated": tu.iso(now), "tz": cfg.get("local_utc_offset_hours", 8),
        "reload_min": None if variant == "artifact" else 10,
        "base": {"lo": base_lo, "inferred": bool(state.get("decided")), "effr": effr.get("rate"),
                 "effr_date": effr.get("date")},
        "decided": state.get("decided"),
        "status": {"errors": state.get("errors") or [], "warnings": state.get("warnings") or [],
                   "last_ok": state.get("last_ok")},
        "now": now_obj, "settle": settle, "path": path, "intraday": intraday,
        "changes": out_changes,
        "upcoming": [_event_view(e, show_numbers) for e in upcoming[:30]],
        "private": private,
        "links": {"official": OFFICIAL_URL},
    }


def _fill(template: str, payload: dict) -> str:
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    return template.replace("__DATA__", data)


def _standalone(body: str, public: bool) -> str:
    m = re.match(r"\s*<title>(.*?)</title>\s*", body, re.S)
    title = m.group(1) if m else "聯準會利率機率追蹤"
    if m:
        body = body[m.end():]
    extra = '<meta name="robots" content="noindex">\n' if public else ""
    return WRAPPER % {"title": title, "extra": extra, "body": body}


def render_all(ctx, state: dict, daily: dict, rows: List[dict], changes: List[dict], cache: dict,
               meetings: Dict[model.YM, dt.date]) -> List[str]:
    """依 ctx.variant 產出檔案，回傳寫出的路徑。"""
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "dashboard", "template.html"), "r", encoding="utf-8") as f:
        template = f.read()
    os.makedirs(ctx.out_dir, exist_ok=True)
    written: List[str] = []
    variants = {"public": ["public"], "artifact": ["artifact"]}.get(ctx.variant, ["local", "artifact"])
    for variant in variants:
        payload = build_payload(ctx, state, daily, rows, changes, cache, meetings, variant)
        body = _fill(template, payload)
        if variant == "artifact":
            path = os.path.join(ctx.out_dir, "_artifact_body.html")
            write_text_atomic(path, body)
        else:
            path = os.path.join(ctx.out_dir, "index.html")
            write_text_atomic(path, _standalone(body, variant == "public"))
            if variant == "public":
                write_text_atomic(os.path.join(ctx.out_dir, "data.json"),
                                  json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        written.append(path)
    return written
