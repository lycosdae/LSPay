# LSPay

LSPay 是金流後台，商戶 API 與 FuturePay v1s 相容。遊戲網站只需要換掉 API 網址與金鑰，原本串 FuturePay 的程式碼就能直接使用。

- **商戶 API**：`/v1s/order/receive`（收款）、`/v1s/order/withdraw`（付款）、`/v1s/order/list`（查單）、`/v1s/client/balance`（餘額）。訂單完成時會依 FuturePay 格式回調商戶。
- **管理後台**：儀表板、會員列表、建立會員、車隊、車隊跑單、收款訂單、付款訂單、待審核付款訂單、用戶資料、商戶、收款帳戶。
- **Telegram**：新訂單與狀態變更會推送到群組。審單員可以在群組按鈕審核付款，或用指令確認入帳。

## 金流流程

**收款（入金）**
1. 遊戲站呼叫 `order/receive`，系統配對一個公司收款帳戶並回傳，同時提供收銀台連結 `fronttable_url`。
2. 玩家從**自己登記的帳戶**轉帳。登記帳戶來源有兩個：`bank_no_check` 帶入的帳戶，或後台會員資料中的帳戶。
3. 審單員依銀行入帳明細，在後台或 Telegram 填入實際轉出帳戶與金額。系統比對後，**帳戶或金額不符就不入帳**，只有管理員可以填寫原因後強制入帳。
4. 入帳後狀態改為「已完成(3)」，商戶餘額扣除手續費後增加，並回調遊戲站。
5. 超過 `RECEIVE_TIMEOUT_MINUTES` 仍未入帳的訂單，會自動標記為「收款超時(91)」。

**付款（出金）**
1. 遊戲站呼叫 `order/withdraw`，系統先檢查商戶餘額，再把「金額＋手續費」轉為凍結金額，訂單進入「待審核付款訂單」。
2. 若收款戶名與會員姓名不同、帳號不在會員登記帳戶中、或會員已停用，畫面和 Telegram 都會顯示 ⚠️。
3. 審核通過後，從公司帳戶轉帳，再按「已出款」，訂單完成並回調遊戲站。
4. 審核駁回或出款失敗時，凍結金額會退回商戶餘額。

**收款帳戶**只能登記公司名下的帳戶，或是銀行、合法金流商提供的虛擬帳號。

## 訂單狀態（與 FuturePay 相同）

0 新訂單、1 已配對、2 已收單、3 已完成、4 回調失敗、90 付款超時、91 收款超時、92 金額不符、95 訂單無效、96 駁回單、98 餘額不足、99 超時配對無效單。

## 簽名

- **API 呼叫**：參數名依 ASCII 排序，組成 `k=v&k=v…&key=<Sign Key>`，不做 URL encode，取 MD5 小寫放入 `sign`。Header 需帶 `CLIENTSID` 與 `ACCESSTOKEN`。
- **回調**：`md5(API Key + 金額 + 商戶單號 + Sign Key)`，金額去掉尾端的 0（例如 `123.40` 寫成 `123.4`）。Header `ACCESSTOKEN` 為 API Key，商戶收到後需回應純文字 `success`，否則系統會依 1、2、5、10、30、60 分鐘的間隔重送。

完整的串接範例見 `scripts/client_example.py`。

## 本機試用

```bash
pip install -r requirements-dev.txt
DATABASE_URL=sqlite:///./demo.db python scripts/demo_seed.py   # 示範資料：admin / admin1234
DATABASE_URL=sqlite:///./demo.db SESSION_SECRET=dev uvicorn lspay.main:app --reload
# 開啟 http://localhost:8000/admin
pytest
```

## 正式部署

1. 執行 `cp .env.example .env`，填入資料庫密碼、`BASE_URL`、`SESSION_SECRET`，以及要用的 Telegram 設定。
2. 執行 `docker compose up -d --build`。
3. 執行 `docker compose exec app python -m lspay.cli create-admin <帳號>` 建立第一個管理員。
4. 在前面架一層 HTTPS 反向代理（nginx 或 Caddy），轉發到 `127.0.0.1:8000`。商戶 IP 白名單判斷的是代理轉過來的真實 IP。
5. 登入後台，依序建立收款帳戶、商戶（系統會自動產生 Client SID、API Key、Sign Key，請立即複製）、車隊與用戶。
6. 如果要用 Telegram：
   - 用 @BotFather 建立 bot，把 bot 加進群組，將群組 ID 填入 `TELEGRAM_CHAT_ID` 或各車隊的設定。
   - 執行 `docker compose exec app python -m lspay.cli set-telegram-webhook https://<你的網域>/telegram/webhook`。
   - 在「用戶資料」為每位審單員填入 Telegram 數字 ID。

只跑**一個** app 程序。背景排程（逾時、回調重送）在 app 程序內執行，跑多個程序會造成重複回調。

### Telegram 指令

| 指令 | 說明 |
|---|---|
| `/q 單號` | 查單 |
| `/in 系統單號 822-帳號 金額` | 確認收款入帳（會比對帳戶與金額） |
| `/pending` | 列出待審核付款訂單 |
| `/reject 系統單號 原因` | 駁回付款 |
| `/balance` | 查詢商戶餘額 |

付款訂單訊息下方有「通過／駁回」按鈕，審核通過後會換成「已出款／出款失敗」按鈕。只有已綁定 Telegram ID 的審單員或管理員，在已設定的群組中操作才有效。

## 角色

- **管理員**：所有功能，包括商戶、收款帳戶、用戶管理、強制入帳、調整餘額。
- **審單員**：確認入帳、審核付款、出款。
- **客服**：查看資料、管理會員。
