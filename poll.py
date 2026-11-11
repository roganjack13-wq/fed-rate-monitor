#!/usr/bin/env python3
"""poll.py — 一次輪詢：抓報價 → 算機率 → 存檔 → 偵測變化 → 找對應事件 → 發通知 → 重產儀表板。

排程每 10 分鐘執行一次（Windows 工作排程或 GitHub Actions）。只用 Python 標準函式庫。

  python -X utf8 poll.py                 正常輪詢
  python -X utf8 poll.py --dry-run       只印結果與「原本會發的訊息」，不寫檔、不發訊息
  python -X utf8 poll.py --backfill      重新回補歷史資料（第一次執行會自動做）
  python -X utf8 poll.py --replay 2026-11-10   用歷史 5 分 K 重播某一天，印出當時會發的通知
  python -X utf8 poll.py --render-only   只重產儀表板
  python -X utf8 poll.py --summary       立刻產生每日總結（搭配 --dry-run 可預覽）
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import random
import sys
import time
import traceback
from typing import Dict, List, Optional, Tuple

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fw import __version__, attribute, detect, messages, model, notify, sources  # noqa: E402
from fw import store as storemod  # noqa: E402
from fw import timeutil as tu  # noqa: E402

MIN15 = dt.timedelta(minutes=15)


def say(msg: str = "") -> None:
    """印到主控台。用 pythonw 執行時沒有 stdout，直接略過。"""
    if sys.stdout is None:
        return
    try:
        print(msg)
    except Exception:                                  # noqa: BLE001
        pass


def deep_merge(base: dict, extra: dict) -> dict:
    out = dict(base)
    for k, v in extra.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(root: str) -> dict:
    with open(os.path.join(root, "config.json"), "r", encoding="utf-8") as f:
        cfg = json.load(f)
    local = os.path.join(root, "config.local.json")
    if os.path.exists(local):
        with open(local, "r", encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    return cfg


class Ctx:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.cfg = load_config(ROOT)
        self.now = tu.now_utc()
        self.data_dir = os.path.abspath(args.data_dir or os.path.join(ROOT, "data"))
        self.out_dir = os.path.abspath(args.out_dir or os.path.join(ROOT, "out"))
        self.private_dir = os.path.join(ROOT, "private")
        self.dry = bool(args.dry_run)
        self.store = storemod.Store(self.data_dir)
        self.ua = self.cfg.get("user_agent") or sources.DEFAULT_UA
        self.source = sources.quote_source(self.cfg.get("quote_source", "yahoo"), self.ua)
        self.offset = self.cfg.get("local_utc_offset_hours", 8)
        self.notify_on = not (args.dry_run or args.no_notify)
        self.variant = args.variant
        self.warnings: List[str] = []
        self.would_send: List[str] = []

    def log(self, msg: str) -> None:
        say(msg)
        if not self.dry:
            self.store.log(msg, self.offset)


# ───────────────────────── 參考資料（EFFR、行事曆、RSS、會議日期） ─────────────────────────

def age_min(entry: Optional[dict], now: dt.datetime) -> float:
    if not entry or not entry.get("t"):
        return 1e9
    return (now - tu.parse_iso(entry["t"])).total_seconds() / 60.0


def refresh_refdata(ctx: Ctx, cache: dict) -> None:
    now = ctx.now
    e = cache.get("effr") or {}
    last_meet = model.last_past(model.meeting_index(ctx.cfg.get("meetings") or []), now)
    just_met = bool(last_meet and (now - tu.decision_utc(last_meet)) < dt.timedelta(days=4))
    if age_min(e, now) > (20 if just_met else 120):
        try:
            cache["effr"] = {"t": tu.iso(now), "row": sources.fetch_effr_latest(ctx.ua)}
        except sources.SourceError as exc:
            ctx.warnings.append("紐約聯準銀行利率抓不到（%s）" % exc)

    cal = cache.setdefault("calendar", {"t": None, "items": {}})
    if age_min(cal, now) > 180:
        try:
            fresh = sources.fetch_calendar(ctx.ua)
            if fresh:                                  # 這一週的資料整批換新（事件改時間時不會留下舊的那筆）
                lo, hi = fresh[0]["t"], fresh[-1]["t"]
                cal["items"] = {k: v for k, v in cal["items"].items() if not (lo <= v["t"] <= hi)}
                for ev in fresh:
                    cal["items"][ev["id"]] = ev
            cal["t"] = tu.iso(now)
        except sources.SourceError as exc:
            ctx.warnings.append("經濟行事曆抓不到（%s）" % exc)
        cutoff = tu.iso(now - dt.timedelta(days=21))
        cal["items"] = {k: v for k, v in cal["items"].items() if v["t"] >= cutoff}

    fed = cache.setdefault("fed", {"t": None, "items": []})
    if age_min(fed, now) > 30:
        try:
            items = sources.fetch_fed_rss(ctx.ua)
            cutoff = tu.iso(now - dt.timedelta(days=14))
            fed["items"] = [i for i in items if i["t"] >= cutoff]
            fed["t"] = tu.iso(now)
        except Exception:                              # noqa: BLE001  非關鍵來源
            pass

    fomc = cache.setdefault("fomc", {"t": None, "dates": []})
    if age_min(fomc, now) > 7 * 24 * 60:
        try:
            dates = sources.fetch_fomc_dates(ctx.ua)
            if dates:
                fomc["dates"] = dates
            fomc["t"] = tu.iso(now)
        except Exception:                              # noqa: BLE001
            fomc["t"] = tu.iso(now - dt.timedelta(days=6))   # 明天再試


def build_meetings(ctx: Ctx, cache: dict) -> Dict[model.YM, dt.date]:
    """設定檔裡的會議日期，加上聯準會官網新公布、設定檔還沒有的未來日期。"""
    cfg_dates = set(ctx.cfg.get("meetings") or [])
    fetched = set((cache.get("fomc") or {}).get("dates") or [])
    today = tu.to_et(ctx.now).date().isoformat()
    last_cfg = max(cfg_dates) if cfg_dates else ""
    dates = set(cfg_dates)
    dates.update(d for d in fetched if d > last_cfg)
    if fetched:
        years = {d[:4] for d in fetched}
        only_site = sorted(d for d in fetched if d >= today and d <= last_cfg and d not in cfg_dates)
        only_cfg = sorted(d for d in cfg_dates if d >= today and d[:4] in years and d not in fetched)
        if only_site or only_cfg:
            ctx.warnings.append("會議日期與聯準會官網不一致，請確認 config.json（官網多：%s；設定多：%s）" % (
                "、".join(only_site) or "無", "、".join(only_cfg) or "無"))
    meetings = model.meeting_index(sorted(dates))
    if len(model.upcoming(meetings, ctx.now)) < 3:
        ctx.warnings.append("未來的會議日期剩不到 3 場，請更新 config.json 的 meetings")
    return meetings


def settled_rates(ctx: Ctx, cache: dict, meetings: Dict[model.YM, dt.date]) -> Dict[model.YM, float]:
    """已到期月份的利率（EFFR 月平均＝該月合約的最後結算值）。只有「這個月有會議、上個月沒有」時需要。"""
    out = {model.parse_ym(k): float(v) for k, v in (cache.get("settled") or {}).items()}
    ct = tu.to_ct(ctx.now)
    cur = (ct.year, ct.month)
    prev = model.ym_prev(cur)
    if cur in meetings and prev not in meetings and prev not in out:
        try:
            first = dt.date(prev[0], prev[1], 1)
            last = dt.date(cur[0], cur[1], 1) - dt.timedelta(days=1)
            rows = sources.fetch_effr_history(first - dt.timedelta(days=10), last, ctx.ua)
            avg = model.monthly_average({tu.parse_date(r["date"]): r["rate"] for r in rows}, prev)
            if avg is not None:
                out[prev] = avg
                if ct.day >= 3:                        # 月底最後一個營業日的數字隔天才公布，過幾天再存
                    cache.setdefault("settled", {})[model.ym_key(prev)] = avg
        except sources.SourceError as exc:
            ctx.warnings.append("上個月的利率平均抓不到，暫用最新 EFFR（%s）" % exc)
    return out


# ───────────────────────── 報價與快照 ─────────────────────────

def fetch_quotes(ctx: Ctx, cache: dict, meetings: Dict[model.YM, dt.date]) -> Tuple[Dict[str, dict], List[str]]:
    """{ "2026-12": {"p": 最新成交價, "t": 成交時間, "pc": 前一日結算價} }"""
    now = ctx.now
    miss = cache.setdefault("missing", {})
    cutoff = tu.iso(now - dt.timedelta(hours=24))
    yms = [ym for ym in model.required_contracts(meetings, now) if miss.get(model.ym_key(ym), "") < cutoff]
    got, missing, errors = ctx.source.latest(yms)
    confirmed = bool(errors) and errors[0].startswith("spark")   # 已經逐檔查過：查不到就是真的不存在
    for ym in missing:                                 # 整批查詢裡沒有的合約，逐檔確認是不是真的不存在
        if confirmed:
            miss[model.ym_key(ym)] = tu.iso(now)
            continue
        try:
            d = ctx.source.chart(ym, "5m", "1d")
            d["prev_close"] = None
            got[ym] = d
        except sources.NotFound:
            miss[model.ym_key(ym)] = tu.iso(now)
        except Exception as exc:                       # noqa: BLE001
            errors.append("%s %s" % (model.contract_code(ym), exc))
    quotes: Dict[str, dict] = {}
    for ym, d in got.items():
        price = d.get("price")
        pc = d.get("prev_close")
        if price is None and pc is None:
            continue
        quotes[model.ym_key(ym)] = {"p": price if price is not None else pc,
                                    "t": int(d.get("time") or 0), "pc": pc}
    return quotes, errors


def dist_map(res: List[dict]) -> Dict[str, Dict[str, float]]:
    """會議日 → {區間下限: 機率}。不到 0.05% 的區間不存（畫面只顯示到 0.1%）。"""
    return {r["date"]: {str(b): round(p, 4) for b, p in r["dist"].items() if p >= 0.0005} for r in res}


def build_snapshot(ctx: Ctx, quotes: Dict[str, dict], base_lo: int, inferred: bool,
                   settled: Dict[model.YM, float], effr_rate: Optional[float],
                   asof: dt.datetime, meetings: Dict[model.YM, dt.date]) -> dict:
    prices = {model.parse_ym(k): q["p"] for k, q in quotes.items() if q.get("p") is not None}
    res = model.compute(prices, meetings, asof, base_lo, settled, effr_rate)
    xc_max = float(ctx.cfg["alert"].get("cross_check_bp", 2.0))
    thin_sec = float(ctx.cfg["poll"].get("thin_hours", 6)) * 3600.0
    is_open = tu.cme_open(asof)
    snap = {"t": tu.iso(asof), "b": base_lo, "inf": inferred, "m": dist_map(res),
            "x": {}, "s": [], "q": {}, "g": {}, "thin": []}
    for r in res:
        d = r["date"]
        times = sorted(int(quotes[model.ym_key(ym)]["t"]) for ym in r["used"] if model.ym_key(ym) in quotes)
        newest = times[-1] if times else 0
        snap["x"][d] = [round(r["pre"], 4), round(r["post"], 4)]
        snap["q"][d] = newest
        snap["g"][d] = ",".join(str(t) for t in times)
        if r.get("approx") or (r.get("xc") is not None and r["xc"] > xc_max):
            snap["s"].append(d)                        # 資料品質不足：照樣顯示，但不拿來觸發通知
        if is_open and newest and asof.timestamp() - newest > thin_sec:
            snap["thin"].append(d)
    return snap


def row_of(snap: dict, backfilled: bool = False) -> dict:
    row = {"t": snap["t"], "b": snap["b"], "m": snap["m"], "x": snap["x"], "q": snap["q"]}
    if snap.get("s"):
        row["s"] = snap["s"]
    if snap.get("inf"):
        row["inf"] = 1
    if backfilled:
        row["bf"] = 1
    return row


def resolve_base(ctx: Ctx, effr_row: dict, quotes: Dict[str, dict], settled: Dict[model.YM, float],
                 meetings: Dict[model.YM, dt.date], state: dict) -> Tuple[int, bool]:
    """目前目標區間下限（bp）。會議剛開完、官方資料還沒更新時，用期貨價格推定這次升降了幾碼。"""
    base_lo = int(round(float(effr_row["lo"]) * 100))
    last = model.last_past(meetings, ctx.now)
    if last and effr_row["date"] <= last.isoformat():
        prices = {model.parse_ym(k): q["p"] for k, q in quotes.items() if q.get("p") is not None}
        res = model.compute(prices, meetings, ctx.now, base_lo, settled, effr_row["rate"])
        if res:
            steps = model.infer_decision_steps(res[0]["pre"], float(effr_row["rate"]))
            state["decided"] = {"date": last.isoformat(), "steps": steps,
                                "base_lo": base_lo + steps * model.STEP_BP, "t": tu.iso(ctx.now)}
            return base_lo + steps * model.STEP_BP, True
    state.pop("decided", None)
    return base_lo, False


def update_settle(ctx: Ctx, quotes: Dict[str, dict], daily: dict, meetings: Dict[model.YM, dt.date],
                  base_lo: int, settled: Dict[model.YM, float], effr_rate: Optional[float]) -> Optional[str]:
    """用報價源的「前一日結算價」記下最近一個交易日的定盤值。有新增就回傳日期。"""
    day = tu.last_session_day(ctx.now)
    key = day.isoformat()
    days = daily.setdefault("days", {})
    if key in days:
        return None
    px = {k: q["pc"] for k, q in quotes.items() if q.get("pc") is not None}
    if len(px) < 3:
        return None
    known = sorted(days)
    if known:
        last_px = days[known[-1]].get("px") or {}
        common = [k for k in px if k in last_px]
        if common and all(abs(px[k] - last_px[k]) < 1e-9 for k in common):
            return None                                # 還沒換成新的結算價，或美國休市
    asof = tu.settle_utc(day)
    prices = {model.parse_ym(k): v for k, v in px.items()}
    res = model.compute(prices, meetings, asof, base_lo, settled, effr_rate)
    if not res:
        return None
    days[key] = {"b": base_lo, "m": dist_map(res),
                 "x": {r["date"]: [round(r["pre"], 4), round(r["post"], 4)] for r in res},
                 "px": px, "src": "prevclose", "t": tu.iso(ctx.now)}
    daily.setdefault("path", {})[key] = model.post_rates(prices, meetings, asof)
    return key


# ───────────────────────── 回補與重播 ─────────────────────────

def daily_closes(chart: dict) -> Dict[str, float]:
    """日 K → {日期: 收盤價}。最後一筆若不是紐約午夜的時間戳，就是進行中的那一盤，要排除。"""
    out: Dict[str, float] = {}
    bars = chart.get("bars") or []
    for i, (ts, close) in enumerate(bars):
        t = tu.from_epoch(ts)
        if i == len(bars) - 1 and not (t.minute == 0 and t.hour in (4, 5)):
            continue
        out[t.date().isoformat()] = close
    return out


def backfill(ctx: Ctx, cache: dict, daily: dict, meetings: Dict[model.YM, dt.date], base_lo: int,
             settled: Dict[model.YM, float], effr_rate: Optional[float]) -> None:
    now = ctx.now
    miss = cache.get("missing") or {}
    yms = [ym for ym in model.required_contracts(meetings, now) if model.ym_key(ym) not in miss]
    ctx.log("回補歷史資料：日線一年、5 分 K 60 天（%d 檔合約）" % len(yms))

    d_ok, _, d_err = ctx.source.daily(yms, "1y")
    closes = {ym: daily_closes(ch) for ym, ch in d_ok.items()}
    last = model.last_past(meetings, now)
    start = last.isoformat() if last else ""
    all_days = sorted(set().union(*[set(m) for m in closes.values()])) if closes else []
    days = daily.setdefault("days", {})
    path = daily.setdefault("path", {})
    n_days = 0
    for key in all_days:
        asof = tu.settle_utc(tu.parse_date(key))
        prices = {ym: m[key] for ym, m in closes.items() if key in m}
        pr = model.post_rates(prices, meetings, asof)
        if pr and key not in path:
            path[key] = pr
        if start and key >= start and key not in days:
            res = model.compute(prices, meetings, asof, base_lo, settled, effr_rate)
            if res:
                days[key] = {"b": base_lo, "m": dist_map(res),
                             "x": {r["date"]: [round(r["pre"], 4), round(r["post"], 4)] for r in res},
                             "px": {model.ym_key(ym): p for ym, p in prices.items()},
                             "src": "daily", "t": tu.iso(now)}
                n_days += 1

    i_ok, _, i_err = ctx.source.intraday(yms, "60d")
    series = {ym: ch["bars"] for ym, ch in i_ok.items() if ch.get("bars")}
    t_start = now - dt.timedelta(days=60)
    if last:
        t_start = max(t_start, tu.decision_utc(last) + dt.timedelta(minutes=10))
    rows = simulate_rows(ctx, series, t_start, now - dt.timedelta(minutes=10), meetings, base_lo,
                         settled, effr_rate, delay_min=0, thin_after_days=7)
    for r in rows:
        r["bf"] = 1
    merged = {r["t"]: r for r in rows}
    merged.update({r["t"]: r for r in ctx.store.read_lines("intraday.jsonl")})
    if not ctx.dry:
        ctx.store.write_lines("intraday.jsonl", [merged[k] for k in sorted(merged)])
        ctx.store.save("daily.json", daily)
    ctx.log("回補完成：定盤值 %d 天、預期利率 %d 天、盤中 %d 筆%s" % (
        n_days, len(path), len(rows), "（部分合約抓不到：%s）" % "；".join((d_err + i_err)[:3]) if (d_err or i_err) else ""))


def simulate_rows(ctx: Ctx, series: Dict[model.YM, List[Tuple[int, float]]], t_from: dt.datetime,
                  t_to: dt.datetime, meetings: Dict[model.YM, dt.date], base_lo: int,
                  settled: Dict[model.YM, float], effr_rate: Optional[float],
                  delay_min: int = 0, thin_after_days: int = 0) -> List[dict]:
    """用 5 分 K 重建每 10 分鐘的快照。delay_min：模擬報價延遲。thin_after_days：超過幾天前的只取每小時一筆。"""
    idx = {ym: 0 for ym in series}
    cur: Dict[model.YM, Tuple[int, float]] = {}
    rows: List[dict] = []
    t = t_from.replace(second=0, microsecond=0)
    t += dt.timedelta(minutes=(2 - t.minute) % 10)     # 對齊到 :02、:12…（跟排程一樣）
    old_cut = ctx.now - dt.timedelta(days=thin_after_days) if thin_after_days else None
    while t <= t_to:
        step = 60 if (old_cut and t < old_cut) else 10
        if tu.cme_open(t - dt.timedelta(minutes=delay_min)):
            limit = (t - dt.timedelta(minutes=delay_min)).timestamp()
            for ym, bars in series.items():
                i = idx[ym]
                while i < len(bars) and bars[i][0] <= limit:
                    cur[ym] = bars[i]
                    i += 1
                idx[ym] = i
            quotes = {model.ym_key(ym): {"p": c, "t": ts, "pc": None} for ym, (ts, c) in cur.items()}
            snap = build_snapshot(ctx, quotes, base_lo, False, settled, effr_rate, t, meetings)
            if snap["m"]:
                rows.append(snap)
        t += dt.timedelta(minutes=step)
    return [row_of(s) if thin_after_days else s for s in rows]


def replay(ctx: Ctx, day_str: str, cache: dict, meetings: Dict[model.YM, dt.date], base_lo: int,
           settled: Dict[model.YM, float], effr_rate: Optional[float]) -> int:
    """重播某一天：用歷史 5 分 K 模擬當時每 10 分鐘的輪詢，印出會發的通知。不寫檔、不發訊息。"""
    day = tu.parse_date(day_str)
    start = dt.datetime.combine(day, dt.time(0, 0), tzinfo=tu.UTC) - dt.timedelta(hours=ctx.offset)
    end = min(start + dt.timedelta(hours=36), ctx.now)
    miss = cache.get("missing") or {}
    yms = [ym for ym in model.required_contracts(meetings, start) if model.ym_key(ym) not in miss]
    i_ok, _, _ = ctx.source.intraday(yms, "60d")
    series = {ym: ch["bars"] for ym, ch in i_ok.items() if ch.get("bars")}
    snaps = simulate_rows(ctx, series, start, end, meetings, base_lo, settled, effr_rate, delay_min=10)
    events = list((cache.get("calendar") or {}).get("items", {}).values())
    fed_items = (cache.get("fed") or {}).get("items") or []
    headlines = gather_news(ctx)
    state: dict = {}
    seen: List[dict] = []
    n_alert = 0

    def find_ref(t: dt.datetime) -> Optional[dict]:
        key = tu.iso(t)
        prior = [s for s in seen if s["t"] <= key]
        return prior[-1] if prior else None

    say("重播 %s（台北時間 %s 起 36 小時），共 %d 筆模擬輪詢" % (day_str, tu.fmt_local(start, ctx.offset), len(snaps)))
    for snap in snaps:
        t = tu.parse_iso(snap["t"])
        res = detect.evaluate(state, snap, find_ref, events, t, ctx.cfg)
        seen.append(snap)
        alert = res["alert"]
        if not alert:
            continue
        causes = causes_for(alert, events, fed_items, headlines, t)
        text = messages.compose_change(alert, snap, causes, ctx.cfg)
        detect.commit_alert(state, alert, snap, t, ctx.cfg)
        n_alert += 1
        say("\n──── 模擬 %s（%s，變動 %.1f）────" % (tu.fmt_local(t, ctx.offset), alert["kind"], alert["moved_pp"]))
        say(text)
    say("\n重播結束：共 %d 則通知" % n_alert)
    return 0


# ───────────────────────── 歸因、通知、每日總結 ─────────────────────────

def gather_news(ctx: Ctx) -> List[dict]:
    """抓新聞標題（只在要發通知時才抓）。只留白名單來源，並略過帶有操作字眼的標題。"""
    n = ctx.cfg.get("news") or {}
    out: List[dict] = []
    jobs = []
    if n.get("zh"):
        jobs.append(("zh", lambda: sources.fetch_news(n["zh"], "zh", ctx.ua)))
    if n.get("en"):
        jobs.append(("en", lambda: sources.fetch_news(n["en"], "en", ctx.ua)))
    jobs.append(("en", lambda: sources.fetch_cnbc(ctx.ua)))
    for lang, fn in jobs:
        try:
            for h in fn():
                h["lang"] = lang
                out.append(h)
        except Exception:                              # noqa: BLE001  新聞不是關鍵來源
            continue
    allow = [a.lower() for a in (n.get("sources_allow") or [])]
    skip = n.get("skip_words") or []
    keep = []
    for h in out:
        src = (h.get("source") or "").lower()
        if allow and not any(a in src for a in allow):
            continue
        if any(w in h["title"] for w in skip):
            continue
        keep.append(h)
    return keep


def causes_for(alert: dict, events: List[dict], fed_items: List[dict], headlines: List[dict],
               now: dt.datetime) -> Dict[str, list]:
    if alert["kind"] == "event" and alert.get("events"):
        t0 = tu.parse_iso(alert["events"][0]["t"]) - dt.timedelta(minutes=5)
    else:
        since = tu.parse_iso(alert.get("since") or alert["ref_t"])
        t0 = max(tu.parse_iso(alert["ref_t"]), since - dt.timedelta(minutes=120))
    return attribute.find_causes(events, fed_items, headlines, t0, now, alert.get("events"))


def change_record(alert: dict, snap: dict, causes: Dict[str, list], sent) -> dict:
    rows = []
    for d in alert.get("watch") or []:
        cur = snap["m"].get(d) or {}
        ref = (alert.get("ref") or {}).get(d) or {}
        items = [{"b": b, "from": round(ref.get(b, 0.0), 4), "to": round(cur.get(b, 0.0), 4)}
                 for b in sorted(set(cur) | set(ref), key=int)
                 if abs(cur.get(b, 0.0) - ref.get(b, 0.0)) >= 0.005]
        if items:
            rows.append({"date": d, "items": items})
    slim = {"events": [{k: e.get(k) for k in ("t", "title", "impact", "forecast", "previous")}
                       for e in causes.get("events") or []],
            "fed": [{k: f.get(k) for k in ("t", "title", "link")} for f in causes.get("fed") or []],
            "news": [{k: n.get(k) for k in ("t", "title", "link", "source")} for n in causes.get("news") or []]}
    return {"id": "%s|%s" % (alert["t"], alert["kind"]), "t": alert["t"], "kind": alert["kind"],
            "moved_pp": alert.get("moved_pp"), "ref_t": alert["ref_t"], "b": snap["b"],
            "rows": rows, "causes": slim, "sent": sent}


def deliver(ctx: Ctx, text: str) -> Optional[bool]:
    """送出訊息。回傳 True＝送出成功；False＝有設定但失敗；None＝沒設定 Telegram 或這次不發。"""
    if ctx.dry:
        ctx.would_send.append(text)
        return None
    if not ctx.notify_on or not notify.configured():
        return None
    return notify.send(text, silent=detect.in_quiet_hours(ctx.now, ctx.cfg), log=ctx.log)


def handle_alert(ctx: Ctx, state: dict, snap: dict, alert: dict, cache: dict, changes: List[dict]) -> bool:
    events = list((cache.get("calendar") or {}).get("items", {}).values())
    fed_items = (cache.get("fed") or {}).get("items") or []
    causes = causes_for(alert, events, fed_items, gather_news(ctx), ctx.now)
    text = messages.compose_change(alert, snap, causes, ctx.cfg, inferred=bool(snap.get("inf")))
    decided = state.get("decided")
    if decided and (ctx.now - tu.decision_utc(tu.parse_date(decided["date"]))) < dt.timedelta(hours=3):
        text = messages.compose_decision_note(decided) + "\n\n" + text
    sent = deliver(ctx, text)
    if sent is False:
        ctx.log("通知送出失敗，下一輪重試")
        return False
    if ctx.dry:
        return True
    detect.commit_alert(state, alert, snap, ctx.now, ctx.cfg)
    changes.append(change_record(alert, snap, causes, True if sent else "off"))
    del changes[:-300]
    ctx.log("變動通知（%s，%.1f 個百分點）%s" % (alert["kind"], alert.get("moved_pp") or 0.0,
                                         "已送出" if sent else "已記錄（未設定 Telegram）"))
    return True


def provisional_day(rows: List[dict], day: dt.date) -> Optional[dict]:
    """還沒有結算價時，用該交易日收盤前最後一筆盤中快照當「初值」。"""
    close = tu.iso(tu.session_close_utc(day))
    opened = tu.iso(tu.session_close_utc(tu.prev_weekday(day)))
    prior = [r for r in rows if opened < r["t"] <= close]
    return prior[-1] if prior else None


def maybe_summary(ctx: Ctx, state: dict, snap: Optional[dict], daily: dict, rows: List[dict],
                  changes: List[dict], cache: dict, force: bool = False) -> bool:
    today = "forced" if force else detect.summary_due(state, ctx.now, ctx.cfg)
    if not today:
        return False
    days = daily.get("days") or {}
    known = sorted(days)
    target = tu.last_session_day(ctx.now).isoformat()
    provisional = False
    cur_day = prev_day = None
    cur_m = prev_m = None
    if target in days:
        cur_day, cur_m = target, days[target]["m"]
        earlier = [k for k in known if k < target]
        if earlier:
            prev_day, prev_m = earlier[-1], days[earlier[-1]]["m"]
    else:
        prov = provisional_day(rows, tu.parse_date(target))
        if prov:
            cur_day, cur_m, provisional = target, prov["m"], True
            if known:
                prev_day, prev_m = known[-1], days[known[-1]]["m"]
    if cur_m is None:
        if force:
            say("還沒有可以總結的定盤值")
            return False
        loc = tu.to_local(ctx.now, ctx.offset)
        hh, mm = [int(x) for x in str(ctx.cfg["summary"].get("time", "07:30")).split(":")]
        if (loc.hour * 60 + loc.minute) - (hh * 60 + mm) > 180:
            state["summary_date"] = today             # 等了三小時還是沒有新資料（美國休市）：今天不發
        return False
    if not force and state.get("summary_settle") == cur_day and not provisional:
        state["summary_date"] = today                 # 這個交易日已經總結過
        return False
    base_lo = int((snap or {}).get("b") or days.get(cur_day, {}).get("b") or 0) or int(
        round(float(((cache.get("effr") or {}).get("row") or {}).get("lo", 0)) * 100))
    cutoff = tu.iso(ctx.now - dt.timedelta(hours=26))
    recent = [c for c in changes if c["t"] >= cutoff]
    cal_items = list((cache.get("calendar") or {}).get("items", {}).values())
    upcoming = attribute.upcoming_events(cal_items, ctx.now, hours=24.0, min_rank=2, limit=6)
    text = messages.compose_summary(cur_day, cur_m, prev_m, prev_day, base_lo, bool((snap or {}).get("inf")),
                                    recent, upcoming, ctx.now, ctx.cfg, provisional=provisional)
    sent = deliver(ctx, text)
    if sent is False:
        return False
    if ctx.dry or force:
        return True
    state["summary_date"] = today
    state["summary_settle"] = cur_day
    if snap and snap.get("m"):
        detect.set_baseline(state, snap)              # 總結等於告知過使用者：基準重設為當下數值
        state["pending"] = None
    ctx.log("每日總結（%s%s）%s" % (cur_day, "，初值" if provisional else "",
                                "已送出" if sent else "已產生（未設定 Telegram）"))
    return True


# ───────────────────────── 產出儀表板 ─────────────────────────

def do_render(ctx: Ctx, state: dict, daily: dict, rows: List[dict], changes: List[dict], cache: dict,
              meetings: Dict[model.YM, dt.date]) -> None:
    from fw import render                               # 延後載入：沒有範本時引擎照樣能跑
    try:
        render.render_all(ctx, state, daily, rows, changes, cache, meetings)
    except Exception as exc:                            # noqa: BLE001  儀表板失敗不影響通知
        ctx.log("儀表板產生失敗：%s" % type(exc).__name__)
        write_error(ctx)


def write_error(ctx: Ctx) -> None:
    try:
        with open(os.path.join(ctx.data_dir, "error.log"), "a", encoding="utf-8") as f:
            f.write("\n[%s]\n%s" % (tu.iso(tu.now_utc()), traceback.format_exc()))
    except OSError:
        pass


def ci_output(name: str, value: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s=%s\n" % (name, value))


# ───────────────────────── 主流程 ─────────────────────────

def run(ctx: Ctx) -> int:
    args = ctx.args
    st = ctx.store
    lock = storemod.Lock(st.path("poll.lock"))
    locked = False
    if not ctx.dry:
        locked = lock.acquire()
        if not locked:
            say("另一個輪詢還在執行，這次略過")
            return 0
    try:
        state = st.load("state.json", {})
        cache = st.load("cache.json", {})
        daily = st.load("daily.json", {"days": {}, "path": {}})
        changes = st.load("changes.json", [])
        rows = st.read_lines("intraday.jsonl")

        refresh_refdata(ctx, cache)
        meetings = build_meetings(ctx, cache)
        effr_row = (cache.get("effr") or {}).get("row")

        if args.render_only:
            do_render(ctx, state, daily, rows, changes, cache, meetings)
            return 0
        if not effr_row:
            ctx.log("拿不到目前的目標區間（紐約聯準銀行），這次略過")
            return 1

        settled = settled_rates(ctx, cache, meetings)
        first_run = not daily.get("days") and not rows
        active = tu.market_active(ctx.now)
        closed_due = age_min({"t": state.get("last_quote_t")}, ctx.now) > float(
            ctx.cfg["poll"].get("closed_refresh_min", 120))
        need_quotes = active or closed_due or first_run or args.backfill or args.replay or ctx.dry or args.summary

        quotes: Dict[str, dict] = {}
        errors: List[str] = []
        snap: Optional[dict] = None
        changed = False
        if need_quotes:
            quotes, errors = fetch_quotes(ctx, cache, meetings)
            base_lo, inferred = resolve_base(ctx, effr_row, quotes, settled, meetings, state)
            snap = build_snapshot(ctx, quotes, base_lo, inferred, settled, effr_row["rate"], ctx.now, meetings)
        else:
            base_lo = int(state.get("base_lo") or round(float(effr_row["lo"]) * 100))
            inferred = bool(state.get("decided"))

        if args.replay:
            return replay(ctx, args.replay, cache, meetings, base_lo, settled, effr_row["rate"])

        if (first_run or args.backfill) and not ctx.dry:
            backfill(ctx, cache, daily, meetings, base_lo, settled, effr_row["rate"])
            rows = st.read_lines("intraday.jsonl")
            changed = True

        ok = bool(snap and snap["m"])
        if need_quotes and not ok:
            if active:
                detail = errors or ["期貨報價不足，算不出最近一場會議"]
                ctx.log("抓資料失敗：%s" % "；".join(detail[:3]))
                if not ctx.dry and detect.note_failure(state, ctx.cfg):
                    if deliver(ctx, messages.compose_failure(detail, int(state.get("fail_n", 0)), ctx.cfg)):
                        state["fail_alerted"] = True
                state["errors"] = detail[:3]
        elif ok:
            state["errors"] = []
            if not ctx.dry and detect.note_success(state):
                if deliver(ctx, messages.compose_recovered()) is not False:
                    state["fail_alerted"] = False
            state["base_lo"] = base_lo
            state["last_quote_t"] = tu.iso(ctx.now)
            state["last_ok"] = tu.iso(ctx.now)

            new_day = update_settle(ctx, quotes, daily, meetings, base_lo, settled, effr_row["rate"])
            if new_day:
                changed = True
                ctx.log("記下 %s 的定盤值" % new_day)
                if not ctx.dry:
                    st.save("daily.json", daily)

            row = row_of(snap)
            prev_row = rows[-1] if rows else None
            if not prev_row or prev_row.get("m") != row["m"] or prev_row.get("b") != row["b"]:
                if not ctx.dry:
                    st.append_line("intraday.jsonl", row)
                rows.append(row)
                changed = True

            def find_ref(t: dt.datetime) -> Optional[dict]:
                key = tu.iso(t)
                lo, hi = 0, len(rows)
                while lo < hi:                         # 找最後一筆 t <= key 的快照
                    mid = (lo + hi) // 2
                    if rows[mid]["t"] <= key:
                        lo = mid + 1
                    else:
                        hi = mid
                return rows[lo - 1] if lo else None

            if active:
                events = list((cache.get("calendar") or {}).get("items", {}).values())
                res = detect.evaluate(state, snap, find_ref, events, ctx.now, ctx.cfg)
                if res["reset"]:
                    ctx.log("重設比較基準（%s）" % {"first": "首次執行", "month": "換月",
                                             "meeting": "最近一場會議已結束"}.get(res["reset"], res["reset"]))
                if res["alert"]:
                    if handle_alert(ctx, state, snap, res["alert"], cache, changes):
                        changed = True
                elif res["capped"] and not (state.get("sent") or {}).get("capped"):
                    msg = "<b>📈 今天的利率預期變動較頻繁</b>\n已達每日通知上限，其餘變動請看儀表板或明早的每日總結。"
                    if deliver(ctx, msg) is not False and not ctx.dry:
                        state["sent"]["capped"] = True
                detect.prune_events_done(state, ctx.now)

        ref_snap = snap if ok else (dict(rows[-1], g={}) if rows else None)
        if maybe_summary(ctx, state, ref_snap, daily, rows, changes, cache, force=bool(args.summary)):
            changed = True

        if ctx.dry:
            print_dry(ctx, snap, state)
            return 0

        if rows and age_min({"t": state.get("compact_t")}, ctx.now) > 24 * 60:
            compacted = storemod.compact_intraday(rows, ctx.now)
            if len(compacted) != len(rows):
                st.write_lines("intraday.jsonl", compacted)
                rows = compacted
            state["compact_t"] = tu.iso(ctx.now)

        state["last_run"] = tu.iso(ctx.now)
        state["warnings"] = ctx.warnings[:5]
        state["version"] = __version__
        st.save("state.json", state)
        st.save("cache.json", cache)
        st.save("changes.json", changes)

        render_due = changed or age_min({"t": state.get("render_t")}, ctx.now) > 60
        if render_due:
            do_render(ctx, state, daily, rows, changes, cache, meetings)
            state["render_t"] = tu.iso(ctx.now)
            st.save("state.json", state)
        ci_output("changed", "true" if render_due else "false")

        if ok:
            d0 = sorted(snap["m"])[0]
            top = max(snap["m"][d0], key=lambda b: snap["m"][d0][b])
            ctx.log("OK　%s 會議最可能 %s（%s）%s" % (
                tu.fmt_md(tu.parse_date(d0)), messages.bucket_range(top), messages.pct(snap["m"][d0][top]),
                "　｜　" + "；".join(ctx.warnings[:2]) if ctx.warnings else ""))
        elif not need_quotes:
            say("休市中，沿用上次的資料")
        return 0 if (ok or not need_quotes or not active) else 1
    finally:
        if locked:
            lock.release()


def print_dry(ctx: Ctx, snap: Optional[dict], state: dict) -> None:
    say("── 試跑（不寫檔、不發訊息）　台北時間 %s ──" % tu.fmt_local(ctx.now, ctx.offset))
    if not snap or not snap["m"]:
        say("沒有算出任何會議的機率")
    else:
        say("目前目標區間 %s%s" % (messages.bucket_range(snap["b"]), "（推定）" if snap.get("inf") else ""))
        dates = sorted(snap["m"])
        for i, d in enumerate(dates):
            dist = snap["m"][d]
            cells = "　".join("%s %s" % (messages.bucket_range(b), messages.pct(p))
                             for b, p in sorted(dist.items(), key=lambda kv: int(kv[0])) if p >= 0.0005)
            flag = "（不觸發通知）" if d in snap["s"] else ("（成交稀疏）" if d in snap.get("thin", []) else "")
            say("%s　%s%s" % (d, cells, flag))
    for w in ctx.warnings:
        say("注意：" + w)
    for i, text in enumerate(ctx.would_send, 1):
        say("\n──── 原本會發的訊息 %d ────" % i)
        say(text)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="聯準會利率機率追蹤：一次輪詢")
    ap.add_argument("--dry-run", action="store_true", help="只印結果，不寫檔、不發訊息")
    ap.add_argument("--no-notify", action="store_true", help="照常寫檔，但不發 Telegram")
    ap.add_argument("--backfill", action="store_true", help="重新回補歷史資料")
    ap.add_argument("--replay", metavar="YYYY-MM-DD", help="重播某一天，印出當時會發的通知")
    ap.add_argument("--render-only", action="store_true", help="只重產儀表板")
    ap.add_argument("--summary", action="store_true", help="立刻產生每日總結")
    ap.add_argument("--variant", choices=("local", "public", "artifact"), default="local",
                    help="儀表板版本：local 本機（含私人內容）、public 公開、artifact 給 claude.ai")
    ap.add_argument("--data-dir", help="資料目錄（預設 ./data）")
    ap.add_argument("--out-dir", help="儀表板輸出目錄（預設 ./out）")
    ap.add_argument("--ci", action="store_true", help="在 GitHub Actions 裡執行（開始前隨機等幾十秒）")
    args = ap.parse_args(argv)
    if args.ci and not (args.dry_run or args.render_only):
        time.sleep(random.uniform(0, 40))              # 避免所有複本同一秒打同一個資料來源
    ctx = Ctx(args)
    try:
        return run(ctx)
    except Exception:                                  # noqa: BLE001
        say(traceback.format_exc())
        write_error(ctx)
        return 1


if __name__ == "__main__":
    sys.exit(main())
