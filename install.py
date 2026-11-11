#!/usr/bin/env python3
"""install.py — Windows 本機安裝：檢查環境 → 設定 Telegram（可略過）→ 註冊每 10 分鐘的工作排程 → 跑第一次。

  python -X utf8 install.py              完整安裝
  python -X utf8 install.py --schedule   只註冊（或更新）工作排程
  python -X utf8 install.py --remove     移除工作排程（資料夾與設定不動）
  python -X utf8 install.py --yes        不詢問，直接用預設選項

只用 Python 標準函式庫。工作排程用 XML 註冊（不需要 PowerShell），所以中文使用者名稱的路徑也沒問題。
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import subprocess
import sys
import tempfile
import webbrowser
from xml.sax.saxutils import escape

ROOT = os.path.dirname(os.path.abspath(__file__))
TASK = "FedRateMonitor_poll"


def pythonw() -> str:
    exe = sys.executable
    cand = os.path.join(os.path.dirname(exe), "pythonw.exe")
    return cand if os.path.exists(cand) else exe


def task_xml(user: str) -> str:
    start = dt.datetime.now().replace(hour=0, minute=3, second=0, microsecond=0)
    return """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>聯準會利率機率追蹤：每 10 分鐘抓一次公開期貨報價、偵測變化、發 Telegram、更新儀表板</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger>
      <Repetition><Interval>PT10M</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>%(start)s</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author"><UserId>%(user)s</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <ExecutionTimeLimit>PT8M</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>%(cmd)s</Command>
      <Arguments>%(args)s</Arguments>
      <WorkingDirectory>%(wd)s</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
""" % {"start": start.strftime("%Y-%m-%dT%H:%M:%S"), "user": escape(user), "cmd": escape(pythonw()),
       "args": escape('-X utf8 "%s"' % os.path.join(ROOT, "poll.py")), "wd": escape(ROOT)}


def run(cmd) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, encoding="mbcs", errors="replace")


def register() -> bool:
    user = os.environ.get("USERDOMAIN", "") + "\\" + os.environ.get("USERNAME", "")
    fd, path = tempfile.mkstemp(suffix=".xml")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-16") as f:
            f.write(task_xml(user))
        r = run(["schtasks", "/Create", "/TN", TASK, "/XML", path, "/F"])
    finally:
        os.remove(path)
    if r.returncode != 0:
        print("註冊工作排程失敗：%s" % (r.stderr or r.stdout).strip())
        return False
    print("已註冊工作排程「%s」：每 10 分鐘（:03、:13…）執行一次。" % TASK)
    return True


def remove() -> int:
    r = run(["schtasks", "/Delete", "/TN", TASK, "/F"])
    print("已移除工作排程「%s」" % TASK if r.returncode == 0 else "找不到工作排程「%s」" % TASK)
    return 0


def ask(q: str, yes: bool) -> bool:
    if yes:
        return True
    try:
        return input(q + "（Y/n）").strip().lower() in ("", "y", "yes")
    except EOFError:
        return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schedule", action="store_true")
    ap.add_argument("--remove", action="store_true")
    ap.add_argument("--yes", action="store_true")
    a = ap.parse_args()
    if sys.platform != "win32":
        print("本機安裝程式只支援 Windows；其他環境請看 README 的雲端版。")
        return 1
    if a.remove:
        return remove()
    if a.schedule:
        return 0 if register() else 1

    print("== 聯準會利率機率追蹤：安裝 ==")
    print("Python：%s（%s）" % (sys.version.split()[0], sys.executable))
    if sys.version_info < (3, 9):
        print("需要 Python 3.9 以上。")
        return 1
    if "onedrive" in ROOT.lower():
        print("注意：這個資料夾在 OneDrive 底下，同步可能讓資料檔被鎖住。建議移到不會同步的資料夾。")

    sys.path.insert(0, ROOT)
    from fw import notify
    if notify.configured():
        print("Telegram：已設定")
    elif not a.yes and ask("要現在設定 Telegram 通知嗎？", False):
        subprocess.call([sys.executable, "-X", "utf8", os.path.join(ROOT, "telegram_setup.py")])
    else:
        print("Telegram：先略過（之後執行 python -X utf8 telegram_setup.py 設定；沒設定也會照常更新儀表板）")

    print("第一次執行（回補歷史資料，約 1 分鐘）…")
    code = subprocess.call([sys.executable, "-X", "utf8", os.path.join(ROOT, "poll.py")])
    if code != 0:
        print("第一次執行沒有成功，請看 data\\poll.log 與 data\\error.log。")
    if not register():
        return 1
    page = os.path.join(ROOT, "out", "index.html")
    if os.path.exists(page) and ask("要打開儀表板嗎？", a.yes):
        webbrowser.open("file:///" + page.replace("\\", "/"))
    print("完成。移除排程：python -X utf8 install.py --remove")
    return 0


if __name__ == "__main__":
    sys.exit(main())
