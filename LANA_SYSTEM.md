# LANA 系統完整說明文件
> 最後更新：2026-06-13
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
**Telegram**：Bot Token / Chat ID 存在 Railway Variables

---

## 二、評分系統（全系統統一標準）

### 多頭評分（LONG）
| 分項 | 滿分 | 條件 |
|------|------|------|
| 趨勢 | 25分 | MA7>MA25>MA99=25；MA7>MA25=15；MA7<MA25=0 |
| RSI | 20分 | 50-70=20；40-50=15；30-40=10；70-80=8；其他=0 |
| 量能 | 20分 | ≥2.5x=20；≥2.0x=18；≥1.5x=14；≥1.2x=10；≥1.0x=6；<1.0x=0 |
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

### K線回調扣分（多空評分共用）
| 條件 | 扣分 |
|------|------|
| 距近4根4H K線高點 < -5% 且持續下跌 | -30分 |
| 距高點 < -3% 且持續下跌 | -15分 |

**重要：量能統一定義 = 現量 / 過去20根均量（vol_ratio_1h）**
- meme_scanner（indicators.py: volume_ratio）與 lana-monitor 深度分析（app.py: vol_ratio）已統一此算法
- 舊算法（近7根均量/前7根均量）已棄用

---

## 三、推送條件（TG 推送門檻）

```
LONG 必須同時滿足：
1. 分數 >= 76（MIN_SCORE_TO_ALERT）
2. 趨勢：MA7 > MA25 > MA99（三條全排）
3. RSI < 72
4. 量能：vol_ratio_1h >= 1.0x（原0.8x已提高）
5. 24H漲幅 >= +1.5%
6. RSI < 75

SHORT 必須同時滿足：
1. 分數 >= 76
2. 空頭專屬分 >= 65 + MA7 < MA25 + RSI >= 68
   OR RSI >= 78 + 量能 >= 1.5x + 資金費率 > 0.0005
3. 量能：vol_ratio_1h >= 1.0x
4. 24H跌幅 >= -1.5%
5. RSI > 28
```

---

## 四、掃描排程架構

**架構**：Cron Job 觸發（解決雙 process 重複推送問題）
- `lana-cron` 服務：每 15 分鐘執行一次，POST `/api/trigger_scan`
- `tender-laughter`：`USE_CRON=true`，背景排程停用，只接受 trigger_scan 觸發
- 注意：`tender-laughter` 本身的 railway.json 也設了 numReplicas:1（無害但非必要）

---

## 五、去重機制

- meme_scanner.py 本輪去重：同一 symbol 只推一次
- 跨輪冷卻：同一幣 30 分鐘（2個掃描週期）內不重複推
- TG header 帶指紋：`#xxxxxxxx` 方便追蹤
- **注意**：歷史上重複推送問題反覆出現，若再發生需檢查：(1) tender-laughter是否仍顯示"Next in 15 minutes"代表自己也被當cron執行 (2) USE_CRON變數是否重複設定

---

## 六、API 資料來源

**全部改用 OKX**（Binance 被 Railway IP 451 地理封鎖）
- K 線：`https://www.okx.com/api/v5/market/candles`
- Ticker：`https://www.okx.com/api/v5/market/ticker`
- 資金費率：`https://www.okx.com/api/v5/public/funding-rate`
- 市場掃描：`https://www.okx.com/api/v5/market/tickers?instType=SWAP`
- Binance 只做備用（會 451 失敗）

**深度分析 K線**：`analyze_coin` 改用 1H K線（150根），原為1D K線（造成RSI天差地遠的bug已修）
- 7日/30日漲幅改用150根1H K線計算（30日為近似值，約6.25天）

---

## 七、Railway Variables（tender-laughter）

| 變數 | 值 | 說明 |
|------|----|------|
| MIN_SCORE_TO_ALERT | 76 | TG 推送分數門檻 |
| MIN_CHANGE_PCT | 3 | 最低漲跌幅篩選 |
| MIN_VOLUME_USDT | 500000 | 最低成交量 |
| MAX_COINS_TO_SCAN | 25 | 最多掃描幣數（原50，因單輪耗時超過15分排程週期而下調）|
| SCAN_INTERVAL_MIN | 15 | 排程間隔（備用） |
| PORT | 8080 | Flask port |
| USE_CRON | true | 停用背景排程 |
| ANTHROPIC_API_KEY | ****** | Claude API |
| TELEGRAM_BOT_TOKEN/CHAT_ID | ****** | TG |

---

## 八、深度分析 AI 邏輯（重要）

**提示詞包含 K 線趨勢資料**（防止下跌中還建議做多）：
- 計算近4根4H K線的高點、現價距高點%、K線方向（上升/下跌/震盪）
- 規則：距高點<-5%且下跌中 → 不應建議LONG，優先SHORT或WATCH
- 此規則同時應用於 AI 版本與備用規則式版本（兩者邏輯已統一）

**深度分析與TG推送分歧的歷史教訓**：
- 第一次分歧（HOME幣）：根因是深度分析用1D K線、TG推送用1H K線，RSI差很多 → 已修正為都用1H
- 第二次分歧（ICP幣）：根因是vol_ratio算法不同（7根均量比 vs 現量/20根均量）→ 已統一算法
- 若未來再出現分數差異很大，優先檢查：兩邊抓的K線週期是否一致、量能算法是否一致

---

## 九、網頁功能 Tab 說明

| Tab | 功能 | 資料來源 |
|-----|------|---------|
| 市場掃描 | OKX/Bybit 期貨訊號 | lana-monitor 自帶 |
| 深度分析 | 手動輸入幣名分析 | OKX 1H K線 + Claude AI |
| 觀察名單 | 自訂追蹤清單 | 本地儲存 |
| 土狗 | Meme幣掃描結果 | tender-laughter /api/meme_signals |
| 社群 | 鏈上新幣情緒 | lana-social-scanner |
| 日誌 | 操作記錄 | 本地 |
| 回測 | 歷史回測 | OKX K線 |

---

## 十、已知問題與歷史 Bug（時間序）

| 問題 | 根本原因 | 修法 |
|------|---------|------|
| 量能排除一直失效 | ind.get("vol_ratio") 應為 vol_ratio_1h | 改讀 vol_ratio_1h |
| TG 每個幣推兩次 | Railway 部署新舊 process 重疊 | 改用 Cron Job 架構 |
| 深度分析 451 錯誤 | Binance 地理封鎖 Railway IP | 全改 OKX API |
| 土狗頁面無法連線 | 前端寫死舊 domain | 改同源 /api/meme_signals |
| 社群幣顯示 0 分 | 前端讀 risk.score，AI 輸出 risk_score | 加對齊 key |
| 做空從不出現 | 篩選邏輯偏多頭，無空頭專屬評分 | 加 _calc_short_score |
| HOME幣深度分析持續建議做多即使大跌 | 1.AI無K線歷史 2.備用規則無K線判斷 | 加K線趨勢判斷+回調扣分(雙邊) |
| TG推送76分但量能僅0.8x仍推送 | 量能門檻0.8x太寬鬆 | 提高至1.0x |
| ICP幣TG=89分但深度分析=43分 | vol_ratio算法不同(7根均量比 vs 現量/20均量) | 統一為現量/20根均量 |
| 土狗tab永遠"掃描中"+同訊號重複推送 | /api/trigger_scan無重疊保護，單輪掃描(50幣×3種K線)可能超過15分鐘，多個run_scan並行寫入_cache互相干擾且各自推送 | 加_scan_running鎖+K線數量精簡+MAX_COINS 50→25 |

---

## 十一、新對話開始時給 Claude 的指令

```
請讀取 GitHub nicepolo/lana-monitor repo 的 LANA_SYSTEM.md，
了解 LANA 系統架構後繼續協助我優化。
```

