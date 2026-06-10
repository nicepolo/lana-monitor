# LANA 系統完整說明文件
> 最後更新：2026-06-09
> 維護人：Polo（台中）

---

## 一、系統架構

| 服務名稱 | GitHub Repo | Railway 服務 | Railway Project | 功能 |
|---------|------------|-------------|----------------|------|
| LANA Monitor（網頁） | nicepolo/lana-monitor | web-production-7cdf9.up.railway.app | be4b45 | 主網頁儀表板 |
| LANA Meme Scanner（掃描+推送） | nicepolo/lana-meme-scanner | tender-laughter-production-5045.up.railway.app | be4b45 | 掃描 OKX 期貨 + TG 推送 |
| LANA Cron Job（觸發排程） | nicepolo/lana-cron | be4b45 project | be4b45 | 每15分鐘 POST /api/trigger_scan |
| LANA Social Scanner（社群） | nicepolo/lana-social-scanner | web-production-3303b.up.railway.app | c5806d | GeckoTerminal 鏈上新幣 |

**GitHub Token**：存在 Railway Variables（不寫在這裡）

**Telegram**：
- Bot Token：存在 Railway Variables（TELEGRAM_BOT_TOKEN）
- Chat ID：存在 Railway Variables（TELEGRAM_CHAT_ID）

---

## 二、評分系統（全系統統一標準）

### 多頭評分（LONG）
| 分項 | 滿分 | 條件 |
|------|------|------|
| 趨勢 | 25分 | MA7>MA25>MA99=25；MA7>MA25=15；MA7<MA25=0 |
| RSI | 20分 | 50-70=20；40-50=15；30-40=10；70-80=8；其他=0 |
| 量能 | 20分 | ≥2.5x=20；≥2.0x=18；≥1.5x=14；≥1.2x=10；≥0.8x=4；<0.8x=0 |
| BB位置 | 15分 | <0.3=15；<0.5=12；≤0.7=8；>0.7=4 |
| 資金費率 | 20分 | 極中性=20；偏中性=15；負費率=18；極高=2 |

### 空頭評分（SHORT，專屬）
| 分項 | 滿分 | 條件 |
|------|------|------|
| 趨勢空頭 | 25分 | MA7<MA25<MA99=25；MA7<MA25=15 |
| RSI超買 | 25分 | ≥80=25；≥75=20；≥70=12 |
| 量能 | 20分 | ≥2.0x=20；≥1.5x=15；≥1.0x=8 |
| BB突破上軌 | 15分 | >1.0=15；>0.8=12；>0.7=6 |
| 資金費率偏高 | 15分 | >0.001=15；>0.0005=8 |

**重要**：量能讀取用 `vol_ratio_1h`（不是 `vol_ratio`）

---

## 三、推送條件（TG 推送門檻）

```
LONG 必須同時滿足：
1. 分數 >= 76（MIN_SCORE_TO_ALERT）
2. 趨勢：MA7 > MA25 > MA99（三條全排）
3. RSI < 72
4. 量能：vol_ratio_1h >= 0.8x
5. 24H漲幅 >= +1.5%
6. RSI < 75

SHORT 必須同時滿足：
1. 分數 >= 76
2. 空頭專屬分 >= 65 + MA7 < MA25 + RSI >= 68
   OR RSI >= 78 + 量能 >= 1.5x + 資金費率 > 0.0005
3. 量能：vol_ratio_1h >= 0.8x
4. 24H跌幅 >= -1.5%
5. RSI > 28（避免超賣追空）
```

---

## 四、掃描排程架構

**架構**：Cron Job 觸發（解決雙 process 重複推送問題）

- `lana-cron` 服務：每 15 分鐘執行一次，POST `/api/trigger_scan`
- `tender-laughter`：`USE_CRON=true`，背景排程停用，只接受 trigger_scan 觸發
- 台北時間 03:00-07:00 靜默期：由 Cron Job 控制（可調整 cron schedule）

**重複推送問題**：已用 Cron Job 架構解決，lana-cron 只有一個 instance

---

## 五、去重機制

- meme_scanner.py 本輪去重：同一 symbol 只推一次
- 跨輪冷卻：同一幣 30 分鐘（2個掃描週期）內不重複推
- TG header 帶指紋：`#xxxxxxxx` 方便追蹤

---

## 六、API 資料來源

**全部改用 OKX**（Binance 被 Railway IP 451 地理封鎖）

- K 線：`https://www.okx.com/api/v5/market/candles`
- Ticker：`https://www.okx.com/api/v5/market/ticker`
- 資金費率：`https://www.okx.com/api/v5/public/funding-rate`
- 市場掃描：`https://www.okx.com/api/v5/market/tickers?instType=SWAP`
- Binance 只做備用（會 451 失敗，Railway IP 被封）

---

## 七、Railway Variables（tender-laughter）

| 變數 | 值 | 說明 |
|------|----|------|
| MIN_SCORE_TO_ALERT | 76 | TG 推送分數門檻 |
| MIN_CHANGE_PCT | 3 | 最低漲跌幅篩選 |
| MIN_VOLUME_USDT | 500000 | 最低成交量 |
| MAX_COINS_TO_SCAN | 50 | 最多掃描幣數 |
| SCAN_INTERVAL_MIN | 15 | 排程間隔（備用） |
| PORT | 8080 | Flask port（private network 固定用） |
| USE_CRON | true | 停用背景排程，改用 Cron Job 觸發 |
| ANTHROPIC_API_KEY | ****** | Claude API |
| GEMINI_API_KEY | ****** | Gemini API（備用） |
| TELEGRAM_BOT_TOKEN | ****** | TG Bot |
| TELEGRAM_CHAT_ID | ****** | TG Chat |

---

## 八、網頁架構（lana-monitor/app.py）

**主要路由：**
- `/` → index.html（主頁面）
- `/api/meme_signals` → Proxy 到 tender-laughter:8080（土狗 tab）
- `/api/ai_analyze` → 深度分析 AI（Claude Haiku，OKX K線）
- `/health` → 健康檢查

**Private Network**：
- lana-monitor → tender-laughter 透過 `http://tender-laughter:8080`
- 兩個服務都在 Railway project be4b45

---

## 九、AI 分析費用策略

- **掃描推送（自動）**：純規則式，零 API 費用
- **深度分析（手動點）**：Claude Haiku `claude-haiku-4-5-20251001`，才消耗 token
- Gemini 已停用（JSON 截斷問題太多）

---

## 十、網頁功能 Tab 說明

| Tab | 功能 | 資料來源 |
|-----|------|---------|
| 市場掃描 | OKX/Bybit 期貨訊號 | lana-monitor 自帶 |
| 深度分析 | 手動輸入幣名分析 | OKX K線 + Claude AI |
| 觀察名單 | 自訂追蹤清單 | 本地儲存 |
| 土狗 | Meme幣掃描結果 | tender-laughter /api/meme_signals |
| 社群 | 鏈上新幣情緒 | lana-social-scanner（web-production-3303b） |
| 日誌 | 操作記錄 | 本地 |
| 回測 | 歷史回測 | OKX K線 |

---

## 十一、已知問題與歷史 Bug

| 問題 | 根本原因 | 修法 |
|------|---------|------|
| 量能排除一直失效 | ind.get("vol_ratio") 應為 vol_ratio_1h | 改讀 vol_ratio_1h |
| TG 每個幣推兩次 | Railway 部署新舊 process 重疊各推一次 | 改用 Cron Job 架構 |
| 深度分析 451 錯誤 | Binance 地理封鎖 Railway IP | 全改 OKX API |
| Gemini JSON 截斷 | maxOutputTokens 太低 + AI 費用高 | 改純規則式 |
| 土狗頁面無法連線 | 前端寫死舊 domain | MEME_API 改同源 /api/meme_signals |
| 分數不統一 | 兩套邏輯 | 統一 MA+RSI+量能+BB+FR |
| 社群幣顯示 0 分 | 前端讀 risk.score，AI 輸出 risk_score | 加對齊 key |
| 做空從不出現 | 篩選邏輯偏多頭，無空頭專屬評分 | 加 _calc_short_score |

---

## 十二、新對話開始時給 Claude 的指令

```
請讀取 GitHub nicepolo/lana-monitor repo 的 LANA_SYSTEM.md，
了解 LANA 系統架構後繼續協助我優化。
```

