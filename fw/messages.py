"""messages.py — 把偵測結果組成 Telegram 訊息（繁體中文，HTML 格式）。

只陳述數字與時間對得上的事件，不寫任何買賣建議。所有外來文字都先跳脫再放進訊息。
"""
from __future__ import annotations

import datetime as dt
import html
from typing import Dict, List, Optional

from . import timeutil as tu
from .attribute import zh_title
from .model import STEP_BP, summarize

DISCLAIMER = "依公開期貨價格推算，非 CME 官方數值；僅供參考，不構成投資建議。"


def esc(s: object) -> str:
    return html.escape(str(s), quote=False)


def pct(p: float) -> str:
    return "%.1f%%" % (p * 100.0)


def pp(d: float) -> str:
    return "%s%.1f" % ("+" if d >= 0 else "−", abs(d) * 100.0)


def bucket_range(b: object) -> str:
    lo = int(b)
    return "%.2f–%.2f%%" % (lo / 100.0, (lo + STEP_BP) / 100.0)


def move_label(b: object, base_lo: int, cumulative: bool) -> str:
    k = (int(b) - int(base_lo)) // STEP_BP
    if k == 0:
        return "與目前相同" if cumulative else "維持不變"
    if k > 0:
        return ("累計升 %d 碼" if cumulative else "升息 %d 碼") % k
    return ("累計降 %d 碼" if cumulative else "降息 %d 碼") % (-k)


def days_to(day: dt.date, now: dt.datetime, offset: float) -> int:
    return (day - tu.to_local(now, offset).date()).days


def event_line(e: dict, offset: float, with_date: bool = False) -> str:
    when = tu.fmt_local(tu.parse_iso(e["t"]), offset, with_date=with_date)
    extra = []
    if e.get("forecast"):
        extra.append("預期 %s" % e["forecast"])
    if e.get("previous"):
        extra.append("前值 %s" % e["previous"])
    tail = "（%s）" % "、".join(extra) if extra else ""
    name = zh_title(e["title"])
    prefix = "" if name.startswith(("聯準會", "美國", "FOMC")) else "美國 "
    return "%s %s%s%s" % (when, prefix, name, tail)


def _base_line(base_lo: int, inferred: bool) -> str:
    s = "目前目標區間 %s" % bucket_range(base_lo)
    return s + "（依期貨價格推定，待官方資料確認）" if inferred else s


def change_rows(cur: Dict[str, float], ref: Dict[str, float], limit: int = 4, min_pp: float = 1.0) -> List[str]:
    """變動夠大的區間，依目前機率由高到低。"""
    keys = [b for b in set(cur) | set(ref) if abs(cur.get(b, 0.0) - ref.get(b, 0.0)) * 100.0 >= min_pp]
    keys.sort(key=lambda b: (cur.get(b, 0.0), -int(b)), reverse=True)
    return keys[:limit]


def compose_change(alert: dict, snap: dict, causes: Dict[str, list], cfg: dict,
                   inferred: bool = False) -> str:
    offset = cfg.get("local_utc_offset_hours", 8)
    now = tu.parse_iso(alert["t"])
    base_lo = int(snap["b"])
    head = {"event": "📊 數據公布後，利率預期變動",
            "threshold": "📊 利率預期變動",
            "flip": "🔄 最可能的結果換了"}.get(alert["kind"], "📊 利率預期變動")
    lines = ["<b>%s</b>（台北 %s）" % (head, tu.fmt_local(now, offset)), _base_line(base_lo, inferred)]
    dates = sorted(snap["m"])
    for d in alert.get("watch") or []:
        cur = snap["m"].get(d) or {}
        ref = (alert.get("ref") or {}).get(d) or {}
        rows = change_rows(cur, ref)
        if not rows:
            continue
        day = tu.parse_date(d)
        cumulative = dates.index(d) > 0
        lines.append("")
        lines.append("<b>▍%s 會議</b>（%d 天後）" % (tu.fmt_md(day), days_to(day, now, offset)))
        for b in rows:
            c, r = cur.get(b, 0.0), ref.get(b, 0.0)
            lines.append("%s %s：%s → %s（%s）" % (move_label(b, base_lo, cumulative), bucket_range(b),
                                                 pct(r), pct(c), pp(c - r)))
    lines.append("")
    lines.append("<b>可能原因</b>")
    any_cause = False
    for e in causes.get("events") or []:
        lines.append("・" + esc(event_line(e, offset)))
        any_cause = True
    for f in causes.get("fed") or []:
        lines.append("・%s 聯準會發布：<a href=\"%s\">%s</a>" % (
            tu.fmt_local(tu.parse_iso(f["t"]), offset, with_date=False),
            html.escape(f.get("link") or "", quote=True), esc(f["title"])))
        any_cause = True
    if not any_cause:
        earlier = causes.get("earlier") or []
        if earlier:
            lines.append("・這段時間沒有新的排定數據或談話；稍早有：")
            for e in earlier:
                lines.append("　" + esc(event_line(e, offset)))
        else:
            lines.append("・這段時間沒有排定的重要數據或官員談話")
    news = causes.get("news") or []
    if news:
        lines.append("<b>相關新聞</b>")
        for n in news:
            src = "（%s）" % esc(n["source"]) if n.get("source") else ""
            lines.append("・<a href=\"%s\">%s</a>%s" % (html.escape(n.get("link") or "", quote=True),
                                                      esc(n["title"]), src))
    lines.append("")
    lines.append("比較基準：%s 的數值" % tu.fmt_local(tu.parse_iso(alert["ref_t"]), offset))
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def compose_decision_note(decided: dict) -> str:
    """會議剛開完時附在訊息裡的一行。"""
    k = int(decided.get("steps", 0))
    what = "維持利率不變" if k == 0 else ("升息 %d 碼" % k if k > 0 else "降息 %d 碼" % -k)
    return "%s FOMC 決議：依期貨價格推定為「%s」，目標區間 %s（待官方資料確認）" % (
        tu.fmt_md(tu.parse_date(decided["date"])), what, bucket_range(decided["base_lo"]))


def compose_summary(day: str, cur: Dict[str, Dict[str, float]], prev: Optional[Dict[str, Dict[str, float]]],
                    prev_day: Optional[str], base_lo: int, inferred: bool, changes: List[dict],
                    upcoming: List[dict], now: dt.datetime, cfg: dict, provisional: bool = False) -> str:
    """每日總結。cur／prev：會議日 → 分布（定盤值）。"""
    offset = cfg.get("local_utc_offset_hours", 8)
    d = tu.parse_date(day)
    lines = ["<b>🗓 每日總結｜%s%s</b>" % (tu.fmt_md(d, with_weekday=True), "初值" if provisional else "定盤值"),
             _base_line(base_lo, inferred)]
    dates = sorted(cur)
    for i, md in enumerate(dates[:3]):
        dist = cur[md]
        s = summarize({int(k): v for k, v in dist.items()}, base_lo)
        top = str(s["top"])
        p_now = dist.get(top, 0.0)
        mday = tu.parse_date(md)
        lines.append("")
        lines.append("<b>▍%s 會議</b>（%d 天後）" % (tu.fmt_md(mday), days_to(mday, now, offset)))
        tail = ""
        if prev and md in prev:
            p_prev = prev[md].get(top, 0.0)
            tail = "（前一交易日 %s，%s）" % (pct(p_prev), pp(p_now - p_prev))
        lines.append("最可能：%s %s　%s%s" % (move_label(top, base_lo, i > 0), bucket_range(top), pct(p_now), tail))
        others = sorted((b for b in dist if b != top and dist[b] >= 0.05), key=lambda b: dist[b], reverse=True)[:2]
        for b in others:
            t2 = ""
            if prev and md in prev:
                t2 = "（%s）" % pp(dist[b] - prev[md].get(b, 0.0))
            lines.append("其次：%s %s　%s%s" % (move_label(b, base_lo, i > 0), bucket_range(b), pct(dist[b]), t2))
    if dates:
        lines.append("")
        lines.append("<b>利率路徑</b>（機率加權，目標區間中點）")
        parts = []
        for md in dates[:4]:
            s = summarize({int(k): v for k, v in cur[md].items()}, base_lo)
            parts.append("%s 後 %.2f%%" % (tu.fmt_md(tu.parse_date(md)), s["exp_mid"]))
        lines.append("｜".join(parts))
        last = dates[-1]
        s_last = summarize({int(k): v for k, v in cur[last].items()}, base_lo)
        lines.append("到 %s 累計約 %s%.1f 碼" % (tu.parse_date(last).strftime("%Y/%m/%d"),
                                              "+" if s_last["exp_steps"] >= 0 else "−", abs(s_last["exp_steps"])))
    lines.append("")
    lines.append("<b>過去一天的變動</b>")
    if changes:
        for c in changes[-4:]:
            lines.append("・" + esc(change_brief(c, offset)))
    else:
        lines.append("・沒有達到通知門檻的變動")
    if upcoming:
        lines.append("<b>接下來要公布（台北時間）</b>")
        for e in upcoming:
            lines.append("・" + esc(event_line(e, offset, with_date=True)))
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def change_brief(c: dict, offset: float) -> str:
    """變動紀錄的一行摘要（每日總結與儀表板共用）。"""
    when = tu.fmt_local(tu.parse_iso(c["t"]), offset)
    names = [zh_title(e["title"]) for e in (c.get("causes") or {}).get("events") or []][:2]
    why = "、".join(names) + " 之後" if names else "無對應的排定事件"
    top = ""
    rows = c.get("rows") or []
    if rows and rows[0].get("items"):
        it = max(rows[0]["items"], key=lambda x: abs(x["to"] - x["from"]))
        top = "，%s 會議 %s %s → %s" % (tu.fmt_md(tu.parse_date(rows[0]["date"])), bucket_range(it["b"]),
                                       pct(it["from"]), pct(it["to"]))
    return "%s %s%s" % (when, why, top)


def compose_failure(errors: List[str], n: int, cfg: dict) -> str:
    detail = "；".join(errors[:3]) if errors else "原因不明"
    return ("<b>⚠️ 利率機率追蹤：資料來源異常</b>\n已連續 %d 次抓不到期貨報價（%s）。\n"
            "程式會繼續重試，恢復時再通知。" % (n, esc(detail)))


def compose_recovered() -> str:
    return "<b>✅ 利率機率追蹤：資料來源已恢復</b>\n報價已能正常取得。"


def compose_hello(snap: Optional[dict], cfg: dict) -> str:
    lines = ["<b>✅ 利率機率追蹤已連上 Telegram</b>", "之後的變動通知與每日總結會傳到這裡。"]
    if snap and snap.get("m"):
        d = sorted(snap["m"])[0]
        dist = snap["m"][d]
        top = max(dist, key=lambda b: dist[b])
        lines.append("目前 %s 會議最可能的結果：%s %s　%s" % (
            tu.fmt_md(tu.parse_date(d)), move_label(top, int(snap["b"]), False), bucket_range(top), pct(dist[top])))
    lines.append(DISCLAIMER)
    return "\n".join(lines)
