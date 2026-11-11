#!/usr/bin/env python3
"""在 GitHub Actions 裡查 Telegram chat id，不把 id 印在公開的執行紀錄上。

只有 TELEGRAM_BOT_TOKEN：對每個找到的對話，由 bot 私訊「你的 chat id 是…」。
兩個都有：送一則測試訊息。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fw import notify  # noqa: E402


def main() -> int:
    token = (os.environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
    chat = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()
    if not token:
        print("沒有設定 Secret TELEGRAM_BOT_TOKEN。請到 Settings → Secrets and variables → Actions 新增。")
        return 1
    if chat:
        ok = notify.send("🔔 利率機率追蹤：測試訊息（GitHub Actions）")
        print("測試訊息已送出" if ok else "測試訊息送出失敗：請確認 TELEGRAM_CHAT_ID 正確，而且你已經對 bot 按過「開始」")
        return 0 if ok else 1
    try:
        upd = notify.api(token, "getUpdates")
    except notify.TelegramError as exc:
        print("Token 無法使用：%s" % exc)
        return 1
    ids = []
    for u in upd.get("result", []):
        msg = u.get("message") or u.get("edited_message") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid is not None and cid not in ids:
            ids.append(cid)
    for cid in ids:
        print("::add-mask::%s" % cid)
        try:
            notify.api(token, "sendMessage", {"chat_id": cid, "text":
                       "你的 chat id 是 %s\n請把它存成 GitHub Secret：TELEGRAM_CHAT_ID" % cid})
        except notify.TelegramError:
            pass
    if not ids:
        print("找不到對話：請先在 Telegram 打開你的 bot，按「開始」或傳一句話，再重新執行。")
        return 1
    print("已把 chat id 私訊到 %d 個對話，請到 Telegram 查看。" % len(ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
