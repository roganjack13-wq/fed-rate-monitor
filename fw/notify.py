"""notify.py — Telegram Bot 通知。

金鑰讀取順序：
  1. 環境變數 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID（GitHub Actions 的 Secrets 用這個）
  2. Windows 認證管理員（telegram_setup.py 會存進去）
  3. 都沒有 → 不發訊息（程式照常產生儀表板）

Token 絕不寫進任何檔案、log 或錯誤訊息。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import List, Optional, Tuple

from . import wincred

ENV_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_CHAT = "TELEGRAM_CHAT_ID"
CRED_TOKEN = "fed-rate-monitor/telegram_bot_token"
CRED_CHAT = "fed-rate-monitor/telegram_chat_id"
MAX_LEN = 3900


class TelegramError(Exception):
    """訊息已去除 Token，可以安全地寫進 log。"""


def credentials() -> Tuple[Optional[str], Optional[str], str]:
    """回傳 (token, chat_id, 來源)。來源：env／wincred／none。"""
    token = (os.environ.get(ENV_TOKEN) or "").strip()
    chat = (os.environ.get(ENV_CHAT) or "").strip()
    if token and chat:
        return token, chat, "env"
    if wincred.available():
        token = wincred.read(CRED_TOKEN) or ""
        chat = wincred.read(CRED_CHAT) or ""
        if token and chat:
            return token, chat, "wincred"
    return None, None, "none"


def configured() -> bool:
    return credentials()[2] != "none"


def api(token: str, method: str, params: Optional[dict] = None, timeout: int = 20) -> dict:
    """呼叫 Telegram Bot API。任何錯誤都轉成不含 Token 的 TelegramError。"""
    url = "https://api.telegram.org/bot%s/%s" % (token, method)
    data = urllib.parse.urlencode(params or {}).encode("utf-8")
    try:
        req = urllib.request.Request(url, data=data)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        desc = ""
        try:
            desc = json.loads(exc.read().decode("utf-8")).get("description", "")
        except Exception:                             # noqa: BLE001
            pass
        raise TelegramError(("HTTP %d %s" % (exc.code, desc)).replace(token, "***").strip())
    except Exception as exc:                          # noqa: BLE001
        raise TelegramError(type(exc).__name__)


def chunks(text: str, limit: int = MAX_LEN) -> List[str]:
    """超過長度就在換行處切開（每一行本身是完整的，不會切壞 HTML 標籤）。"""
    if len(text) <= limit:
        return [text]
    out: List[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            out.append(cur)
            cur = ""
        cur = line if not cur else cur + "\n" + line
    if cur:
        out.append(cur)
    return out


def send(text: str, silent: bool = False, log=None) -> bool:
    """送出訊息（HTML 格式）。沒設定或失敗回 False，不丟例外。"""
    token, chat, source = credentials()
    if source == "none":
        return False
    ok_all = True
    for part in chunks(text):
        ok = False
        for attempt in (1, 2):                        # 網路一時不穩時重試一次
            try:
                res = api(token, "sendMessage", {
                    "chat_id": chat, "text": part, "parse_mode": "HTML",
                    "disable_web_page_preview": "true",
                    "disable_notification": "true" if silent else "false"})
                ok = bool(res.get("ok"))
                if not ok and log:
                    log("Telegram 送出失敗：%s" % str(res.get("description", ""))[:200])
                break
            except TelegramError as exc:
                if log:
                    log("Telegram 送出失敗（第 %d 次）：%s" % (attempt, exc))
                if attempt == 1:
                    time.sleep(3)
        ok_all = ok_all and ok
    return ok_all
