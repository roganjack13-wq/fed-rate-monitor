# 聯準會利率機率追蹤（fed-rate-monitor）

由 30 天期聯邦基金利率期貨（CBOT: ZQ）的公開延遲報價，推算各次 FOMC 會議後的目標利率機率；機率有明顯變化時發 Telegram 通知，並附上時間對得上的經濟數據、聯準會官員談話與新聞標題。另外產生一個單檔儀表板網頁。

- 只用 Python 標準函式庫，不需要 `pip install`
- 每 10 分鐘更新一次（報價本身約延遲 10 分鐘）
- 每個人用自己的 Telegram bot；這個專案不收集任何資料

> **這不是 CME 官方數值。** 計算方法與 CME 公開的 FedWatch 方法論相同，但這裡用最新成交價與每日結算價自行推算，盤中可能與官方數字相差數個百分點。本專案與 CME Group 沒有任何關聯。官方數值請看 [CME FedWatch 工具](https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html)。

## 會收到哪些通知

| 種類 | 條件（可在 `config.json` 調整） |
|---|---|
| 利率預期變動 | 最近兩場會議任一利率區間的機率，比上次通知時變動 5 個百分點以上（連續兩筆新成交都成立才發；變動 10 個百分點以上立即發） |
| 最可能的結果換了 | 例如「升息 1 碼」被「維持不變」超過 |
| 數據公布後的反應 | CPI、非農、PCE、FOMC 決議等高重要性數據公布後 45 分鐘內，相對公布前變動 2 個百分點以上 |
| 每日總結 | 週二到週六 07:30（台北），前一交易日的定盤值、變動、今天要公布的數據 |
| 資料來源異常 | 連續 3 次抓不到報價，恢復時再通知一次 |

同一場會議 60 分鐘內不重複通知，每天最多 8 則；00:00–07:00 的通知以靜音送達。

## 安裝方式一：雲端版（不用安裝軟體，24 小時運作）

1. 在 Telegram 找 **@BotFather** 建立一個 bot（`/newbot`），複製它給你的 token。打開你的 bot，按「開始」。
2. 在這個 repo 的頁面按 **Use this template → Create a new repository**（請用 template，不要用 Fork：Fork 的排程預設是停用的）。建議設成 **Public**（私人 repo 的 Actions 免費分鐘數不夠每 10 分鐘跑一次）。
3. 到你的新 repo：**Settings → Secrets and variables → Actions → New repository secret**，新增 `TELEGRAM_BOT_TOKEN`。
4. **Actions** 分頁 → 左邊選「測試 Telegram（查 chat id）」→ **Run workflow**。bot 會私訊你的 chat id；把它存成第二個 Secret `TELEGRAM_CHAT_ID`，再執行一次會收到測試訊息。
5. **Settings → Pages → Build and deployment → Source** 選 **GitHub Actions**。
6. **Actions** 分頁 → 「更新利率機率」→ **Run workflow** 跑第一次。之後每 10 分鐘自動執行，儀表板網址是 `https://<你的帳號>.github.io/<repo 名稱>/`。

注意：GitHub 的排程是「盡力而為」，常延遲 10–30 分鐘，新建的 repo 有時甚至好幾個小時都不會觸發，所以雲端版的通知會比本機版晚一點。如果你有一台 24 小時開機、能 `git push` 到這個 repo 的電腦，可以定時執行下面這行來確保每 10 分鐘更新（把 main 的最新 commit 推到 `tick` 分支，GitHub 收到推送就會立刻執行）：

```
git fetch -q origin main && git push -q --force origin $(git commit-tree "origin/main^{tree}" -p origin/main -m tick):refs/heads/tick
```
公開 repo 60 天沒有任何活動時 GitHub 會停用排程；這個 workflow 每次執行都會更新 `state` 分支，正常運作下不會被停用。

## 安裝方式二：Windows 本機版（通知最準時，電腦要開著）

1. 安裝 [Python 3.9 以上](https://www.python.org/downloads/windows/)（安裝時勾選「Add python.exe to PATH」）。
2. 下載這個 repo 的 ZIP（**Code → Download ZIP**），在 ZIP 檔按右鍵 →「內容」→ 勾「解除封鎖」，再解壓縮到**不會被 OneDrive 同步**的資料夾。
3. 在 Telegram 建好 bot、按「開始」，複製 token。
4. 雙擊 `windows\install.bat`。它會：從剪貼簿讀 token 存進 Windows 認證管理員（不顯示、不寫檔）→ 回補歷史資料 → 註冊每 10 分鐘的工作排程 → 打開儀表板（`out\index.html`）。
5. 移除：雙擊 `windows\uninstall.bat`（只移除排程；Telegram 設定用 `python -X utf8 telegram_setup.py --delete` 刪除）。

電腦睡眠或沒有登入時不會執行。同一個 bot 不要同時開雲端版與本機版，不然會收到兩份通知。

## 常用指令

```
python -X utf8 poll.py --dry-run              # 只印出目前機率與「原本會發的訊息」，不寫檔、不發送
python -X utf8 poll.py --replay 2026-11-10    # 用歷史 5 分 K 重播某一天，看當時會發哪些通知
python -X utf8 poll.py --summary --dry-run    # 預覽每日總結
python -X utf8 telegram_setup.py --test       # 送一則測試訊息
python -X utf8 -m unittest discover -s tests -t .
```

要改門檻或時區：在同一層建立 `config.local.json`，只寫要覆蓋的欄位，例如 `{"alert": {"threshold_pp": 8}}`。

## 計算方法

1. 合約隱含利率 = 100 − 價格，代表那個月有效聯邦基金利率（EFFR）的日平均。
2. 會議月有 M 天、決議日是 N 號：當月平均 = (N/M)×會議前利率 + ((M−N)/M)×會議後利率。前一個月沒開會時，會議前利率取前一個月合約；下一個月沒開會時，會議後利率取下一個月合約；連續兩個月都有會議時由當月合約往回解。
3. 每場會議的預期變動拆成相鄰兩個「碼數」的機率，再跨會議用機率樹累加，從目前目標區間起算。
4. 已到期的月份改用紐約聯準銀行公布的 EFFR 月平均。會議剛開完、官方目標區間還沒更新的一兩天，由期貨價格推定決議結果，並在頁面上標示「推定」。

## 資料來源

- 期貨報價：Yahoo Finance 的延遲報價（非官方介面，可能失效；報價來源在 `fw/sources.py` 可抽換）
- 有效聯邦基金利率與目標區間：[紐約聯邦準備銀行 Markets API](https://markets.newyorkfed.org/)
- 經濟行事曆：Forex Factory 每週匯出（有預期值與前值，沒有實際值）
- 聯準會新聞稿與演講：federalreserve.gov 官方 RSS
- 新聞標題：Google News RSS、CNBC RSS（只列標題、來源與連結，不轉載內文）

各資料來源有自己的使用條款，請自行確認你的使用方式符合規定。

## 免責聲明

本專案只是公開市場資料的統計整理，用來觀察市場對利率的預期，不構成任何投資建議，也不是任何商品的買賣邀約。計算結果可能有誤差或延遲，依此做出的任何決定，風險由使用者自行承擔。

## 授權

MIT License，見 [LICENSE](LICENSE)。
