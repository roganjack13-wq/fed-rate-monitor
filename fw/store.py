"""store.py — JSON／JSONL 檔案儲存：原子寫入、lock、資料保留、簡單的 log。

檔案（都在 data 目錄）：
  state.json      通知狀態（基準、連續確認、每日總結、失敗計數）
  intraday.jsonl  盤中快照，一行一筆（10 分鐘一筆留 7 天，之後每小時一筆留 90 天）
  daily.json      每日定盤值與各場會議後的預期利率
  changes.json    變動紀錄（含對應的數據／事件）
  cache.json      行事曆、RSS、EFFR、已到期月份利率等快取
  poll.log        每次輪詢一行摘要
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
from typing import Any, List

from . import timeutil as tu


class Store:
    def __init__(self, data_dir: str):
        self.dir = data_dir
        os.makedirs(data_dir, exist_ok=True)

    def path(self, name: str) -> str:
        return os.path.join(self.dir, name)

    # ── JSON ──
    def load(self, name: str, default: Any) -> Any:
        p = self.path(name)
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return default
        except (ValueError, OSError):
            try:                                   # 壞掉的檔案改名留著，不直接蓋掉
                os.replace(p, p + ".bad")
            except OSError:
                pass
            return default

    def save(self, name: str, obj: Any) -> None:
        write_text_atomic(self.path(name), json.dumps(obj, ensure_ascii=False, separators=(",", ":")))

    # ── JSONL ──
    def append_line(self, name: str, obj: Any) -> None:
        with open(self.path(name), "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")

    def read_lines(self, name: str) -> List[dict]:
        out: List[dict] = []
        try:
            with open(self.path(name), "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        continue                   # 寫到一半被中斷的那一行直接略過
        except FileNotFoundError:
            pass
        return out

    def write_lines(self, name: str, rows: List[dict]) -> None:
        text = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows)
        write_text_atomic(self.path(name), text)

    # ── log ──
    def log(self, msg: str, offset_hours: float = 8) -> None:
        stamp = tu.to_local(tu.now_utc(), offset_hours).strftime("%Y-%m-%d %H:%M")
        p = self.path("poll.log")
        try:
            if os.path.exists(p) and os.path.getsize(p) > 400_000:
                with open(p, "r", encoding="utf-8", errors="replace") as f:
                    tail = f.read()[-200_000:]
                write_text_atomic(p, tail[tail.find("\n") + 1:])
            with open(p, "a", encoding="utf-8", newline="\n") as f:
                f.write("%s  %s\n" % (stamp, msg.replace("\n", " ")))
        except OSError:
            pass


def write_text_atomic(path: str, text: str) -> None:
    """先寫 .tmp 再換名，避免寫到一半被讀到。Windows 上目標檔被占用時重試幾次。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    for attempt in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.3)


class Lock:
    """同一時間只讓一個輪詢在跑。lock 檔超過 stale_sec 沒更新就視為上一個已經掛掉。"""

    def __init__(self, path: str, stale_sec: int = 900):
        self.path = path
        self.stale_sec = stale_sec
        self.held = False

    def acquire(self) -> bool:
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, ("%d %d" % (os.getpid(), int(time.time()))).encode("ascii"))
                os.close(fd)
                self.held = True
                return True
            except FileExistsError:
                try:
                    if time.time() - os.path.getmtime(self.path) > self.stale_sec:
                        os.remove(self.path)
                        continue
                except OSError:
                    pass
                return False
        return False

    def release(self) -> None:
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass
            self.held = False


def compact_intraday(rows: List[dict], now: dt.datetime, full_days: int = 7, keep_days: int = 90) -> List[dict]:
    """盤中快照的保留策略：最近 full_days 天全留；更早的每小時留第一筆；超過 keep_days 天丟掉。"""
    full_from = now - dt.timedelta(days=full_days)
    keep_from = now - dt.timedelta(days=keep_days)
    out: List[dict] = []
    seen_hours = set()
    for r in sorted(rows, key=lambda x: x.get("t", "")):
        try:
            t = tu.parse_iso(r["t"])
        except (KeyError, ValueError):
            continue
        if t < keep_from:
            continue
        if t >= full_from:
            out.append(r)
            continue
        hour = r["t"][:13]
        if hour not in seen_hours:
            seen_hours.add(hour)
            out.append(r)
    return out
