#!/usr/bin/env python3
"""telegram_setup.py — 把 Telegram Bot 設定存進 Windows 認證管理員（只用 Python 標準函式庫）。

步驟：
  1. 在 Telegram 打開你的 bot，按「開始」或隨便傳一句話給它（這樣才查得到你的 chat id）
  2. 到 @BotFather 複製 bot 的 token
  3. 執行：python -X utf8 telegram_setup.py
     會從剪貼簿讀 token（讀完立刻清空剪貼簿），找到你的 chat id，存好後送一則測試訊息。

其他用法：
  python -X utf8 telegram_setup.py --test     送一則測試訊息
  python -X utf8 telegram_setup.py --status   看目前有沒有設定（不顯示 token）
  python -X utf8 telegram_setup.py --delete   刪除設定

Token 不會顯示在畫面上、不會寫進任何檔案或 log。
"""
from __future__ import annotations

import argparse
import getpass
import os
import re
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fw import notify, wincred  # noqa: E402

TOKEN_RX = re.compile(r"\d{6,12}:[A-Za-z0-9_-]{30,}")


def read_clipboard_token() -> str:
    """從剪貼簿找 token；找到就清空剪貼簿，避免之後被誤貼到別處。"""
    try:
        import tkinter
        r = tkinter.Tk()
        r.withdraw()
        try:
            text = r.clipboard_get()
        except tkinter.TclError:
            text = ""
        m = TOKEN_RX.search(text or "")
        if m:
            r.clipboard_clear()
            r.update()
        r.destroy()
        return m.group(0) if m else ""
    except Exception:                                  # noqa: BLE001
        return ""


def find_chat(token: str):
    upd = notify.api(token, "getUpdates")
    chats = []
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("edited_message") or u.get("channel_post") or {}
        chat = msg.get("chat") or {}
        if chat.get("id") is not None and chat["id"] not in [c["id"] for c in chats]:
            chats.append(chat)
    return chats


def setup() -> int:
    if not wincred.available():
        print("這個設定程式只用在 Windows。其他系統請設定環境變數 TELEGRAM_BOT_TOKEN、TELEGRAM_CHAT_ID。")
        return 1
    print("1. 先在 Telegram 打開你的 bot，按「開始」或傳一句話給它")
    print("2. 到 @BotFather 複製 bot 的 token，再執行本程式（會自動從剪貼簿讀取）")
    token = read_clipboard_token()
    if token:
        print("已從剪貼簿讀到 token（讀完已清空剪貼簿）")
    else:
        token = getpass.getpass("剪貼簿裡沒有 token。請按滑鼠右鍵貼上後按 Enter（輸入時不會顯示）：")
    token = "".join(ch for ch in token if ch.isprintable()).strip()
    if not TOKEN_RX.fullmatch(token):
        print("收到的內容不像 bot token（格式應該是「數字:英數字」）。請重新複製後再執行一次。")
        return 1
    try:
        me = notify.api(token, "getMe")
    except notify.TelegramError as exc:
        print("Token 無法使用：%s。若剛用 /revoke 換過 token，請改用新的。" % exc)
        return 1
    if not me.get("ok"):
        print("Token 無法使用。")
        return 1
    bot = me["result"].get("username")
    chats = find_chat(token)
    if not chats:
        print("找不到對話。請先打開 @%s 傳一句話給它，再重新執行一次。" % bot)
        return 1
    chat = chats[-1]
    wincred.write(notify.CRED_TOKEN, token)
    wincred.write(notify.CRED_CHAT, str(chat["id"]))
    who = chat.get("title") or chat.get("first_name") or "你的對話"
    ok = notify.send("<b>✅ 利率機率追蹤已連上 Telegram</b>\n之後的變動通知與每日總結會傳到這裡。")
    print("已存進 Windows 認證管理員（bot @%s → %s）%s" % (bot, who, "，測試訊息已送出" if ok else "，但測試訊息送出失敗"))
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description="設定 Telegram 通知")
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--delete", action="store_true")
    a = ap.parse_args()
    if a.status:
        src = notify.credentials()[2]
        print({"env": "已設定（環境變數）", "wincred": "已設定（Windows 認證管理員）", "none": "尚未設定"}[src])
        return 0
    if a.delete:
        wincred.delete(notify.CRED_TOKEN)
        wincred.delete(notify.CRED_CHAT)
        print("已刪除 Telegram 設定")
        return 0
    if a.test:
        if not notify.configured():
            print("尚未設定，請先執行 python -X utf8 telegram_setup.py")
            return 1
        ok = notify.send("🔔 利率機率追蹤：測試訊息")
        print("已送出" if ok else "送出失敗")
        return 0 if ok else 1
    return setup()


if __name__ == "__main__":
    sys.exit(main())
