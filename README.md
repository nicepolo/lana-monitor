# LANA 妖幣監測系統

## 專案結構

```
lana_project/
├── app.py                  # 主後端 Flask API（lana-monitor）
├── lana_strategy.py        # 確定性多空評分、訊號 ID、ATR 交易價位
├── paper_trading.py        # 模擬倉位、止盈止損、移動止損
├── requirements.txt        # lana-monitor 依賴
├── Procfile                # Railway 啟動設定
├── templates/
│   └── index.html          # 前端網頁
├── lana_cron/
│   ├── trigger.py          # Cron 掃描推播腳本（lana-cron）
│   ├── requirements.txt    # lana-cron 依賴
│   └── railway.json        # Cron 排程設定（*/15 * * * *）
├── .env.example            # 環境變數範例
├── tests/                  # 策略、模擬交易與 Flask 整合測試
└── LANA_架構說明.md
```

## 兩個獨立 Railway 服務

### 1. lana-monitor（web 服務）
- 常駐運行的 Flask API
- 提供 `/api/scan`、`/api/ai_analyze`、`/telegram/webhook` 等端點
- 所有狀態（快取、推送控制）存在 Python 記憶體中

### 2. lana-cron（排程服務）
- 每15分鐘執行一次 trigger.py
- 掃描 OKX 全市場 → 呼叫 lana-monitor API → 推播 Telegram
- 無狀態，每次都是全新容器

## 部署說明

1. Fork 兩個 Repo 或上傳至自己的 GitHub
2. 在 Railway 建立兩個服務，分別連接對應 Repo
3. 填入環境變數（參考 .env.example）
4. lana-cron 的 `railway.json` 已設定 `cronSchedule: "*/15 * * * *"`
5. lana-monitor 部署後，執行 `GET /telegram/set_webhook` 設定 TG Webhook

## 核心 API 端點

| 端點 | 方法 | 說明 |
|------|------|------|
| /api/scan | GET/POST | 掃描全市場並計算 LANA 分 |
| /api/ai_analyze | POST | 對指定幣種做 AI 深度分析 |
| /api/paper/status | GET | 查詢模擬訊號、持倉與績效 |
| /api/paper/mark | POST | 依目前價格更新模擬止盈止損 |
| /api/push_control | GET/POST | 查詢/設定推播暫停狀態 |
| /telegram/webhook | POST | TG Webhook（接收按鈕點擊）|
| /telegram/set_webhook | GET | 一次性設定 TG Webhook |
| /health | GET | 健康檢查 |

## 已知問題（待優化）

1. **AI 一致性**：已加入確定性規則仲裁；AI 與規則方向衝突時一律 WATCH
2. **單 Worker**：gunicorn sync x1，AI 分析期間會阻塞，建議改 async
3. **記憶體快取**：重新部署會清空，建議改 Redis
4. **自動下單**：目前僅啟用 Paper Trading；需先完成含費用、滑價與回撤的驗證

## 第一階段安全機制

- 只使用已收盤 1H K 線建立方向訊號。
- 同一市場快照產生固定 `signal_id`，不可重複開模擬倉。
- LANA 總分負責篩選標的；LONG/SHORT 分數負責決定方向。
- AI 只能確認規則方向或否決為 WATCH，不能反轉方向。
- Paper Trading 內建 0.5% 單筆風險、TP1 50%、TP2 30%、剩餘倉位移動止損及 24 小時時間止損。
- Paper Trading 永遠不會呼叫 OKX 私有交易 API。

## 本地測試

```bash
python -m unittest discover -s tests -v
```
