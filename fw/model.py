"""model.py — 由 30 天期聯邦基金利率期貨（ZQ）價格，推算各次 FOMC 會議後的目標利率機率。

算法（與 CME 公開的 FedWatch 方法論相同，已用官網整張表格逐格驗證）：

- 合約隱含利率 R = 100 − 價格（代表該月 EFFR 的日平均）。
- 會議月有 M 天、決議日是 N 號：當月平均 = (N/M)·會議前利率 + ((M−N)/M)·會議後利率。
- 會議前利率 pre(m)：前一個月沒有會議 → 用前一個月的 R；
                     否則用當月合約往回解 [R_m − ((M−N)/M)·post(m)] / (N/M)。
- 會議後利率 post(m)：下一個月沒有會議 → 用下一個月的 R；否則等於 pre(下一個月)。
- 每場會議的預期變動 Δ = post − pre，x = Δ / 25bp，k = floor(x)，f = x − k：
  變動 k 碼的機率是 1−f，變動 k+1 碼的機率是 f。
- 跨會議用機率樹累加，從目前目標區間起算每個區間的機率。
"""
from __future__ import annotations

import calendar
import datetime as dt
import math
from typing import Dict, List, Optional, Tuple

from . import timeutil as tu

YM = Tuple[int, int]
MONTH_CODES = "FGHJKMNQUVXZ"
STEP_BP = 25
MAX_ABS_DELTA_BP = 200.0      # 單場會議預期變動超過這個值視為資料異常


class MissingPrice(Exception):
    def __init__(self, ym: YM):
        self.ym = ym
        super().__init__("%d-%02d" % ym)


def ym_next(ym: YM) -> YM:
    return (ym[0] + 1, 1) if ym[1] == 12 else (ym[0], ym[1] + 1)


def ym_prev(ym: YM) -> YM:
    return (ym[0] - 1, 12) if ym[1] == 1 else (ym[0], ym[1] - 1)


def ym_add(ym: YM, n: int) -> YM:
    idx = ym[0] * 12 + (ym[1] - 1) + n
    return (idx // 12, idx % 12 + 1)


def ym_key(ym: YM) -> str:
    return "%d-%02d" % ym


def parse_ym(s: str) -> YM:
    y, m = s.split("-")
    return (int(y), int(m))


def contract_code(ym: YM) -> str:
    """(2026, 12) → 'ZQZ26'"""
    return "ZQ%s%02d" % (MONTH_CODES[ym[1] - 1], ym[0] % 100)


def meeting_index(meeting_dates: List[str]) -> Dict[YM, dt.date]:
    """{(年, 月): 決議日}。過去與未來的會議都要放進來（判斷「前一個月有沒有會議」要用）。"""
    out: Dict[YM, dt.date] = {}
    for s in meeting_dates:
        d = tu.parse_date(s)
        out[(d.year, d.month)] = d
    return out


def upcoming(meetings: Dict[YM, dt.date], asof: dt.datetime) -> List[dt.date]:
    """決議時間還沒到的會議，依先後排序。"""
    return sorted(d for d in meetings.values() if tu.decision_utc(d) > asof)


def last_past(meetings: Dict[YM, dt.date], asof: dt.datetime) -> Optional[dt.date]:
    past = sorted(d for d in meetings.values() if tu.decision_utc(d) <= asof)
    return past[-1] if past else None


def required_contracts(meetings: Dict[YM, dt.date], asof: dt.datetime,
                       extra: int = 2, cap_months: int = 20) -> List[YM]:
    """要抓的合約月份：從現在這個月（美中時間）到最後一場會議之後 extra 個月。"""
    ct = tu.to_ct(asof)
    start: YM = (ct.year, ct.month)
    ups = upcoming(meetings, asof)
    if not ups:
        return [start]
    end = ym_add((ups[-1].year, ups[-1].month), extra)
    out: List[YM] = []
    cur = start
    while cur <= end and len(out) < cap_months:
        out.append(cur)
        cur = ym_next(cur)
    return out


def contracts_for_nearest(meetings: Dict[YM, dt.date], asof: dt.datetime, n: int = 2) -> List[YM]:
    """最近 n 場會議算機率時會用到的合約（比整條曲線少，可以抓得比較勤）。"""
    ups = upcoming(meetings, asof)[:n]
    if not ups:
        return []
    need = set()
    for d in ups:
        ym = (d.year, d.month)
        need.update((ym_prev(ym), ym, ym_next(ym), ym_add(ym, 2)))
    ct = tu.to_ct(asof)
    start: YM = (ct.year, ct.month)
    return sorted(x for x in need if x >= start)


def compute(prices: Dict[YM, float], meetings: Dict[YM, dt.date], asof: dt.datetime,
            base_lo_bp: int, settled: Optional[Dict[YM, float]] = None,
            effr: Optional[float] = None) -> List[dict]:
    """算出每一場未來會議的機率分布。

    prices   {(年, 月): 合約價格}，只放目前還在交易的合約
    meetings {(年, 月): 決議日}，過去與未來都要有
    asof     計算的時間點（UTC）
    base_lo_bp 目前目標區間下限（bp，例如 375）
    settled  {(年, 月): 利率}，已到期月份的 EFFR 月平均（等同該合約最後結算值）
    effr     最新的 EFFR；只有在 settled 也沒有時才拿來頂替（結果會標 approx）

    回傳 list（依會議先後），缺價或資料異常時在那一場停止。
    每筆：date、pre、post、delta_bp、dist {區間下限 bp: 機率}、used（用到的合約）、approx
    """
    settled = settled or {}
    ct = tu.to_ct(asof)
    cur_month: YM = (ct.year, ct.month)
    used: List[YM] = []
    flags = {"approx": False}

    def rate(ym: YM) -> float:
        p = prices.get(ym)
        if p is not None:
            used.append(ym)
            return 100.0 - float(p)
        if ym in settled:
            return float(settled[ym])
        raise MissingPrice(ym)

    def post(ym: YM, depth: int = 0) -> float:
        n = ym_next(ym)
        if n not in meetings:
            return rate(n)
        return pre(n, depth + 1)

    def pre(ym: YM, depth: int = 0) -> float:
        if depth > 8:
            raise MissingPrice(ym)
        p = ym_prev(ym)
        if p not in meetings:
            try:
                return rate(p)
            except MissingPrice:
                if effr is not None and p < cur_month:      # 已到期又沒有月平均：用最新 EFFR 頂替
                    flags["approx"] = True
                    return float(effr)
                raise
        days = calendar.monthrange(ym[0], ym[1])[1]
        n_old = meetings[ym].day
        return (rate(ym) - ((days - n_old) / days) * post(ym, depth + 1)) / (n_old / days)

    out: List[dict] = []
    tree: Dict[int, float] = {0: 1.0}
    for d in upcoming(meetings, asof):
        ym = (d.year, d.month)
        del used[:]
        flags["approx"] = False
        try:
            a = pre(ym)
            b = post(ym)
        except MissingPrice:
            break
        delta_bp = (b - a) * 100.0
        if not math.isfinite(delta_bp) or abs(delta_bp) > MAX_ABS_DELTA_BP:
            break
        # 交叉檢查：下一個月也有會議時，會議後利率可以改用「當月合約＋會議前利率」正解一次。
        # 兩種算法差太多，代表其中一檔合約是過期或離譜的成交價。
        xc: Optional[float] = None
        days = calendar.monthrange(ym[0], ym[1])[1]
        n_old = d.day
        if ym_next(ym) in meetings and (days - n_old) / days >= 0.3 and prices.get(ym) is not None:
            alt = (rate(ym) - (n_old / days) * a) / ((days - n_old) / days)
            xc = round(abs(b - alt) * 100.0, 2)
        x = delta_bp / STEP_BP
        k = math.floor(x)
        f = x - k
        nxt: Dict[int, float] = {}
        for steps, p in tree.items():
            if f < 1.0:
                nxt[steps + k] = nxt.get(steps + k, 0.0) + p * (1.0 - f)
            if f > 0.0:
                nxt[steps + k + 1] = nxt.get(steps + k + 1, 0.0) + p * f
        tree = nxt
        dist = {base_lo_bp + STEP_BP * s: p for s, p in sorted(tree.items()) if p >= 5e-5}
        out.append({
            "date": d.isoformat(),
            "pre": round(a, 5),
            "post": round(b, 5),
            "delta_bp": round(delta_bp, 3),
            "dist": dist,
            "used": sorted(set(used)),
            "approx": flags["approx"],
            "xc": xc,
        })
    return out


def post_rates(prices: Dict[YM, float], meetings: Dict[YM, dt.date], asof: dt.datetime) -> Dict[str, float]:
    """各場會議後的預期利率（只用到會議當月之後的合約，所以能回補較久以前的歷史）。"""
    def rate(ym: YM) -> float:
        p = prices.get(ym)
        if p is None:
            raise MissingPrice(ym)
        return 100.0 - float(p)

    def post(ym: YM, depth: int = 0) -> float:
        if depth > 8:
            raise MissingPrice(ym)
        n = ym_next(ym)
        if n not in meetings:
            return rate(n)
        days = calendar.monthrange(n[0], n[1])[1]
        n_old = meetings[n].day
        return (rate(n) - ((days - n_old) / days) * post(n, depth + 1)) / (n_old / days)

    out: Dict[str, float] = {}
    for d in upcoming(meetings, asof):
        try:
            v = post((d.year, d.month))
        except MissingPrice:
            continue
        if math.isfinite(v) and 0.0 <= v <= 25.0:
            out[d.isoformat()] = round(v, 4)
    return out


def summarize(dist: Dict[int, float], base_lo_bp: int) -> dict:
    """把分布整理成：高於／等於／低於目前區間的機率、最可能的區間、機率加權的利率（區間中點）。"""
    total = sum(dist.values()) or 1.0
    up = sum(p for b, p in dist.items() if b > base_lo_bp) / total
    hold = dist.get(base_lo_bp, 0.0) / total
    down = sum(p for b, p in dist.items() if b < base_lo_bp) / total
    top = max(dist.items(), key=lambda kv: (kv[1], -kv[0]))[0] if dist else base_lo_bp
    exp_lo = sum(b * p for b, p in dist.items()) / total if dist else float(base_lo_bp)
    return {"up": up, "hold": hold, "down": down, "top": top,
            "exp_mid": (exp_lo + STEP_BP / 2.0) / 100.0,
            "exp_steps": (exp_lo - base_lo_bp) / STEP_BP}


def monthly_average(effr_by_day: Dict[dt.date, float], ym: YM) -> Optional[float]:
    """某個月 EFFR 的日平均（週末假日沿用前一個營業日），等同該月合約的最後結算利率。"""
    days = calendar.monthrange(ym[0], ym[1])[1]
    first = dt.date(ym[0], ym[1], 1)
    last = dt.date(ym[0], ym[1], days)
    known = sorted(d for d in effr_by_day if d <= last)
    if not known or known[0] > first:
        return None
    total = 0.0
    idx = 0
    current: Optional[float] = None
    day = first
    while idx < len(known) and known[idx] <= first:
        current = effr_by_day[known[idx]]
        idx += 1
    while day <= last:
        while idx < len(known) and known[idx] <= day:
            current = effr_by_day[known[idx]]
            idx += 1
        if current is None:
            return None
        total += current
        day += dt.timedelta(days=1)
    return round(total / days, 5)


def infer_decision_steps(pre_next: float, effr_before: float) -> int:
    """會議剛開完、官方目標區間還沒更新時：用下一場會議的「會議前利率」推定這次升降了幾碼。"""
    return int(round((pre_next - effr_before) * 100.0 / STEP_BP))
