"""detect.py — 變化偵測與去重。純邏輯、不做任何讀寫或連網，方便用合成資料測試。

快照（snap）格式：
  {"t": "2026-11-10T13:52:00Z", "b": 375,
   "m": {"2026-12-09": {"400": 0.9085, "425": 0.0915}, ...},   # 會議日 → {區間下限 bp: 機率}
   "s": ["2027-12-08"],                                        # 資料品質不足、不拿來觸發通知的會議
   "q": {"2026-12-09": 1794361980},                            # 算這場會議用到的合約中，最新一筆成交的時間
   "g": {"2026-12-09": "1794359100,1794361980"}}               # 上述合約的成交時間組合（變了代表有新成交）

狀態（state）裡跟偵測有關的欄位：
  baseline      上次「告知使用者」時的快照（通知或每日總結之後更新）
  pending       連續確認用：{"key", "n", "g", "since"}
  last_alert_t  上次通知時間
  events_done   已處理過的行事曆事件 id
  month、nearest  用來發現換月與會議結束
  sent          {"day": 當地日期, "n": 今天已發幾則, "capped": 是否已提醒過}

為什麼「確認」要看有沒有新成交：期貨跳一檔，最近一場會議的機率就會動 1–2 個百分點。
沒有新成交時，下一次輪詢看到的是同一個價格，不能算確認。
"""
from __future__ import annotations

import datetime as dt
from typing import Callable, Dict, List, Optional, Tuple

from . import timeutil as tu

Dist = Dict[str, float]


def max_move(cur: Dist, ref: Dist) -> Tuple[Optional[str], float]:
    """變動最大的區間與變動量（機率，0–1）。"""
    best_b: Optional[str] = None
    best = 0.0
    for b in set(cur) | set(ref):
        d = cur.get(b, 0.0) - ref.get(b, 0.0)
        if abs(d) > abs(best):
            best_b, best = b, d
    return best_b, best


def top_bucket(d: Dist) -> Optional[str]:
    if not d:
        return None
    return max(d.items(), key=lambda kv: (kv[1], -int(kv[0])))[0]


def expected_bp(d: Dist) -> float:
    total = sum(d.values()) or 1.0
    return sum(int(b) * p for b, p in d.items()) / total


def move_pp(cur: Dist, ref: Dist, nearest: bool = False) -> float:
    """變動量（百分點）＝單一區間的最大機率變動。

    最近一場會議另外跟「預期碼數的變動」取較大者，補上跨過整數的情況：
    預期從升 0.97 碼變成 1.03 碼時，每個區間都只動 3 個百分點，但市場預期其實動了 6。
    較遠的會議是累計分布，預期碼數的變動會把好幾個區間的小變動加總，所以不套用。"""
    _, diff = max_move(cur, ref)
    out = abs(diff)
    if nearest:
        out = max(out, abs(expected_bp(cur) - expected_bp(ref)) / 25.0)
    return out * 100.0


def direction(cur: Dist, ref: Dist) -> str:
    return "+" if expected_bp(cur) >= expected_bp(ref) else "-"


def is_key_event(e: dict) -> bool:
    """會觸發「公布後反應」檢查的事件：行事曆標為高影響的（CPI、PCE、非農、FOMC 決議等）。"""
    return e.get("impact") == "High"


def set_baseline(state: dict, snap: dict) -> None:
    state["baseline"] = {"t": snap["t"], "m": snap["m"]}


def _confirm(state: dict, key: str, trade_sig: str, now: dt.datetime) -> int:
    """同一個方向、而且期間有新成交，才把確認次數加一。"""
    pend = state.get("pending") or {}
    if pend.get("key") != key:
        pend = {"key": key, "n": 1, "g": trade_sig, "since": tu.iso(now)}
    elif pend.get("g") != trade_sig:
        pend = {"key": key, "n": int(pend.get("n", 0)) + 1, "g": trade_sig,
                "since": pend.get("since") or tu.iso(now)}
    state["pending"] = pend
    return int(pend["n"])


def _sent_today(state: dict, now: dt.datetime, cfg: dict) -> dict:
    day = tu.to_local(now, cfg.get("local_utc_offset_hours", 8)).date().isoformat()
    sent = state.get("sent") or {}
    if sent.get("day") != day:
        sent = {"day": day, "n": 0, "capped": False}
        state["sent"] = sent
    return sent


def evaluate(state: dict, snap: dict, find_ref: Callable[[dt.datetime], Optional[dict]],
             events: List[dict], now: dt.datetime, cfg: dict) -> dict:
    """看這一筆快照要不要發通知。

    find_ref(t) 要回傳「t 當下或之前最後一筆」已存的快照（沒有就回 None）。
    回傳 {"alert": dict 或 None, "reset": 原因或 None, "capped": bool}。
    只會更新 state 的 pending／month／nearest／過期的事件；基準要等訊息送出成功後由 commit_alert 更新。
    """
    a = cfg["alert"]
    result: dict = {"alert": None, "reset": None, "capped": False}
    dates = sorted(snap.get("m") or {})
    if not dates:
        return result
    skip = set(snap.get("s") or [])
    watch = [d for d in dates[: int(a["meetings"])] if d not in skip]
    trade_sig = "|".join((snap.get("g") or {}).get(d, "") for d in watch)
    confirm_need = int(a["confirm_polls"])

    month = tu.to_ct(now).strftime("%Y-%m")
    reset = None
    if not state.get("baseline"):
        reset = "first"
    elif state.get("month") != month:
        reset = "month"
    elif state.get("nearest") != dates[0]:
        reset = "meeting"
    if reset:                                   # 首次執行、換月、會議剛結束：只重設基準，不發通知
        set_baseline(state, snap)
        state["pending"] = None
        result["reset"] = reset
    state["month"] = month
    state["nearest"] = dates[0]

    sent = _sent_today(state, now, cfg)
    cap = int(a.get("max_per_day", 0) or 0)

    def finish(alert: dict) -> dict:
        if cap and int(sent.get("n", 0)) >= cap:
            result["capped"] = True
            return result
        result["alert"] = alert
        return result

    # 1) 重要數據公布後的反應（跟「公布前最後一筆快照」比，不看基準）
    done = state.setdefault("events_done", {})
    after = dt.timedelta(minutes=float(a["event_after_min"]))
    until = dt.timedelta(minutes=float(a["event_until_min"]))
    ev_thr = float(a["event_threshold_pp"])
    groups: Dict[str, List[dict]] = {}
    for e in events:
        if is_key_event(e) and e["id"] not in done:
            groups.setdefault(e["t"], []).append(e)
    in_window: List[dict] = []                  # 已公布、還在觀察時間內的重要事件
    for t_iso in sorted(groups):
        te = tu.parse_iso(t_iso)
        if now > te + until:                    # 觀察時間過了都沒有明顯反應：記為已處理
            for e in groups[t_iso]:
                done[e["id"]] = tu.iso(now)
            continue
        if now < te:
            continue
        in_window.extend(groups[t_iso])
        if now < te + after:
            continue
        newest = max([int((snap.get("q") or {}).get(d, 0) or 0) for d in watch] or [0])
        if newest < te.timestamp():             # 報價還沒有公布之後的成交（報價本身有延遲）
            continue
        ref = find_ref(te - dt.timedelta(seconds=30))
        if not ref:
            continue
        moved = 0.0
        lead = None
        for d in watch:
            if d in ref["m"]:
                mv = move_pp(snap["m"][d], ref["m"][d], nearest=(d == dates[0]))
                if mv > moved:
                    moved, lead = mv, d
        if lead is None or moved < ev_thr:
            continue
        alert = {"kind": "event", "t": tu.iso(now), "ref_t": ref["t"], "ref": ref["m"],
                 "events": groups[t_iso], "watch": watch, "moved_pp": round(moved, 1)}
        if moved >= 2.0 * ev_thr:               # 變動夠大就不用等確認
            return finish(alert)
        key = "ev|%s|%s" % (t_iso, direction(snap["m"][lead], ref["m"][lead]))
        if _confirm(state, key, trade_sig, now) >= confirm_need:
            return finish(alert)
        return result                           # 等下一筆新成交確認

    if reset:
        return result

    # 2) 相對基準的累積變動，或「最可能的結果」換了
    base = state["baseline"]
    thr = float(a["threshold_pp"])
    best: Optional[dict] = None
    for d in watch:
        ref_d = base["m"].get(d)
        if ref_d is None:
            continue
        cur = snap["m"][d]
        mv = move_pp(cur, ref_d, nearest=(d == dates[0]))
        tc, tr = top_bucket(cur), top_bucket(ref_d)
        flip = bool(tc and tr and tc != tr
                    and (cur.get(tc, 0.0) - cur.get(tr, 0.0)) * 100.0 >= float(a["flip_margin_pp"]))
        if mv >= thr or flip:
            cand = {"date": d, "mv": mv, "flip": flip, "dir": direction(cur, ref_d)}
            if best is None or mv > best["mv"]:
                best = cand
    if best is None:
        pend = state.get("pending") or {}
        if not str(pend.get("key", "")).startswith("ev|"):
            state["pending"] = None
        return result

    big = best["mv"] >= 2.0 * thr
    if not big:
        key = "th|%s|%s" % (best["date"], best["dir"])
        if _confirm(state, key, trade_sig, now) < confirm_need:
            return result
        last = state.get("last_alert_t")
        if last and now - tu.parse_iso(last) < dt.timedelta(minutes=float(a["cooldown_min"])):
            return result
    kind = "threshold" if best["mv"] >= thr else "flip"
    if in_window:                               # 剛好有重要數據公布：併成同一則，之後不再為同一個事件重發
        kind = "event"
    since = (state.get("pending") or {}).get("since") or tu.iso(now)
    return finish({"kind": kind, "t": tu.iso(now), "ref_t": base["t"], "ref": base["m"], "events": in_window,
                   "watch": watch, "since": since, "moved_pp": round(best["mv"], 1)})


def commit_alert(state: dict, alert: dict, snap: dict, now: dt.datetime, cfg: dict) -> None:
    """訊息送出成功（或沒有設定 Telegram、只記錄不發送）之後才呼叫：更新基準、記下已通知。"""
    set_baseline(state, snap)
    state["pending"] = None
    state["last_alert_t"] = tu.iso(now)
    done = state.setdefault("events_done", {})
    for e in alert.get("events") or []:
        done[e["id"]] = tu.iso(now)
    sent = _sent_today(state, now, cfg)
    sent["n"] = int(sent.get("n", 0)) + 1


def prune_events_done(state: dict, now: dt.datetime, keep_days: int = 14) -> None:
    done = state.get("events_done") or {}
    cutoff = tu.iso(now - dt.timedelta(days=keep_days))
    state["events_done"] = {k: v for k, v in done.items() if v >= cutoff}


def summary_due(state: dict, now: dt.datetime, cfg: dict) -> Optional[str]:
    """每日總結到時間了沒。到了回傳當地日期字串；還沒到、已發過、或晚了超過 6 小時回 None。"""
    s = cfg.get("summary") or {}
    if not s.get("enabled", True):
        return None
    loc = tu.to_local(now, cfg.get("local_utc_offset_hours", 8))
    if loc.weekday() not in (s.get("weekdays") or []):
        return None
    hh, mm = [int(x) for x in str(s.get("time", "07:30")).split(":")]
    late = (loc.hour * 60 + loc.minute) - (hh * 60 + mm)
    if late < 0:
        return None
    today = loc.date().isoformat()
    if state.get("summary_date") == today:
        return None
    if late > 360:                               # 太晚才第一次跑：這天就不補發
        state["summary_date"] = today
        return None
    return today


def note_failure(state: dict, cfg: dict) -> bool:
    """記一次抓資料失敗。回 True 表示這次該發「資料來源異常」。"""
    state["fail_n"] = int(state.get("fail_n", 0)) + 1
    return state["fail_n"] >= int(cfg["alert"]["fail_after"]) and not state.get("fail_alerted")


def note_success(state: dict) -> bool:
    """記一次成功。回 True 表示之前發過異常、現在該發「已恢復」。"""
    recovered = bool(state.get("fail_alerted"))
    state["fail_n"] = 0
    return recovered


def in_quiet_hours(now: dt.datetime, cfg: dict) -> bool:
    q = cfg.get("quiet_hours") or {}
    if q.get("mode", "silent") == "normal":
        return False
    try:
        sh, sm = [int(x) for x in str(q.get("start", "00:00")).split(":")]
        eh, em = [int(x) for x in str(q.get("end", "07:00")).split(":")]
    except ValueError:
        return False
    loc = tu.to_local(now, cfg.get("local_utc_offset_hours", 8))
    cur = loc.hour * 60 + loc.minute
    start, end = sh * 60 + sm, eh * 60 + em
    if start == end:
        return False
    return start <= cur < end if start < end else (cur >= start or cur < end)
