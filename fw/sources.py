"""sources.py — 抓公開資料（全部是 GET、用誠實的 User-Agent、不帶 cookie、不偽裝瀏覽器）。

- ZQ 期貨報價：Yahoo chart 端點（非官方介面，可能失效；所以報價來源做成可抽換）
- EFFR 與目標區間：紐約聯準銀行 Markets API（官方）
- 經濟行事曆：Forex Factory 每週匯出檔（有預期值與前值，沒有實際值）
- 聯準會新聞稿／演講：官方 RSS
- 新聞標題：Google News RSS、CNBC RSS（只取標題、來源、連結）
- FOMC 會議日期：聯準會官網行事曆
"""
from __future__ import annotations

import datetime as dt
import email.utils
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional, Tuple

from . import timeutil as tu
from .model import YM, contract_code

DEFAULT_UA = "fed-rate-monitor/0.1"


class SourceError(Exception):
    """抓不到資料。訊息只放主機名稱與原因，不放完整網址。"""


class NotFound(SourceError):
    pass


def http_get(url: str, ua: str = DEFAULT_UA, timeout: int = 12, retries: int = 1) -> bytes:
    host = urllib.parse.urlparse(url).netloc
    last = "unknown"
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": ua, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise NotFound("%s: 404" % host)
            last = "HTTP %d" % exc.code
            if exc.code in (401, 403):
                break
        except Exception as exc:                      # noqa: BLE001  網路層的錯誤種類很多
            last = type(exc).__name__
        if attempt < retries:
            time.sleep(1.5 * (attempt + 1))
    raise SourceError("%s: %s" % (host, last))


# ───────────────────────── ZQ 期貨報價 ─────────────────────────

class YahooQuotes:
    """Yahoo chart 端點。symbol 例：ZQZ26.CBT"""

    name = "yahoo"
    base = "https://query1.finance.yahoo.com/v8/finance/chart/"

    def __init__(self, ua: str = DEFAULT_UA, workers: int = 4):
        self.ua = ua
        self.workers = workers

    def chart(self, ym: YM, interval: str, rng: str) -> dict:
        url = "%s%s.CBT?interval=%s&range=%s" % (self.base, contract_code(ym), interval, rng)
        j = json.loads(http_get(url, self.ua).decode("utf-8"))
        res = (j.get("chart") or {}).get("result")
        if not res:
            raise SourceError("yahoo: empty result")
        r = res[0]
        meta = r.get("meta") or {}
        ts = r.get("timestamp") or []
        quote = ((r.get("indicators") or {}).get("quote") or [{}])[0]
        closes = quote.get("close") or []
        bars = [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None]
        price = meta.get("regularMarketPrice")
        when = meta.get("regularMarketTime")
        if (price is None or when is None) and bars:
            when, price = bars[-1]
        return {"price": price, "time": when, "bars": bars}

    def _many(self, yms: List[YM], interval: str, rng: str) -> Tuple[Dict[YM, dict], List[YM], List[str]]:
        """回傳 (成功的結果, 404 的合約, 其他錯誤訊息)。"""
        ok: Dict[YM, dict] = {}
        missing: List[YM] = []
        errors: List[str] = []

        def one(ym: YM):
            try:
                return ym, self.chart(ym, interval, rng), None
            except NotFound:
                return ym, None, "404"
            except Exception as exc:                  # noqa: BLE001
                return ym, None, "%s %s" % (contract_code(ym), exc)

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            for ym, data, err in pool.map(one, yms):
                if data is not None:
                    ok[ym] = data
                elif err == "404":
                    missing.append(ym)
                else:
                    errors.append(err or "?")
        return ok, missing, errors

    def spark(self, yms: List[YM]) -> Dict[YM, dict]:
        """一個請求抓多檔合約的當日 5 分 K 與前一日結算價。回傳裡沒有的合約代表查不到。"""
        by_symbol = {contract_code(ym) + ".CBT": ym for ym in yms}
        url = "https://query1.finance.yahoo.com/v8/finance/spark?symbols=%s&range=1d&interval=5m" % (
            ",".join(by_symbol))
        j = json.loads(http_get(url, self.ua).decode("utf-8"))
        if not isinstance(j, dict):
            raise SourceError("yahoo: unexpected spark payload")
        out: Dict[YM, dict] = {}
        for sym, v in j.items():
            ym = by_symbol.get(sym)
            if ym is None or not isinstance(v, dict):
                continue
            ts = v.get("timestamp") or []
            closes = v.get("close") or []
            bars = [(int(t), float(c)) for t, c in zip(ts, closes) if c is not None]
            prev = v.get("previousClose")
            if prev is None:
                prev = v.get("chartPreviousClose")
            if not bars and prev is None:
                continue
            out[ym] = {"price": bars[-1][1] if bars else None,
                       "time": bars[-1][0] if bars else None,
                       "prev_close": float(prev) if prev is not None else None,
                       "bars": bars}
        return out

    def latest(self, yms: List[YM]) -> Tuple[Dict[YM, dict], List[YM], List[str]]:
        """最新成交價與前一日結算價：{ym: {price, time, prev_close, bars}}。

        先用 spark（每 20 檔一個請求）；失敗才逐檔改用 chart。
        回傳 (成功的結果, 查不到的合約, 錯誤訊息)。"""
        ok: Dict[YM, dict] = {}
        errors: List[str] = []
        try:
            for i in range(0, len(yms), 20):
                ok.update(self.spark(yms[i:i + 20]))
        except SourceError as exc:
            errors.append("spark %s" % exc)
            ok = {}
        if ok:
            return ok, [ym for ym in yms if ym not in ok], errors
        got, missing, errs = self._many(yms, "5m", "1d")
        for ym, d in got.items():
            d["prev_close"] = None
        return got, missing, errors + errs

    def intraday(self, yms: List[YM], rng: str = "5d") -> Tuple[Dict[YM, dict], List[YM], List[str]]:
        return self._many(yms, "5m", rng)

    def daily(self, yms: List[YM], rng: str = "1y") -> Tuple[Dict[YM, dict], List[YM], List[str]]:
        return self._many(yms, "1d", rng)


def quote_source(name: str, ua: str):
    """報價來源工廠。之後要改接付費官方 API 或券商報價，就在這裡加一個類別。"""
    if name == "yahoo":
        return YahooQuotes(ua)
    raise ValueError("unknown quote source: %s" % name)


# ───────────────────────── 紐約聯準銀行：EFFR 與目標區間 ─────────────────────────

NYFED = "https://markets.newyorkfed.org/api/rates/unsecured/effr/"


def _effr_rows(j: dict) -> List[dict]:
    rows = []
    for r in j.get("refRates") or []:
        try:
            rows.append({
                "date": r["effectiveDate"],
                "rate": float(r["percentRate"]),
                "lo": float(r["targetRateFrom"]) if r.get("targetRateFrom") is not None else None,
                "hi": float(r["targetRateTo"]) if r.get("targetRateTo") is not None else None,
            })
        except (KeyError, TypeError, ValueError):
            continue
    rows.sort(key=lambda x: x["date"])
    return rows


def fetch_effr_latest(ua: str = DEFAULT_UA) -> dict:
    rows = _effr_rows(json.loads(http_get(NYFED + "last/1.json", ua).decode("utf-8")))
    if not rows:
        raise SourceError("nyfed: empty")
    return rows[-1]


def fetch_effr_history(start: dt.date, end: dt.date, ua: str = DEFAULT_UA) -> List[dict]:
    url = "%ssearch.json?startDate=%s&endDate=%s" % (NYFED, start.isoformat(), end.isoformat())
    return _effr_rows(json.loads(http_get(url, ua).decode("utf-8")))


# ───────────────────────── 經濟行事曆 ─────────────────────────

FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"


def fetch_calendar(ua: str = DEFAULT_UA) -> List[dict]:
    """本週美國事件：[{id, t(UTC ISO), title, impact(High/Medium/Low), forecast, previous}]"""
    raw = json.loads(http_get(FF_URL, ua).decode("utf-8"))
    out = []
    for e in raw:
        if e.get("country") != "USD":
            continue
        try:
            t = tu.parse_iso(e["date"])
        except (KeyError, ValueError):
            continue
        title = (e.get("title") or "").strip()
        impact = (e.get("impact") or "").strip()      # High／Medium／Low／Holiday
        if not title:
            continue
        out.append({
            "id": "ff|%s|%s" % (tu.iso(t), title),
            "t": tu.iso(t),
            "title": title,
            "impact": impact,
            "forecast": (e.get("forecast") or "").strip(),
            "previous": (e.get("previous") or "").strip(),
        })
    out.sort(key=lambda x: x["t"])
    return out


# ───────────────────────── RSS ─────────────────────────

def _text(node: Optional[ET.Element]) -> str:
    return (node.text or "").strip() if node is not None else ""


def parse_rss(xml_bytes: bytes, default_source: str = "") -> List[dict]:
    """[{title, link, t(UTC ISO), source}]；解析失敗回空清單。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return []
    items = []
    for it in root.iter("item"):
        title = _text(it.find("title"))
        link = _text(it.find("link"))
        pub = _text(it.find("pubDate"))
        if not title or not pub:
            continue
        try:
            when = email.utils.parsedate_to_datetime(pub)
            if when.tzinfo is None:
                when = when.replace(tzinfo=tu.UTC)
        except (TypeError, ValueError):
            continue
        source = _text(it.find("source")) or default_source
        if source and title.endswith(" - " + source):      # Google News 會把來源接在標題後面
            title = title[: -len(" - " + source)].strip()
        items.append({"title": title, "link": link, "t": tu.iso(when), "source": source})
    return items


FED_FEEDS = {
    "聯準會貨幣政策新聞稿": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "聯準會官員演講": "https://www.federalreserve.gov/feeds/speeches.xml",
}


def fetch_fed_rss(ua: str = DEFAULT_UA) -> List[dict]:
    out: List[dict] = []
    for name, url in FED_FEEDS.items():
        try:
            out.extend(parse_rss(http_get(url, ua), name))
        except SourceError:
            continue
    out.sort(key=lambda x: x["t"])
    return out


def fetch_news(query: str, lang: str = "zh", ua: str = DEFAULT_UA) -> List[dict]:
    """Google News RSS 搜尋最近一天的新聞標題。"""
    loc = "hl=zh-TW&gl=TW&ceid=TW:zh-Hant" if lang == "zh" else "hl=en-US&gl=US&ceid=US:en"
    url = "https://news.google.com/rss/search?q=%s&%s" % (urllib.parse.quote(query + " when:1d"), loc)
    return parse_rss(http_get(url, ua), "Google News")


def fetch_cnbc(ua: str = DEFAULT_UA) -> List[dict]:
    return parse_rss(http_get("https://www.cnbc.com/id/20910258/device/rss/rss.html", ua), "CNBC")


# ───────────────────────── FOMC 會議日期 ─────────────────────────

FOMC_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august",
     "september", "october", "november", "december"], 1)}
_MONTHS.update({m[:3]: i for m, i in list(_MONTHS.items())})


def parse_fomc_calendar(html: str) -> List[str]:
    """從聯準會官網行事曆讀出每場會議的決議日（兩天會議取第二天）。"""
    out: List[str] = []
    panels = list(re.finditer(r"(\d{4}) FOMC Meetings", html))
    for i, m in enumerate(panels):
        year = int(m.group(1))
        block = html[m.end(): panels[i + 1].start() if i + 1 < len(panels) else len(html)]
        months = re.findall(r'fomc-meeting__month[^>]*>\s*(?:<strong>)?\s*([A-Za-z/]+)', block)
        dates = re.findall(r'fomc-meeting__date[^>]*>\s*([^<]+?)\s*<', block)
        for mon, days in zip(months, dates):
            if "unscheduled" in days.lower() or "notation" in days.lower():
                continue
            nums = [int(x) for x in re.findall(r"\d+", days)]
            if not nums:
                continue
            parts = [p.strip().lower() for p in mon.split("/")]
            month = _MONTHS.get(parts[0]) or _MONTHS.get(parts[0][:3])
            if not month:
                continue
            day = nums[-1]
            y = year
            if len(nums) >= 2 and nums[-1] < nums[0]:       # 跨月（例如 4/30–5/1）
                month2 = _MONTHS.get(parts[-1]) or _MONTHS.get(parts[-1][:3]) if len(parts) > 1 else None
                if month2:
                    month = month2
                else:
                    month += 1
                    if month == 13:
                        month, y = 1, year + 1
            try:
                out.append(dt.date(y, month, day).isoformat())
            except ValueError:
                continue
    return sorted(set(out))


def fetch_fomc_dates(ua: str = DEFAULT_UA) -> List[str]:
    return parse_fomc_calendar(http_get(FOMC_URL, ua, timeout=30).decode("utf-8", "replace"))
