"""timeutil.py — 時間換算。

不依賴 zoneinfo／tzdata（Windows 預設沒有時區資料庫）：美國夏令時間用規則自己算
（三月第二個週日 02:00 開始、十一月第一個週日 02:00 結束）。
所有對外的時間一律是「有時區的 UTC datetime」；當地時間用 naive datetime 表示。
"""
from __future__ import annotations

import datetime as dt
from typing import Tuple

UTC = dt.timezone.utc
CT_STD = -6   # 美中標準時間（芝加哥，CME 所在地）
ET_STD = -5   # 美東標準時間（聯準會公布決議用）


def now_utc() -> dt.datetime:
    return dt.datetime.now(UTC).replace(microsecond=0)


def parse_iso(s: str) -> dt.datetime:
    """讀 ISO 時間字串（支援結尾 Z 與 ±hh:mm），回傳 UTC。"""
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = dt.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=UTC)
    return d.astimezone(UTC)


def iso(d: dt.datetime) -> str:
    return d.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def from_epoch(ts: float) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, UTC)


def _nth_sunday(year: int, month: int, n: int) -> dt.date:
    first = dt.date(year, month, 1)
    first_sunday = first + dt.timedelta(days=(6 - first.weekday()) % 7)
    return first_sunday + dt.timedelta(weeks=n - 1)


def _dst_bounds_utc(year: int, std_offset: int) -> Tuple[dt.datetime, dt.datetime]:
    start_local = dt.datetime.combine(_nth_sunday(year, 3, 2), dt.time(2, 0))
    end_local = dt.datetime.combine(_nth_sunday(year, 11, 1), dt.time(2, 0))
    start = (start_local - dt.timedelta(hours=std_offset)).replace(tzinfo=UTC)
    end = (end_local - dt.timedelta(hours=std_offset + 1)).replace(tzinfo=UTC)
    return start, end


def us_offset(d_utc: dt.datetime, std_offset: int) -> int:
    """某個 UTC 時間點，該美國時區相對 UTC 的小時數（含夏令時間）。"""
    start, end = _dst_bounds_utc(d_utc.year, std_offset)
    return std_offset + 1 if start <= d_utc < end else std_offset


def to_zone(d_utc: dt.datetime, std_offset: int) -> dt.datetime:
    """UTC → 美國當地時間（naive）。"""
    d_utc = d_utc.astimezone(UTC)
    return (d_utc + dt.timedelta(hours=us_offset(d_utc, std_offset))).replace(tzinfo=None)


def from_zone(local: dt.datetime, std_offset: int) -> dt.datetime:
    """美國當地時間（naive）→ UTC。換日光節約那一小時的模糊時段不特別處理。"""
    guess = (local - dt.timedelta(hours=std_offset)).replace(tzinfo=UTC)
    off = us_offset(guess, std_offset)
    return (local - dt.timedelta(hours=off)).replace(tzinfo=UTC)


def to_ct(d_utc: dt.datetime) -> dt.datetime:
    return to_zone(d_utc, CT_STD)


def to_et(d_utc: dt.datetime) -> dt.datetime:
    return to_zone(d_utc, ET_STD)


def to_local(d_utc: dt.datetime, offset_hours: float = 8) -> dt.datetime:
    """UTC → 使用者當地時間（naive），預設台北 UTC+8。"""
    return (d_utc.astimezone(UTC) + dt.timedelta(hours=offset_hours)).replace(tzinfo=None)


def parse_date(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def settle_utc(day: dt.date) -> dt.datetime:
    """ZQ 每日定盤時間：美中時間 14:00。"""
    return from_zone(dt.datetime.combine(day, dt.time(14, 0)), CT_STD)


def decision_utc(day: dt.date) -> dt.datetime:
    """FOMC 決議公布時間：美東時間 14:00。"""
    return from_zone(dt.datetime.combine(day, dt.time(14, 0)), ET_STD)


def cme_open(d_utc: dt.datetime) -> bool:
    """CME Globex 利率期貨交易時段：週日 17:00 ～ 週五 16:00（美中），每天 16:00–17:00 休息。
    美國假日不另外處理（休市時價格不動，不會誤發通知）。"""
    ct = to_ct(d_utc)
    wd = ct.weekday()            # 週一=0 … 週日=6
    hm = ct.hour * 60 + ct.minute
    if wd == 5:
        return False
    if wd == 6:
        return hm >= 17 * 60
    if wd == 4:
        return hm < 16 * 60
    return not (16 * 60 <= hm < 17 * 60)


def market_active(d_utc: dt.datetime, grace_min: int = 30) -> bool:
    """交易時段，或剛收盤不久（報價有延遲，收盤後一小段時間還會有新資料）。"""
    return cme_open(d_utc) or cme_open(d_utc - dt.timedelta(minutes=grace_min))


def last_settle_day(d_utc: dt.datetime, lag_min: int = 12) -> dt.date:
    """最近一個「定盤時間已過」的平日（美中日期）。"""
    day = to_ct(d_utc).date()
    for _ in range(10):
        if day.weekday() < 5 and settle_utc(day) + dt.timedelta(minutes=lag_min) <= d_utc:
            return day
        day -= dt.timedelta(days=1)
    return day


def session_close_utc(day: dt.date) -> dt.datetime:
    """美中時間 17:00：當天的交易日結束、下一盤開始（報價源的「前一日結算價」大約這時換成當天的）。"""
    return from_zone(dt.datetime.combine(day, dt.time(17, 0)), CT_STD)


def last_session_day(d_utc: dt.datetime) -> dt.date:
    """最近一個「已經結束」的交易日（美中日期，平日）。"""
    day = to_ct(d_utc).date()
    for _ in range(10):
        if day.weekday() < 5 and session_close_utc(day) <= d_utc:
            return day
        day -= dt.timedelta(days=1)
    return day


def prev_weekday(day: dt.date) -> dt.date:
    day -= dt.timedelta(days=1)
    while day.weekday() >= 5:
        day -= dt.timedelta(days=1)
    return day


def fmt_local(d_utc: dt.datetime, offset_hours: float = 8, with_date: bool = True) -> str:
    loc = to_local(d_utc, offset_hours)
    return ("%d/%d %02d:%02d" % (loc.month, loc.day, loc.hour, loc.minute)) if with_date \
        else ("%02d:%02d" % (loc.hour, loc.minute))


WEEKDAY_ZH = "一二三四五六日"


def fmt_md(day: dt.date, with_weekday: bool = False) -> str:
    s = "%d/%d" % (day.month, day.day)
    return s + "（%s）" % WEEKDAY_ZH[day.weekday()] if with_weekday else s
