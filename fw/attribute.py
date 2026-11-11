"""attribute.py — 找出一段時間內可能造成利率預期變動的數據／事件，以及相關的新聞標題。

只做「時間對得上」的客觀比對：行事曆上的數據與官員談話、聯準會官方發布、新聞標題（標題＋來源＋連結）。
找不到就照實回報沒有，不硬湊原因。
"""
from __future__ import annotations

import datetime as dt
import re
from typing import Dict, List, Optional

from . import timeutil as tu

IMPACT_RANK = {"High": 3, "Medium": 2, "Low": 1}

# 常見事件的中文名稱（Forex Factory 的英文標題 → 中文）
ZH_TITLES = {
    "CPI m/m": "CPI 月增率",
    "CPI y/y": "CPI 年增率",
    "Core CPI m/m": "核心 CPI 月增率",
    "Core CPI y/y": "核心 CPI 年增率",
    "PPI m/m": "PPI 月增率",
    "Core PPI m/m": "核心 PPI 月增率",
    "Core PCE Price Index m/m": "核心 PCE 物價指數月增率",
    "Core PCE Price Index y/y": "核心 PCE 物價指數年增率",
    "PCE Price Index m/m": "PCE 物價指數月增率",
    "Non-Farm Employment Change": "非農就業人數",
    "ADP Non-Farm Employment Change": "ADP 民間就業人數",
    "ADP Weekly Employment Change": "ADP 每週就業變動",
    "Bank Holiday": "銀行假日（休市）",
    "Import Prices m/m": "進口物價月增率",
    "Industrial Production m/m": "工業生產月增率",
    "Building Permits": "營建許可",
    "Housing Starts": "新屋開工",
    "Pending Home Sales m/m": "成屋待完成銷售月增率",
    "Trade Balance": "貿易收支",
    "Federal Budget Balance": "聯邦預算收支",
    "Consumer Credit m/m": "消費信貸月增額",
    "NFIB Small Business Index": "NFIB 中小企業樂觀指數",
    "Crude Oil Inventories": "原油庫存",
    "Natural Gas Storage": "天然氣庫存",
    "Unemployment Rate": "失業率",
    "Average Hourly Earnings m/m": "平均時薪月增率",
    "Unemployment Claims": "初領失業救濟金人數",
    "JOLTS Job Openings": "JOLTS 職缺數",
    "Employment Cost Index q/q": "僱傭成本指數季增率",
    "Retail Sales m/m": "零售銷售月增率",
    "Core Retail Sales m/m": "核心零售銷售月增率",
    "Advance GDP q/q": "GDP 季增年率（初值）",
    "Prelim GDP q/q": "GDP 季增年率（修正值）",
    "Final GDP q/q": "GDP 季增年率（終值）",
    "ISM Manufacturing PMI": "ISM 製造業 PMI",
    "ISM Services PMI": "ISM 服務業 PMI",
    "Flash Manufacturing PMI": "製造業 PMI 初值",
    "Flash Services PMI": "服務業 PMI 初值",
    "CB Consumer Confidence": "經濟諮商會消費者信心",
    "Prelim UoM Consumer Sentiment": "密大消費者信心初值",
    "Revised UoM Consumer Sentiment": "密大消費者信心終值",
    "Prelim UoM Inflation Expectations": "密大通膨預期初值",
    "Federal Funds Rate": "聯邦基金利率決議",
    "FOMC Statement": "FOMC 會後聲明",
    "FOMC Press Conference": "FOMC 會後記者會",
    "FOMC Meeting Minutes": "FOMC 會議紀要",
    "FOMC Economic Projections": "FOMC 經濟預測（點陣圖）",
    "Empire State Manufacturing Index": "紐約州製造業指數",
    "Philly Fed Manufacturing Index": "費城聯準銀行製造業指數",
    "Durable Goods Orders m/m": "耐久財訂單月增率",
    "Core Durable Goods Orders m/m": "核心耐久財訂單月增率",
    "Existing Home Sales": "成屋銷售",
    "New Home Sales": "新屋銷售",
    "Beige Book": "聯準會褐皮書",
    "Treasury Currency Report": "財政部匯率報告",
}

_PATTERNS = [
    (re.compile(r"^FOMC Member (.+) Speaks$"), "聯準會官員 %s 談話"),
    (re.compile(r"^Fed Chair (.+) Speaks$"), "聯準會主席 %s 談話"),
    (re.compile(r"^Fed Chair (.+) Testifies$"), "聯準會主席 %s 國會作證"),
    (re.compile(r"^President (.+) Speaks$"), "美國總統 %s 談話"),
    (re.compile(r"^Treasury Sec (.+) Speaks$"), "美國財政部長 %s 談話"),
    (re.compile(r"^(\d+)-y Bond Auction$"), "%s 年期公債標售"),
]

_FED_WORDS = ("聯準會", "Fed", "FOMC", "升息", "降息", "利率", "通膨", "CPI", "PCE", "PPI", "非農", "就業",
              "失業", "鮑爾", "Powell", "殖利率", "美債", "公債", "inflation", "rate", "jobs", "payroll",
              "Treasury", "yield")

_EVENT_TOKENS = [
    (re.compile(r"CPI", re.I), ("CPI", "消費者物價", "通膨", "inflation")),
    (re.compile(r"PCE", re.I), ("PCE", "通膨", "inflation")),
    (re.compile(r"PPI", re.I), ("PPI", "生產者物價", "通膨")),
    (re.compile(r"Non-Farm|Unemployment|Hourly Earnings|JOLTS|Claims", re.I),
     ("非農", "就業", "失業", "payroll", "jobs", "labor")),
    (re.compile(r"FOMC|Federal Funds|Fed Chair", re.I), ("FOMC", "聯準會", "Fed", "利率決議", "鮑爾", "Powell")),
    (re.compile(r"GDP", re.I), ("GDP", "經濟成長")),
    (re.compile(r"Retail Sales", re.I), ("零售", "retail")),
    (re.compile(r"ISM|PMI", re.I), ("ISM", "PMI", "製造業", "服務業")),
]


def zh_title(title: str) -> str:
    if title in ZH_TITLES:
        return ZH_TITLES[title]
    for rx, tmpl in _PATTERNS:
        m = rx.match(title)
        if m:
            return tmpl % m.group(1)
    return title


def norm_title(s: str) -> str:
    return re.sub(r"[\W_]+", "", s.lower())


def _in(t_iso: str, start: dt.datetime, end: dt.datetime) -> bool:
    try:
        t = tu.parse_iso(t_iso)
    except ValueError:
        return False
    return start <= t <= end


def find_causes(calendar: List[dict], fed_items: List[dict], headlines: List[dict],
                t0: dt.datetime, t1: dt.datetime, alert_events: Optional[List[dict]] = None,
                max_events: int = 4, max_news: int = 2) -> Dict[str, list]:
    """回傳 {"events": [...], "fed": [...], "news": [...]}，每一項都帶時間。

    calendar   行事曆事件（sources.fetch_calendar 的格式）
    fed_items  聯準會官方 RSS
    headlines  新聞標題
    t0、t1     要對照的時間範圍（變動開始 → 現在）
    alert_events  觸發這次通知的事件（一定排在最前面）
    """
    first = list(alert_events or [])
    first_ids = {e["id"] for e in first}
    fed = [f for f in fed_items if _in(f["t"], t0 - dt.timedelta(minutes=10), t1)]
    fed.sort(key=lambda f: f["t"], reverse=True)
    fed = fed[:2]
    fed_names = {f["title"].split(",")[0].strip().lower() for f in fed}

    window = [e for e in calendar
              if e["id"] not in first_ids and IMPACT_RANK.get(e.get("impact"), 0) >= 1
              and _in(e["t"], t0 - dt.timedelta(minutes=10), t1)
              and (speaker_of(e["title"]) or "").lower() not in fed_names]   # 同一場談話已有官方連結就不重複列
    window.sort(key=lambda e: (IMPACT_RANK.get(e.get("impact"), 0), e["t"]), reverse=True)
    events = _unique_titles(first + window)[:max_events]

    earlier: List[dict] = []
    if not events and not fed:                    # 這段時間沒有對到：往前再看三小時，標成「稍早」
        back0, back1 = t0 - dt.timedelta(minutes=190), t0 - dt.timedelta(minutes=10)
        cands = [e for e in calendar
                 if _in(e["t"], back0, back1)
                 and (IMPACT_RANK.get(e.get("impact"), 0) >= 2 or speaker_of(e["title"]))]
        cands.sort(key=lambda e: (IMPACT_RANK.get(e.get("impact"), 0), e["t"]), reverse=True)
        earlier = _unique_titles(cands)[:2]

    tokens: List[str] = []
    for e in events:
        for rx, words in _EVENT_TOKENS:
            if rx.search(e["title"]):
                tokens.extend(words)
    scored = []
    seen = set()
    for h in headlines:
        if not _in(h["t"], t0 - dt.timedelta(minutes=20), t1 + dt.timedelta(minutes=5)):
            continue
        title = h["title"]
        if not any(w.lower() in title.lower() for w in _FED_WORDS):
            continue
        key = norm_title(title)
        if key in seen:
            continue
        seen.add(key)
        score = sum(2 for w in set(tokens) if w.lower() in title.lower())
        score += sum(1 for w in ("聯準會", "Fed", "FOMC", "升息", "降息") if w.lower() in title.lower())
        if h.get("lang") == "zh":                  # 同分時優先放中文標題
            score += 1
        scored.append((score, h["t"], h))
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    news = [h for _, _, h in scored[:max_news]]
    return {"events": events, "fed": fed, "news": news, "earlier": earlier}


_SPEAKER_RX = re.compile(r"^(?:FOMC Member|Fed Chair) (\S+)")


def speaker_of(title: str) -> Optional[str]:
    """「FOMC Member Waller Speaks」→ 'Waller'；不是官員談話就回 None。"""
    m = _SPEAKER_RX.match(title)
    return m.group(1) if m else None


def _unique_titles(events: List[dict]) -> List[dict]:
    seen = set()
    out = []
    for e in events:
        if e["title"] in seen:
            continue
        seen.add(e["title"])
        out.append(e)
    return out


def upcoming_events(calendar: List[dict], now: dt.datetime, hours: float = 24.0,
                    min_rank: int = 2, limit: int = 6) -> List[dict]:
    """接下來 hours 小時內的事件（預設只取中、高影響）。"""
    end = now + dt.timedelta(hours=hours)
    out = [e for e in calendar
           if IMPACT_RANK.get(e.get("impact"), 0) >= min_rank and _in(e["t"], now, end)]
    out.sort(key=lambda e: e["t"])
    return out[:limit]
