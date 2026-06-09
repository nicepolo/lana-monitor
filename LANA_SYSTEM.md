# LANA 系統完整說明文件
> 最後更新：2026-06-09
> 維護人：Polo（台中）

---

## 一、系統架構

| 服務名稱 | GitHub Repo | Railway 服務 | Railway Project | 功能 |
|---------|------------|-------------|----------------|------|
| LANA Monitor（網頁） | nicepolo/lana-monitor | web-production-7cdf9.up.railway.app | be4b45 | 主網頁儀表板 |
| LANA Meme Scanner（掃描+推送） | nicepolo/lana-meme-scanner | tender-laughter-production-5045.up.railway.app | be4b45 | 掃描 OKX 期貨 + TG 推送 |
| LANA Social Scanner（社群） | nicepolo/lana-social-scanner | web-production-950ec.up.railway.app | c5806d | GeckoTerminal 鏈上新幣 |

**GitHub Token**：`ghp_XXXXXXXX（存在 Railway Variables）`

**Telegram**：
- Bot Token：`BOT_TOKEN（存在 Railway Variables）`
- Chat ID：`CHAT_ID（存在 Railway Variables）`

---

## 二、評分系統（全系統統一標準）

| 分項 | 滿分 | 條件 |
|------|------|------|
| 趨勢 | 25分 | MA7>MA25>MA99=25分；MA7>MA25=15分；MA7<MA25=0分 |
| RSI | 20分 | 50-70=20分；40-50=15分；30-40=10分；70-80=8分；其他=0 |
| 量能 | 20分 | ≥2.5x=20；≥2.0x=18；≥1.5x=14；≥1.2x=10；≥0.8x=4；<0.8x=0 |
| BB位置 | 15分 | <0.3=15；<0.5=12；≤0.7=8；>0.7=4 |
| 資金費率 | 20分 | 極中性=20；偏中性=15；負費率=18；極高=2 |

**重要**：量能讀取用 `vol_ratio_1h`（不是 `vol_ratio`）

---

## 三、推送條件（TG 推送門檻）

```
必須同時滿足：
1. 分數 >= 76（Railway Variable: MIN_SCORE_TO_ALERT=76）
2. 方向 = LONG 或 SHORT（WATCH 不推）
3. 趨勢：MA7 > MA25 > MA99（三條全排）
4. 量能：vol_ratio_1h >= 0.8x
5. 做多：24H漲幅 >= +1.5%，RSI < 75
6. 做空：24H跌幅 >= -1.5%，RSI > 28
```

---

## 四、掃描排程

- **台北時間 03:00-07:00**：每 120 分鐘掃描一次（省資源）
- **其他時段**：每 15 分鐘掃描一次
- 啟動後等到下一個 :00/:15/:30/:45 才跑第一次（避免重部署連續觸發）

---

## 五、去重機制

- **檔案鎖**：`/tmp/lana_last_push.json`，10 分鐘內相同指紋不重複推
- **本輪去重**：同一輪 symbol 只推一次
- **跨輪冷卻**：同一幣 30 分鐘內不重複推

---

## 六、方向判斷邏輯

```python
trend_full = MA7 > MA25 > MA99  # 三線對齊
trend_mild = MA7 > MA25

if score >= 70 and trend_full and RSI < 72:
    direction = "LONG"
elif RSI > 75 or (FR > 0.001 and not trend_mild):
    direction = "SHORT"
else:
    direction = "WATCH"
```

---

## 七、API 資料來源

**全部改用 OKX**（Binance 被 Railway IP 451 地理封鎖）

- K 線：`https://www.okx.com/api/v5/market/candles`
- Ticker：`https://www.okx.com/api/v5/market/ticker`
- 資金費率：`https://www.okx.com/api/v5/public/funding-rate`
- 市場掃描：`https://www.okx.com/api/v5/market/tickers?instType=SWAP`
- Binance 只做備用（會 451 失敗）

---

## 八、Railway Variables（tender-laughter）

| 變數 | 值 |
|------|----|
| MIN_SCORE_TO_ALERT | 76 |
| MIN_CHANGE_PCT | 3 |
| MIN_VOLUME_USDT | 500000 |
| MAX_COINS_TO_SCAN | 50 |
| SCAN_INTERVAL_MIN | 15 |
| PORT | 8080 |
| ANTHROPIC_API_KEY | ******** |
| GEMINI_API_KEY | ******** |
| TELEGRAM_BOT_TOKEN | ******** |
| TELEGRAM_CHAT_ID | ******** |

---

## 九、網頁架構（lana-monitor/app.py）

**主要路由：**
- `/` → index.html（主頁面）
- `/api/meme_signals` → Proxy 到 tender-laughter:8080（土狗 tab）
- `/api/ai_analyze` → 深度分析 AI（Claude Haiku，OKX K線）
- `/health` → 健康檢查

**Private Network**：
- lana-monitor → tender-laughter 透過 `http://tender-laughter:8080`
- 兩個服務都在 Railway project be4b45，可用 private network 互通

---

## 十、已知問題與歷史 Bug

| 問題 | 根本原因 | 修法 |
|------|---------|------|
| 量能排除一直失效 | `ind.get("vol_ratio")` 應為 `vol_ratio_1h` | 改為讀 vol_ratio_1h |
| TG 每個幣推兩次 | Railway 部署時新舊 process 重疊 | 檔案鎖 /tmp/lana_last_push.json |
| 深度分析 451 錯誤 | Binance 地理封鎖 Railway IP | 全改 OKX API |
| Gemini JSON 截斷 | maxOutputTokens 太低 | 改為純規則式，零 API 費用 |
| 土狗頁面無法連線 | 前端寫死舊 domain | MEME_API 改為同源 /api/meme_signals |
| 分數不統一 | meme-scanner 和 lana-monitor 兩套邏輯 | 統一用 MA+RSI+量能+BB+FR |

---

## 十一、AI 分析費用策略

- **掃描推送（自動）**：純規則式，零 API 費用
- **深度分析（手動點）**：Claude Haiku，才消耗 token
- Claude API 用 `claude-haiku-4-5-20251001` 模型

---

## 十二、網頁功能 Tab 說明

| Tab | 功能 | 資料來源 |
|-----|------|---------|
| 市場掃描 | OKX/Bybit 期貨訊號 | lana-monitor 自帶 |
| 深度分析 | 手動輸入幣名分析 | OKX K線 + Claude AI |
| 觀察名單 | 自訂追蹤清單 | 本地儲存 |
| 土狗 | Meme幣掃描結果 | tender-laughter API |
| 社群 | 鏈上新幣情緒 | lana-social-scanner |
| 日誌 | 操作記錄 | 本地 |
| 回測 | 歷史回測 | OKX K線 |

---

## 十三、新對話開始時給 Claude 的指令

```
請讀取 GitHub nicepolo/lana-monitor repo 的 LANA_SYSTEM.md，
了解 LANA 系統架構後繼續協助我優化。
GitHub Token: ghp_XXXXXXXX（存在 Railway Variables）
```

