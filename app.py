"""
LANA 妖幣監測 Web App - Flask 後端
部署於 Railway
"""

from flask import Flask, jsonify, render_template, request
import requests
import math
import os
import json
import uuid
from datetime import datetime, timezone, timedelta
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── Meme Signals Cache ──
_meme_cache = {"results": [], "ts": ""}
_meme_cache_time = 0

app = Flask(__name__)

import logging, time
log = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# AI 分析冷卻快取（伺服器記憶體，跨請求持續存在；cron 端每次都是全新容器，無法自行記憶冷卻）
_ai_analyze_cache = {}   # {coin: {"ts": float, "result": dict}}
AI_ANALYZE_COOLDOWN_SEC = 4 * 3600

# 推送控制狀態（伺服器端持久，跨 cron 容器）
_push_control = {
    "paused": False,       # 手動暫停
    "pause_until": None,   # 暫停到幾點（float timestamp），None = 永久暫停直到 /resume
}

# ── 設定（Railway 環境變數優先，fallback 到預設值）──────────────
NOTIFY_TO        = os.environ.get("NOTIFY_TO",        "nicepolo1222@gmail.com")
WEB_BASE_URL = os.environ.get("WEB_BASE_URL", "https://web-production-7cdf9.up.railway.app")

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN",   "8477541527:AAGK7ZEdgXpIJcWtwWEYrQJXfG6OtCf9HaE")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "405822104")
PORT             = int(os.environ.get("PORT", 5000))

# ── 存檔路徑（Railway Volume 掛載在 /data/）──────────────────────
WATCHLIST_FILE  = '/data/watchlist.json'
JOURNAL_FILE    = '/data/trade_journal.json'
RISK_DATA_FILE   = '/data/risk_data.json'
BACKTEST_FILE    = '/data/backtest_data.json'

SECTORS = {
    'DOGE':'迷因','SHIB':'迷因','PEPE':'迷因','WIF':'迷因','BONK':'迷因',
    'FLOKI':'迷因','MOG':'迷因','TRUMP':'迷因','GIGGLE':'迷因',
    'FET':'AI','AGIX':'AI','RNDR':'AI','TAO':'AI','WLD':'AI','AKT':'AI','SKYAI':'AI',
    'BIO':'DeSci','GRT':'DeSci','VITA':'DeSci','RIF':'DeSci',
    'SOL':'L1','AVAX':'L1','APT':'L1','SUI':'L1','SEI':'L1',
    'ARB':'L2','OP':'L2','MATIC':'L2','STRK':'L2','ZK':'L2',
    'ONDO':'RWA','OM':'RWA','LINK':'RWA','MAKER':'RWA',
    'ORDI':'BRC20','SATS':'BRC20',
}
def get_sector(coin): return SECTORS.get(coin.upper(), '其他')

RISK_DEFAULT = {
    "settings": {
        "capital": 10000,
        "weekly_loss_pct": 10,
        "daily_loss_pct": 5,
        "consecutive_loss_limit": 5
    },
    "force_unlock_until": None,
    "trades": []
}
WL_DEFAULT     = ["ORDI", "BIO", "ORCA", "PENGU"]

def load_watchlist():
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, 'r') as f:
                data = json.load(f)
                return data.get('coins', [])
    except Exception:
        pass
    # 本地開發 fallback
    try:
        local_path = os.path.join(os.path.dirname(__file__), 'watchlist.json')
        if os.path.exists(local_path):
            with open(local_path, 'r') as f:
                return json.load(f).get('coins', [])
    except Exception:
        pass
    return list(WL_DEFAULT)

def save_watchlist(coins):
    # 優先寫 /data/（Railway Volume），失敗就寫本地
    for path in [WATCHLIST_FILE, os.path.join(os.path.dirname(__file__), 'watchlist.json')]:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                json.dump({'coins': coins, 'updated_at': datetime.utcnow().isoformat() + 'Z'}, f)
            return
        except Exception:
            continue

# ── 交易日誌 讀寫 ────────────────────────────────────────────
def load_journal():
    for path in [JOURNAL_FILE, os.path.join(os.path.dirname(__file__), 'trade_journal.json')]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
    return []

def save_journal(entries):
    for path in [JOURNAL_FILE, os.path.join(os.path.dirname(__file__), 'trade_journal.json')]:
        try:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(entries, f, ensure_ascii=False, indent=2)
            return
        except Exception:
            continue

# ── 回測 讀寫 + 統計 ──────────────────────────────────────────
def load_backtest():
    for path in [BACKTEST_FILE, os.path.join(os.path.dirname(__file__), 'backtest_data.json')]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception:
            pass
    return {'snapshots': {}, 'results': {}}

def save_backtest(data):
    for path in [BACKTEST_FILE, os.path.join(os.path.dirname(__file__), 'backtest_data.json')]:
        try:
            dn = os.path.dirname(path)
            if dn: os.makedirs(dn, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return
        except Exception:
            continue

def _analyze_safe(coin):
    try: return analyze_coin(coin)
    except: return None

def compute_backtest_stats(data):
    results = data.get('results', {})
    all_e   = [e for day in results.values() for e in day if e.get('pnl_pct') is not None]
    if not all_e:
        return {'total_samples': 0, 'days': len(results), 'ranges': [], 'sectors': [], 'recent': []}

    rngs = [('80+', 80, 101), ('60-79', 60, 80), ('40-59', 40, 60), ('<40', 0, 40)]
    range_stats = []
    for label, lo, hi in rngs:
        bkt = [e for e in all_e if lo <= (e.get('lana_score') or 0) < hi]
        if not bkt:
            range_stats.append({'label': label, 'count': 0, 'avg_pnl': None, 'win_rate': None})
            continue
        avg  = round(sum(e['pnl_pct'] for e in bkt) / len(bkt), 2)
        wins = sum(1 for e in bkt if e['pnl_pct'] > 0)
        range_stats.append({'label': label, 'count': len(bkt), 'avg_pnl': avg,
                            'win_rate': round(wins / len(bkt) * 100, 1)})

    sec_map = {}
    for e in all_e:
        s = e.get('sector', '其他')
        sec_map.setdefault(s, []).append(e['pnl_pct'])
    sec_stats = sorted(
        [{'sector': s, 'avg_pnl': round(sum(v)/len(v), 2), 'count': len(v), 'win_rate': round(sum(1 for x in v if x>0)/len(v)*100,1)}
         for s, v in sec_map.items() if len(v) >= 2],
        key=lambda x: x['avg_pnl'], reverse=True
    )

    recent = []
    for d in sorted(results.keys(), reverse=True)[:5]:
        valid = [e for e in results[d] if e.get('pnl_pct') is not None]
        if valid:
            valid_s = sorted(valid, key=lambda x: x['pnl_pct'], reverse=True)
            recent.append({'date': d, 'count': len(valid),
                           'avg_pnl': round(sum(e['pnl_pct'] for e in valid)/len(valid), 2),
                           'best':  valid_s[0], 'worst': valid_s[-1]})

    return {'total_samples': len(all_e), 'days': len(results),
            'ranges': range_stats, 'sectors': sec_stats, 'recent': recent}

# ── 風控 讀寫 + 計算 ──────────────────────────────────────────
def load_risk_data():
    for path in [RISK_DATA_FILE, os.path.join(os.path.dirname(__file__), 'risk_data.json')]:
        try:
            if os.path.exists(path):
                with open(path, 'r', encoding='utf-8') as f:
                    d = json.load(f)
                    d.setdefault('settings', dict(RISK_DEFAULT['settings']))
                    d.setdefault('trades', [])
                    return d
        except Exception:
            pass
    return {'settings': dict(RISK_DEFAULT['settings']), 'force_unlock_until': None, 'trades': []}

def save_risk_data(data):
    for path in [RISK_DATA_FILE, os.path.join(os.path.dirname(__file__), 'risk_data.json')]:
        try:
            dn = os.path.dirname(path)
            if dn: os.makedirs(dn, exist_ok=True)
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            return
        except Exception:
            continue

def compute_risk_status(data):
    now    = datetime.now(timezone.utc)
    s      = data.get('settings', RISK_DEFAULT['settings'])
    trades = data.get('trades', [])

    capital      = float(s.get('capital', 10000))
    wk_pct       = float(s.get('weekly_loss_pct', 10))
    dy_pct       = float(s.get('daily_loss_pct', 5))
    con_lim      = int(s.get('consecutive_loss_limit', 5))
    weekly_limit = -(wk_pct / 100 * capital)
    daily_limit  = -(dy_pct  / 100 * capital)

    real = [t for t in trades if t.get('coin') != 'SYSTEM']
    wd   = now.weekday()
    wk_start = (now - timedelta(days=wd)).replace(hour=0, minute=0, second=0, microsecond=0)
    dy_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    wk_trades = [t for t in real if _parse_ts(t.get('ts','')) >= wk_start]
    dy_trades = [t for t in real if _parse_ts(t.get('ts','')) >= dy_start]
    weekly_pnl = sum(t.get('pnl_usd', 0) for t in wk_trades)
    daily_pnl  = sum(t.get('pnl_usd', 0) for t in dy_trades)

    consecutive = 0
    for t in real:
        if (t.get('pnl_usd') or 0) < 0: consecutive += 1
        else: break

    # 強制解鎖檢查
    fu = data.get('force_unlock_until')
    if fu:
        try:
            fu_ts = _parse_ts(fu)
            if now < fu_ts:
                return dict(locked=False, force_unlocked=True,
                            fu_hours_left=round((fu_ts - now).total_seconds()/3600, 1),
                            weekly_pnl=weekly_pnl, weekly_limit=weekly_limit,
                            daily_pnl=daily_pnl, daily_limit=daily_limit,
                            consecutive=consecutive, con_lim=con_lim, capital=capital)
        except: pass

    def _lock(reason, until_ts):
        hl = max(0, (until_ts - now).total_seconds() / 3600)
        return dict(locked=True, reason=reason,
                    lock_until=until_ts.isoformat(), hours_left=round(hl, 1),
                    weekly_pnl=weekly_pnl, weekly_limit=weekly_limit,
                    daily_pnl=daily_pnl, daily_limit=daily_limit,
                    consecutive=consecutive, con_lim=con_lim, capital=capital)

    # 規則 A：連續止損
    if consecutive >= con_lim and real:
        lf = _parse_ts(real[0].get('ts', now.isoformat()))
        lu = lf + timedelta(hours=24)
        if now < lu: return _lock(f'連續 {consecutive} 筆止損', lu)

    # 規則 B：週虧損
    if weekly_pnl <= weekly_limit:
        nm = wk_start + timedelta(days=7)
        if now < nm: return _lock(f'本週虧損 ${abs(weekly_pnl):.0f}，超過上限 ${abs(weekly_limit):.0f}', nm)

    # 規則 C：日虧損
    if daily_pnl <= daily_limit:
        tm = dy_start + timedelta(days=1)
        if now < tm: return _lock(f'今日虧損 ${abs(daily_pnl):.0f}，超過上限 ${abs(daily_limit):.0f}', tm)

    warning = (weekly_pnl < weekly_limit * 0.5 or
               daily_pnl  < daily_limit  * 0.5 or
               consecutive >= con_lim - 2)

    return dict(locked=False, warning=warning,
                weekly_pnl=weekly_pnl, weekly_limit=weekly_limit,
                daily_pnl=daily_pnl, daily_limit=daily_limit,
                consecutive=consecutive, con_lim=con_lim, capital=capital)

BINANCE_BASE  = "https://data-api.binance.vision"   # CDN endpoint, bypasses US geo-block
BINANCE_ALT   = "https://api.binance.com"           # fallback
FUTURES_BASE  = "https://fapi.binance.com"

# ── 技術指標計算 ──────────────────────────────────────────────
def ma(closes, n):
    return round(sum(closes[-n:]) / n, 6) if len(closes) >= n else None

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return None
    diffs = [closes[i] - closes[i-1] for i in range(-n, 0)]
    gains  = sum(d for d in diffs if d > 0) / n
    losses = sum(-d for d in diffs if d < 0) / n
    if losses == 0:
        return 100.0
    return round(100 - (100 / (1 + gains / losses)), 2)

def bollinger(closes, n=20, k=2):
    if len(closes) < n:
        return None, None, None
    sl  = closes[-n:]
    mid = sum(sl) / n
    std = math.sqrt(sum((x - mid)**2 for x in sl) / n)
    return round(mid + k*std, 6), round(mid, 6), round(mid - k*std, 6)

def vol_ratio(volumes):
    """現量 / 過去20根均量（與 meme_scanner 定義一致）"""
    if len(volumes) < 21:
        return None
    avg = sum(volumes[-21:-1]) / 20
    return round(volumes[-1] / avg, 2) if avg else None

def calc_lana_score(ma7, ma30, ma120, rsi_val, vr, bb_pos, risks, contract=None,
                    price=None, high20=None, change_24h=None):
    """LANA Score 0-100 v3 動能優先"""
    # 動能 30分（多空對等：用漲跌幅絕對值，跌幅夠深一樣給高分）
    s_momentum = 0
    if change_24h is not None:
        abs_chg = abs(change_24h)
        if abs_chg >= 20:    s_momentum = 30
        elif abs_chg >= 15:  s_momentum = 25
        elif abs_chg >= 10:  s_momentum = 20
        elif abs_chg >= 5:   s_momentum = 14
        elif abs_chg >= 2:   s_momentum = 8
        else:                s_momentum = 3
    # 趨勢 20分（多空對等：空頭排列同樣視為強趨勢）
    if ma7 and ma30 and ma120 and (ma7 > ma30 > ma120 or ma7 < ma30 < ma120):
        s_trend = 20
    elif ma7 and ma30 and abs(ma7 - ma30) > ma30 * 0.03:
        s_trend = 14
    elif ma7 and ma30 and abs(ma7 - ma30) > ma30 * 0.005:
        s_trend = 8
    else:
        s_trend = 2
    # 量能 20分
    if vr is None:   s_vol = 8
    elif vr >= 2.5:  s_vol = 20
    elif vr >= 2.0:  s_vol = 16
    elif vr >= 1.5:  s_vol = 12
    elif vr >= 1.0:  s_vol = 6
    else:            s_vol = 0
    # RSI 15分
    if rsi_val is None:          s_rsi = 7
    elif 50 <= rsi_val < 70:     s_rsi = 15
    elif 40 <= rsi_val < 50:     s_rsi = 10
    elif 70 <= rsi_val < 80:     s_rsi = 6
    elif 30 <= rsi_val < 40:     s_rsi = 8
    else:                        s_rsi = 0
    # BB 10分
    s_bb = {'lower_half': 10, 'below_lower': 8, 'upper_half': 6, 'above_upper': 2}.get(bb_pos, 5)
    # 風險 5分
    s_risk = [5, 4, 2, 1, 0][min(len(risks), 4)]
    base = s_momentum + s_trend + s_vol + s_rsi + s_bb + s_risk

    # 合約端訊號（最多 +30 / -25）
    s_contract = 0
    early_ambush = False
    if contract:
        oi_chg    = contract.get('oi_change_24h')
        price_chg = abs(contract.get('price_change_24h') or 0)
        funding   = contract.get('funding')
        ls        = contract.get('ls_ratio')
        if oi_chg is not None and oi_chg >= 20 and price_chg < 5:
            s_contract += 15; early_ambush = True
        if funding is not None:
            if funding < -0.0005:   s_contract += 10
            elif funding > 0.001:   s_contract -= 15
        if ls is not None:
            if ls < 1.0:            s_contract += 10
            elif ls > 3.0:          s_contract -= 10

    raw = base + s_contract
    # 正規化：有合約資料時分母 130，無合約時分母 100（避免無謂打折）
    denom = 130 if s_contract != 0 else 100
    total = max(0, min(100, round(raw / denom * 100)))
    return {
        "total": total, "raw": raw,
        "trend": s_trend, "rsi": s_rsi, "vol": s_vol,
        "bb": s_bb, "risk": s_risk, "contract": s_contract,
        "early_ambush": early_ambush,
        "momentum": s_momentum,
    }

def _get(url, params=None, timeout=15):
    """帶 fallback 的 GET，先試 CDN endpoint，失敗再試原始 endpoint"""
    try:
        r = requests.get(url, params=params, timeout=timeout)
        r.raise_for_status()
        return r
    except Exception:
        # fallback: replace CDN base with original
        alt_url = url.replace(BINANCE_BASE, BINANCE_ALT)
        r = requests.get(alt_url, params=params, timeout=timeout)
        r.raise_for_status()
        return r

def _fetch_klines_okx(symbol, interval="1D", limit=150):
    """OKX K線（Railway 不封鎖）"""
    interval_map = {"1d": "1D", "1h": "1H", "4h": "4H", "15m": "15m",
                    "1D": "1D", "1H": "1H", "4H": "4H"}
    okx_bar = interval_map.get(interval, "1D")
    sym = symbol.replace("USDT", "")
    try:
        import requests as _req
        for inst_id in [f"{sym}-USDT-SWAP", f"{sym}-USDT"]:
            r = _req.get(
                "https://www.okx.com/api/v5/market/candles",
                params={"instId": inst_id, "bar": okx_bar, "limit": limit},
                timeout=10
            )
            data = r.json().get("data", [])
            if data:
                return [{"o": float(k[1]), "h": float(k[2]),
                         "l": float(k[3]), "c": float(k[4]), "v": float(k[5])}
                        for k in reversed(data)]
    except Exception as e:
        log.warning(f"OKX klines 失敗 {symbol}: {e}")
    return []


def fetch_klines(symbol, interval="1d", limit=150):
    """OKX 優先，Binance 備用"""
    data = _fetch_klines_okx(symbol, interval, limit)
    if data:
        return data
    try:
        r = _get(f"{BINANCE_BASE}/api/v3/klines",
                 params={"symbol": symbol, "interval": interval, "limit": limit})
        return [{"o": float(d[1]), "h": float(d[2]),
                 "l": float(d[3]), "c": float(d[4]), "v": float(d[5])}
                for d in r.json()]
    except:
        return []

def fetch_ticker(symbol):
    """OKX 優先抓 24hr ticker"""
    sym = symbol.replace("USDT", "")
    try:
        import requests as _req
        for inst_id in [f"{sym}-USDT-SWAP", f"{sym}-USDT"]:
            r = _req.get(
                "https://www.okx.com/api/v5/market/ticker",
                params={"instId": inst_id}, timeout=10
            )
            d = r.json().get("data", [])
            if d:
                t = d[0]
                last = float(t.get("last", 0))
                open24 = float(t.get("open24h", last))
                chg_pct = ((last - open24) / open24 * 100) if open24 else 0
                return {
                    "lastPrice": str(last),
                    "priceChangePercent": str(round(chg_pct, 2)),
                    "quoteVolume": str(float(t.get("volCcy24h", t.get("vol24h", 0)))),
                    "highPrice": str(t.get("high24h", last)),
                    "lowPrice": str(t.get("low24h", last)),
                }
    except Exception as e:
        log.warning(f"OKX ticker 失敗 {symbol}: {e}")
    # Binance 備用
    try:
        r = _get(f"{BINANCE_BASE}/api/v3/ticker/24hr", params={"symbol": symbol})
        return r.json()
    except:
        return {}

def fetch_funding(symbol):
    try:
        r = requests.get(f"{FUTURES_BASE}/fapi/v1/premiumIndex",
                         params={"symbol": symbol}, timeout=8)
        return float(r.json().get("lastFundingRate", 0))
    except:
        return None

def fetch_global_ls(symbol):
    try:
        r = requests.get(f"{FUTURES_BASE}/futures/data/globalLongShortAccountRatio",
                         params={"symbol": symbol, "period": "1h", "limit": 1}, timeout=8)
        d = r.json()
        return float(d[0]["longShortRatio"]) if d else None
    except:
        return None

def fetch_top_trader_ls(symbol):
    try:
        r = requests.get(f"{FUTURES_BASE}/futures/data/topLongShortAccountRatio",
                         params={"symbol": symbol, "period": "1h", "limit": 1}, timeout=8)
        d = r.json()
        return float(d[0]["longShortRatio"]) if d else None
    except:
        return None

def fetch_oi_change(symbol):
    """抓取當前 OI 及 24h 變化 %，僅支援合約幣種"""
    try:
        r1 = requests.get(f"{FUTURES_BASE}/fapi/v1/openInterest",
                          params={"symbol": symbol}, timeout=8)
        cur_oi = float(r1.json().get("openInterest", 0))
        r2 = requests.get(f"{FUTURES_BASE}/futures/data/openInterestHist",
                          params={"symbol": symbol, "period": "1h", "limit": 25}, timeout=8)
        hist = r2.json()
        if isinstance(hist, list) and len(hist) >= 2:
            old_oi = float(hist[0].get("sumOpenInterest", 0))
            chg = (cur_oi - old_oi) / old_oi * 100 if old_oi > 0 else 0
            return {"oi": round(cur_oi, 2), "oi_change_24h": round(chg, 2)}
        return {"oi": round(cur_oi, 2), "oi_change_24h": None}
    except:
        return None

# ── 全市場掃描 ────────────────────────────────────────────────
STABLECOINS = {"USDT","USDC","DAI","BUSD","TUSD","FDUSD","USDP","GUSD","FRAX","USDD"}
WRAPPED     = {"WBTC","WETH","WBNB","STETH","WSTETH","CBBTC","RETH","BETH"}

MARKET_SCAN_MIN_VOL_USD = int(os.environ.get("MARKET_SCAN_MIN_VOL_USD", 5_000_000))
MARKET_SCAN_TOP_N       = int(os.environ.get("MARKET_SCAN_TOP_N", 25))

def fetch_market_scan():
    """動態抓幣 stage1：OKX 全市場 ticker 一次抓 → 流動性門檻篩選 →
    依漲跌幅絕對值排序（同時涵蓋 LONG/SHORT 機會）取前 N 個候選。
    候選清單再交給 /api/scan 算 LANA 分（stage2），避免對全市場逐一跑 K 線造成 timeout。"""
    try:
        import requests as _rq
        r = _rq.get("https://www.okx.com/api/v5/market/tickers",
                    params={"instType": "SWAP"}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
    except Exception as e:
        log.warning(f"OKX 全市場抓取失敗: {e}")
        return []

    results = []
    for t in data:
        inst = t.get("instId", "")
        if not inst.endswith("-USDT-SWAP"):
            continue
        coin = inst.replace("-USDT-SWAP", "")
        if coin in STABLECOINS or coin in WRAPPED:
            continue
        try:
            last   = float(t.get("last", 0) or 0)
            open24 = float(t.get("open24h", 0) or 0)
            vol    = float(t.get("volCcy24h", 0) or 0)
        except (TypeError, ValueError):
            continue
        if vol < MARKET_SCAN_MIN_VOL_USD or open24 <= 0 or last <= 0:
            continue
        chg = (last / open24 - 1) * 100
        results.append({"coin": coin, "price": last,
                        "change": round(chg, 2), "volume": round(vol)})

    # 去重（同一幣只留第一筆）
    seen = set()
    unique = []
    for r in results:
        if r["coin"] not in seen:
            seen.add(r["coin"])
            unique.append(r)

    # 依漲跌幅絕對值排序：量先於價的策略下，大幅波動（不分漲跌）才是值得進一步算分的候選
    unique.sort(key=lambda x: abs(x["change"]), reverse=True)
    return unique[:MARKET_SCAN_TOP_N]

# ── 深度分析 ────────────────────────────────────────────────
def analyze_coin(coin):
    symbol = coin.upper() + "USDT"
    klines  = fetch_klines(symbol, "1h", 150)  # 改用1H K線，與TG推送/土狗一致
    ticker  = fetch_ticker(symbol)
    closes  = [k["c"] for k in klines]
    volumes = [k["v"] for k in klines]
    price   = closes[-1]

    ma7  = ma(closes, 7)
    ma30 = ma(closes, 30)
    ma120= ma(closes, 120)
    r14  = rsi(closes)
    bb_up, bb_mid, bb_lo = bollinger(closes)
    vr   = vol_ratio(volumes)
    funding  = fetch_funding(symbol)
    gl_ls    = fetch_global_ls(symbol)
    top_ls   = fetch_top_trader_ls(symbol)
    oi_data  = fetch_oi_change(symbol)

    c24h  = float(ticker.get("priceChangePercent", 0))
    vol24 = float(ticker.get("quoteVolume", 0))
    hi24  = float(ticker.get("highPrice", price))
    lo24  = float(ticker.get("lowPrice", price))
    # 1H K線：7日=168根，30日=720根（但只抓150根，30日改用150根近似）
    c7d   = (price / closes[-168] - 1) * 100 if len(closes) >= 168 else None
    c30d  = (price / closes[-150] - 1) * 100 if len(closes) >= 150 else None  # 近似（約6.25天）

    # 風險信號
    risks = []
    if r14 and r14 > 70:              risks.append("RSI 超買")
    if bb_up and price > bb_up:       risks.append("突破布林上軌")
    if vr and vr < 0.8:               risks.append("量能萎縮（價升量縮）")
    if gl_ls and top_ls and gl_ls > 1.1 and top_ls < 1.0:
                                       risks.append("散戶追多、大戶偏空")
    if funding and funding > 0.001:   risks.append("資金費率過高")
    if c30d and c30d > 50:            risks.append("近期(約6天)漲幅逾50%，注意過熱")

    n = len(risks)
    risk_level = "extreme" if n >= 4 else "high" if n >= 3 else "medium" if n >= 2 else "low" if n >= 1 else "safe"

    bb_pos = (
        "above_upper" if bb_up and price > bb_up else
        "upper_half"  if bb_up and bb_mid and price > bb_mid else
        "lower_half"  if bb_lo and bb_mid and price > bb_lo else
        "below_lower"
    )
    contract_ctx = {
        'oi_change_24h':   oi_data['oi_change_24h'] if oi_data else None,
        'price_change_24h': c24h,
        'funding':          funding,
        'ls_ratio':         gl_ls,
    }
    # high20: 近20根K線高點（突破偵測用）
    _high20 = max([k["h"] for k in klines[-20:]]) if len(klines) >= 20 else None
    ls = calc_lana_score(ma7, ma30, ma120, r14, vr, bb_pos, risks, contract=contract_ctx,
                         price=price, high20=_high20, change_24h=c24h)
    ls_grade = ("💎 極強" if ls["total"] >= 80 else
                "🟢 強"   if ls["total"] >= 65 else
                "🟡 普通" if ls["total"] >= 50 else
                "🟠 弱"   if ls["total"] >= 35 else
                "🔴 危險")

    return {
        "coin": coin.upper(),
        "symbol": symbol,
        "price": price,
        "change_24h": round(c24h, 2),
        "change_7d":  round(c7d, 2)  if c7d  is not None else None,
        "change_30d": round(c30d, 2) if c30d is not None else None,
        "volume_24h": round(vol24),
        "high_24h": hi24,
        "low_24h": lo24,
        "ma7": ma7,
        "ma30": ma30,
        "ma120": ma120,
        "ma_bull": bool(ma7 and ma30 and ma120 and ma7 > ma30 > ma120),
        "rsi": r14,
        "bb_upper": bb_up,
        "bb_mid": bb_mid,
        "bb_lower": bb_lo,
        "bb_position": bb_pos,
        "vol_ratio": vr,
        "funding_rate":    round(funding * 100, 5) if funding is not None else None,
        "global_ls":       round(gl_ls, 3)   if gl_ls   else None,
        "top_ls":          round(top_ls, 3)  if top_ls  else None,
        "oi":              oi_data['oi']           if oi_data else None,
        "oi_change_24h":   oi_data['oi_change_24h'] if oi_data else None,
        "early_ambush":    ls.get("early_ambush", False),
        "risks": risks,
        "risk_level": risk_level,
        "lana_score":  ls["total"],
        "lana_grade":  ls_grade,
        "lana_detail": ls,
        "support1": round(price * 0.75, 6),
        "support2": round(ma30, 6) if ma30 else round(price * 0.85, 6),
        "resistance": round(bb_up, 6) if bb_up else round(price * 1.15, 6),
        "ts": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }

# ── Routes ───────────────────────────────────────────────────
@app.route("/api/test_market")
def api_test_market():
    """測試動態抓幣可行性：OKX 全市場 + 幣安永續清單 + 交集"""
    import requests as _rq
    out = {}
    # 1. OKX 全市場 SWAP
    try:
        r = _rq.get("https://www.okx.com/api/v5/market/tickers",
                    params={"instType": "SWAP"}, timeout=15)
        if r.ok:
            data = r.json().get("data", [])
            okx_usdt = {}
            for t in data:
                inst = t.get("instId", "")
                if inst.endswith("-USDT-SWAP"):
                    coin = inst.replace("-USDT-SWAP", "")
                    vol = float(t.get("volCcy24h", 0) or 0)
                    chg = (float(t.get("last", 0) or 0) / float(t.get("open24h", 1) or 1) - 1) * 100
                    okx_usdt[coin] = {"vol24h_usd": round(vol), "change_24h": round(chg, 1)}
            out["okx_count"] = len(okx_usdt)
            out["okx_sample"] = dict(list(okx_usdt.items())[:3])
            out["_okx_coins"] = set(okx_usdt.keys())
        else:
            out["okx_error"] = f"HTTP {r.status_code}"
    except Exception as e:
        out["okx_error"] = str(e)[:100]

    # 2. 幣安永續合約清單
    try:
        r = _rq.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
        if r.ok:
            syms = r.json().get("symbols", [])
            bn = {s["baseAsset"] for s in syms
                  if s.get("quoteAsset") == "USDT" and s.get("status") == "TRADING"
                  and s.get("contractType") == "PERPETUAL"}
            out["binance_count"] = len(bn)
            out["_binance_coins"] = bn
        else:
            out["binance_error"] = f"HTTP {r.status_code}"
    except Exception as e:
        out["binance_error"] = str(e)[:100]

    # 3. 交集 + 流動性篩選
    if "_okx_coins" in out and "_binance_coins" in out:
        inter = out["_okx_coins"] & out["_binance_coins"]
        out["intersection_count"] = len(inter)
        out["intersection_sample"] = sorted(list(inter))[:15]
    out.pop("_okx_coins", None)
    out.pop("_binance_coins", None)
    return jsonify(out)

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/analyze/<coin>")
def api_analyze(coin):
    try:
        data = analyze_coin(coin)
        return jsonify({"ok": True, "data": data})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/watchlist")
def api_watchlist_get():
    return jsonify({"ok": True, "coins": load_watchlist()})

@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    coin = (request.json or {}).get("coin", "").upper().strip()
    if not coin:
        return jsonify({"ok": False, "error": "coin required"}), 400
    coins = load_watchlist()
    if coin not in coins:
        coins.append(coin)
        save_watchlist(coins)
    return jsonify({"ok": True, "coins": coins})

@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    coin = (request.json or {}).get("coin", "").upper().strip()
    coins = [c for c in load_watchlist() if c != coin]
    save_watchlist(coins)
    return jsonify({"ok": True, "coins": coins})

def _fast_score_one(coin):
    """超快速評分，不拉 4H K 線，專供土狗 tab 使用"""
    try:
        symbol  = f"{coin}USDT"
        klines  = fetch_klines(symbol)
        if not klines or len(klines) < 30:
            return coin, None, None, None, None, False, ""
        price   = klines[-1]["c"]
        ma7     = sum(k["c"] for k in klines[-7:]) / 7
        ma30    = sum(k["c"] for k in klines[-30:]) / 30
        ma120   = sum(k["c"] for k in klines[-120:]) / 120 if len(klines) >= 120 else None
        r14     = calc_rsi(klines, 14)
        vr      = calc_vol_ratio(klines)
        bb_pos  = calc_bb_position(klines, 20, 2.0)
        risks   = []
        price_now  = klines[-1]["c"]
        high20     = max(k["h"] for k in klines[-20:]) if len(klines) >= 20 else None
        change_24h = ((price_now - klines[-25]["c"]) / klines[-25]["c"] * 100) if len(klines) >= 25 else None
        ls      = calc_lana_score(ma7, ma30, ma120, r14, vr, bb_pos, risks,
                                  price=price_now, high20=high20, change_24h=change_24h)
        score   = ls["total"]
        grade   = ("💎 極強" if score >= 80 else "🟢 強" if score >= 65 else
                   "🟡 普通" if score >= 50 else "🟠 弱" if score >= 35 else "🔴 危險")
        ma_bull = bool(ma7 and ma30 and ma120 and ma7 > ma30 > ma120)
        return coin, score, grade, round(r14, 1) if r14 else None, round(vr, 2) if vr else None, ma_bull, bb_pos
    except Exception:
        return coin, None, None, None, None, False, ""



def _refresh_meme_cache():
    """背景掃描所有幣，結果存入 cache"""
    global _meme_cache, _meme_cache_time
    ALL_COINS = [
        # 主流
        "BTC", "ETH", "SOL", "BNB", "XRP",
        # MEME
        "DOGE", "SHIB", "PEPE", "BONK", "WIF",
        # AI/生態
        "WLD", "TAO", "NEAR", "FET", "RENDER",
        # Layer2
        "ARB", "OP", "APT", "SUI", "INJ",
    ]
    results = []
    for coin in ALL_COINS:
        try:
            coin_r, score, grade, rsi_val, vr_val, ma_bull, bb_pos = _fast_score_one(coin)
            if score is None:
                continue
            symbol = coin + "USDT"
            ticker = fetch_ticker(symbol)
            change = float(ticker.get("priceChangePercent", 0)) if ticker else 0
            price  = float(ticker.get("lastPrice", 0)) if ticker else 0
            direction = "LONG" if score >= 55 and ma_bull else "WATCH"
            entry_lo = round(price * 0.995, 6) if price else 0
            entry_hi = round(price * 1.002, 6) if price else 0
            sl = round(price * 0.97, 6) if price else 0
            t1 = round(price * 1.04, 6) if price else 0
            t2 = round(price * 1.08, 6) if price else 0
            rsi_note = ("RSI超賣" if rsi_val and rsi_val < 35 else
                        "RSI偏弱" if rsi_val and rsi_val < 50 else
                        "RSI中性" if rsi_val and rsi_val < 60 else "RSI偏強")
            ma_note = "MA多頭" if ma_bull else "MA空頭"
            results.append({
                "coin": coin, "symbol": coin, "exchange": "okx",
                "price": price, "change": round(change, 2),
                "change_24h": round(change, 2),
                "score": score, "lana_score": score, "lana_grade": grade,
                "direction": direction,
                "rsi": rsi_val, "rsi_1h": rsi_val, "vol_ratio": vr_val,
                "ma_bull": ma_bull,
                "summary": f"{ma_note}，{rsi_note}",
                "reason": f"LANA評分 {score}/100，量比 {vr_val or 'N/A'}x",
                "entry_zone": f"{entry_lo}-{entry_hi}",
                "stop_loss": str(sl), "target_1": str(t1), "target_2": str(t2),
                "timeframe": "4-8小時",
                "risk_note": "嚴控倉位，設好止損，單筆不超 3-5%",
            })
        except Exception as e:
            log.warning(f"meme_cache {coin}: {e}")
            continue
    results.sort(key=lambda x: x["lana_score"], reverse=True)
    _meme_cache["results"] = results
    _meme_cache["ts"] = datetime.now().strftime("%Y-%m-%d %H:%M")
    _meme_cache_time = time.time()
    log.info(f"meme cache 更新，共 {len(results)} 顆")

def _quick_score_one(coin):
    """用 klines 快速算 LANA 分數（不含 futures），供掃描列表用"""
    try:
        symbol = coin.upper() + "USDT"
        klines  = fetch_klines(symbol)
        if not klines:
            return coin, None, None
        closes  = [k["c"] for k in klines]
        volumes = [k["v"] for k in klines]
        ma7  = ma(closes, 7)
        ma30 = ma(closes, 30)
        ma120= ma(closes, 120)
        r14  = rsi(closes)
        bb_up, bb_mid, bb_lo = bollinger(closes)
        vr   = vol_ratio(volumes)
        price = closes[-1]
        bb_pos = (
            "above_upper" if bb_up and price > bb_up else
            "upper_half"  if bb_up and bb_mid and price > bb_mid else
            "lower_half"  if bb_lo and bb_mid and price > bb_lo else
            "below_lower"
        )
        risks = []
        if r14 and r14 > 70: risks.append("RSI超買")
        if bb_up and price > bb_up: risks.append("突破布林上軌")
        if vr and vr < 0.8: risks.append("量能萎縮")
        # 計算近 20 根最高點（突破訊號用）
        high20 = max(k["h"] for k in klines[-20:]) if len(klines) >= 20 else None
        price_now = klines[-1]["c"]
        # 24h 漲幅
        change_24h = ((price_now - klines[-25]["c"]) / klines[-25]["c"] * 100) if len(klines) >= 25 else None
        ls = calc_lana_score(ma7, ma30, ma120, r14, vr, bb_pos, risks,
                             price=price_now, high20=high20, change_24h=change_24h)
        score = ls["total"]

        grade = ("💎 極強" if score >= 80 else "🟢 強" if score >= 65 else
                 "🟡 普通" if score >= 50 else "🟠 弱" if score >= 35 else "🔴 危險")
        ma_bull = bool(ma7 and ma30 and ma120 and ma7 > ma30 > ma120)
        return coin, score, grade, round(r14, 1) if r14 else None, round(vr, 2) if vr else None, ma_bull, bb_pos
    except Exception:
        return coin, None, None, None, None, False, ""


@app.route("/api/scan", methods=["GET", "POST"])
def api_scan():
    try:
        data = fetch_market_scan()
        wl = load_watchlist()
        # 觀察名單幣強制出現（即使漲幅低於門檻）
        scan_coins = {d['coin'] for d in data}
        for coin in wl:
            if coin not in scan_coins:
                try:
                    t = fetch_ticker(coin + "USDT")
                    chg = float(t.get("priceChangePercent", 0))
                    vol = float(t.get("quoteVolume", 0))
                    prc = float(t.get("lastPrice", 0))
                    data.append({"coin": coin, "price": prc,
                                 "change": round(chg, 2), "volume": round(vol)})
                except Exception:
                    pass
        # 平行計算所有幣的快速 LANA 分數
        all_coins = [d['coin'] for d in data]
        score_map = {}
        with ThreadPoolExecutor(max_workers=8) as ex:
            for coin, score, grade, rsi_val, vr_val, ma_bull, bb_pos in ex.map(_quick_score_one, all_coins):
                if score is not None:
                    score_map[coin] = {
                        "lana_score": score, "lana_grade": grade,
                        "rsi": rsi_val, "vol_ratio": vr_val,
                        "ma_bull": ma_bull, "bb_position": bb_pos
                    }
        for d in data:
            if d['coin'] in score_map:
                d.update(score_map[d['coin']])
        # 觀察名單置頂
        wl_set = set(wl)
        data.sort(key=lambda x: (0 if x['coin'] in wl_set else 1, -x['change']))
        return jsonify({"ok": True, "data": data,
                        "watchlist": wl,
                        "ts": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

@app.route("/api/quick_scores", methods=["POST"])
def api_quick_scores():
    """批次快速計算多幣 LANA 分數（只用 klines，不打 futures API，速度快）"""
    body = request.json or {}
    coins = body.get("coins", [])[:30]

    def _quick_score(coin):
        try:
            symbol = coin.upper() + "USDT"
            klines  = fetch_klines(symbol)
            closes  = [k["c"] for k in klines]
            volumes = [k["v"] for k in klines]
            ma7  = ma(closes, 7)
            ma30 = ma(closes, 30)
            ma120= ma(closes, 120)
            r14  = rsi(closes)
            bb_up, bb_mid, bb_lo = bollinger(closes)
            vr   = vol_ratio(volumes)
            price = closes[-1]
            bb_pos = (
                "above_upper" if bb_up and price > bb_up else
                "upper_half"  if bb_up and bb_mid and price > bb_mid else
                "lower_half"  if bb_lo and bb_mid and price > bb_lo else
                "below_lower"
            )
            risks = []
            if r14 and r14 > 70: risks.append("RSI超買")
            if bb_up and price > bb_up: risks.append("突破布林上軌")
            if vr and vr < 0.8: risks.append("量能萎縮")
            ls = calc_lana_score(ma7, ma30, ma120, r14, vr, bb_pos, risks)
            score = ls["total"]
            grade = ("💎 極強" if score >= 80 else "🟢 強" if score >= 65 else
                     "🟡 普通" if score >= 50 else "🟠 弱" if score >= 35 else "🔴 危險")
            return {"coin": coin, "score": score, "grade": grade}
        except Exception:
            return {"coin": coin, "score": None, "grade": None}

    try:
        with ThreadPoolExecutor(max_workers=10) as ex:
            results = list(ex.map(_quick_score, coins))
        return jsonify({"ok": True, "scores": {r["coin"]: r for r in results}})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/email", methods=["POST"])
def api_email():
    body = request.json or {}
    subject = body.get("subject", "[LANA] 分析報告")
    text    = body.get("text", "")
    try:
        resend_key = os.environ.get("RESEND_KEY", "re_e8vfjUR6_2svn3PJAnp8Q3VD5jtu8Xwjn")
        resp = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {resend_key}",
                "Content-Type": "application/json"
            },
            json={
                "from": "LANA 妖幣監測 <onboarding@resend.dev>",
                "to": [NOTIFY_TO],
                "subject": subject,
                "text": text
            },
            timeout=15
        )
        data = resp.json()
        if resp.status_code in (200, 201) and data.get("id"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": data.get("message", str(resp.status_code))}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/api/telegram", methods=["POST"])
def api_telegram():
    body = request.json or {}
    text = body.get("text", "")
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15
        )
        data = resp.json()
        if data.get("ok"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": data.get("description", "unknown")}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── 風控 Routes ───────────────────────────────────────────────
@app.route("/api/risk/status", methods=["GET"])
def api_risk_status():
    return jsonify({"ok": True, "status": compute_risk_status(load_risk_data())})

@app.route("/api/risk/add", methods=["POST"])
def api_risk_add():
    body = request.json or {}
    coin = body.get("coin", "").upper().strip()
    pnl  = body.get("pnl_usd")
    if not coin or pnl is None:
        return jsonify({"ok": False, "error": "coin 和 pnl_usd 必填"}), 400
    data = load_risk_data()
    old_locked = compute_risk_status(data).get('locked', False)
    trade = {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_local": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "coin": coin, "pnl_usd": float(pnl),
        "entry_price": body.get("entry_price"),
        "exit_price": body.get("exit_price"),
        "note": body.get("note", "").strip()
    }
    data["trades"].insert(0, trade)
    new_status = compute_risk_status(data)
    save_risk_data(data)
    # 新觸發熔斷 → Telegram 警告
    if new_status.get('locked') and not old_locked:
        try:
            lu = new_status.get('lock_until','')[:16].replace('T',' ')
            msg = (f"⛔ <b>LANA 風控啟動</b>\n原因：{new_status['reason']}\n"
                   f"鎖定至：{lu}\n\n建議：休息一下，保護本金。")
            requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
        except: pass
    return jsonify({"ok": True, "trade": trade, "status": new_status})

@app.route("/api/risk/unlock", methods=["POST"])
def api_risk_unlock():
    if not (request.json or {}).get("confirm"):
        return jsonify({"ok": False, "error": "需要確認"}), 400
    data = load_risk_data()
    unlock_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    data["force_unlock_until"] = unlock_until
    data["trades"].insert(0, {
        "id": str(uuid.uuid4())[:8],
        "ts": datetime.now(timezone.utc).isoformat(),
        "ts_local": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "coin": "SYSTEM", "pnl_usd": 0, "note": "⚠️ 手動強制解鎖"
    })
    save_risk_data(data)
    try:
        msg = (f"⚠️ <b>LANA 風控手動解鎖</b>\n時間：{datetime.now().strftime('%Y-%m-%d %H:%M')}\n"
               f"有效期：1 小時\n\n請謹慎操作，控制倉位。")
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=10)
    except: pass
    return jsonify({"ok": True, "unlock_until": unlock_until})

@app.route("/api/risk/settings", methods=["POST"])
def api_risk_settings():
    body = request.json or {}
    data = load_risk_data()
    s = data.get("settings", dict(RISK_DEFAULT["settings"]))
    for k in ("capital","weekly_loss_pct","daily_loss_pct","consecutive_loss_limit"):
        if k in body:
            s[k] = int(body[k]) if k == "consecutive_loss_limit" else float(body[k])
    data["settings"] = s
    save_risk_data(data)
    return jsonify({"ok": True, "settings": s, "status": compute_risk_status(data)})

# ── 回測 Routes ───────────────────────────────────────────────
@app.route("/api/backtest/snapshot", methods=["POST"])
def api_backtest_snapshot():
    today = datetime.now().strftime('%Y-%m-%d')
    data  = load_backtest()
    if today in data.get('snapshots', {}):
        return jsonify({'ok': True, 'msg': '今日快照已存在',
                        'count': len(data['snapshots'][today]), 'date': today})
    try:
        scan = fetch_market_scan()
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

    wl         = load_watchlist()
    candidates = list({d['coin'] for d in scan[:30]} | set(wl))[:25]
    analyzed   = []
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(_analyze_safe, c): c for c in candidates}
        for f in as_completed(futs, timeout=60):
            d = f.result()
            if d and d.get('lana_score') is not None:
                analyzed.append(d)

    analyzed.sort(key=lambda x: x['lana_score'], reverse=True)
    snapshot = [{
        'coin': d['coin'], 'lana_score': d['lana_score'], 'price': d['price'],
        'change_24h': d['change_24h'], 'volume_24h': d['volume_24h'],
        'rsi': d['rsi'], 'ma_bull': d['ma_bull'],
        'vol_ratio': d['vol_ratio'], 'bb_position': d.get('bb_position'),
        'sector': get_sector(d['coin']), 'ts': datetime.now().isoformat()
    } for d in analyzed[:20]]

    data.setdefault('snapshots', {})[today] = snapshot
    save_backtest(data)
    return jsonify({'ok': True, 'count': len(snapshot), 'date': today})

@app.route("/api/backtest/validate", methods=["POST"])
def api_backtest_validate():
    data      = load_backtest()
    snapshots = data.get('snapshots', {})
    results   = data.get('results', {})
    now       = datetime.now()
    updated   = 0
    for date_str, snap in snapshots.items():
        if date_str in results:
            continue
        try:
            snap_date = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            continue
        if (now - snap_date).total_seconds() < 86400:
            continue
        day_res = []
        for entry in snap:
            try:
                ticker    = fetch_ticker(entry['coin'] + 'USDT')
                price_now = float(ticker.get('lastPrice', 0))
                pnl       = round((price_now / entry['price'] - 1) * 100, 2) if entry.get('price') else None
                day_res.append({**entry, 'price_24h': price_now, 'pnl_pct': pnl,
                                 'validated_at': now.isoformat()})
                updated += 1
            except: pass
        if day_res:
            results[date_str] = day_res
    data['results'] = results
    if updated:
        save_backtest(data)
    return jsonify({'ok': True, 'updated': updated, **compute_backtest_stats(data)})

@app.route("/api/backtest/stats", methods=["GET"])
def api_backtest_stats():
    return jsonify({'ok': True, **compute_backtest_stats(load_backtest())})

@app.route("/api/backtest/weekly", methods=["POST"])
def api_backtest_weekly():
    stats = compute_backtest_stats(load_backtest())
    rngs  = stats.get('ranges', [])
    secs  = stats.get('sectors', [])
    rngs_txt = "\n".join(
        f"  Score {r['label']}: 平均 {r['avg_pnl']:+.1f}%  命中 {r['win_rate']}%  ({r['count']}筆)"
        if r['count'] else f"  Score {r['label']}: 資料不足"
        for r in rngs)
    secs_txt = "\n".join(
        f"  {'🔥' if i==0 else '📊'} {s['sector']}: 平均 {s['avg_pnl']:+.1f}%  ({s['count']}筆)"
        for i, s in enumerate(secs[:4])) or "  尚無敘事資料"
    msg = (f"📊 <b>LANA Score 回測週報</b>  {datetime.now().strftime('%Y/%m/%d')}\n\n"
           f"累積 <b>{stats['total_samples']}</b> 筆  |  <b>{stats['days']}</b> 天\n\n"
           f"Score 表現：\n{rngs_txt}\n\n板塊排行：\n{secs_txt}\n\n"
           f"⚠️ 僅供回測參考，不構成投資建議")
    try:
        r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=15)
        d = r.json()
        if d.get("ok"): return jsonify({"ok": True})
        return jsonify({"ok": False, "error": d.get("description","unknown")}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

# ── 交易日誌 Routes ───────────────────────────────────────────
@app.route("/api/journal", methods=["GET"])
def api_journal_get():
    return jsonify({"ok": True, "entries": load_journal()})

@app.route("/api/journal/add", methods=["POST"])
def api_journal_add():
    body = request.json or {}
    coin      = body.get("coin", "").upper().strip()
    sentiment = body.get("sentiment", "")
    reason    = body.get("reason", "").strip()
    if not coin or not sentiment:
        return jsonify({"ok": False, "error": "coin 和 sentiment 必填"}), 400
    try:
        data = analyze_coin(coin)
    except Exception as e:
        return jsonify({"ok": False, "error": f"無法取得 {coin} 資料：{e}"}), 400
    entry = {
        "id":         str(uuid.uuid4())[:8],
        "ts":         datetime.now(timezone.utc).isoformat(),
        "ts_local":   datetime.now().strftime("%Y-%m-%d %H:%M"),
        "coin":       coin,
        "price_at":   data["price"],
        "lana_score": data["lana_score"],
        "lana_grade": data["lana_grade"],
        "sentiment":  sentiment,
        "reason":     reason,
        "validated":  False,
        "price_24h":  None,
        "pnl_pct":    None,
        "decision":   None,
    }
    entries = load_journal()
    entries.insert(0, entry)
    save_journal(entries)
    return jsonify({"ok": True, "entry": entry})

@app.route("/api/journal/validate", methods=["POST"])
def api_journal_validate():
    entries = load_journal()
    now     = datetime.now(timezone.utc)
    updated = 0
    for e in entries:
        if e.get("validated"):
            continue
        try:
            ts = datetime.fromisoformat(e["ts"])
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if (now - ts).total_seconds() < 86400:
            continue
        try:
            ticker    = fetch_ticker(e["coin"] + "USDT")
            price_now = float(ticker.get("lastPrice", 0))
            pnl_pct   = round((price_now / e["price_at"] - 1) * 100, 2) if e["price_at"] else None
            s = e.get("sentiment", "")
            bullish = s == "很想買"
            bearish = s in ("怕了", "已經漲太多")
            if bullish:
                decision = "✅ 直覺正確" if (pnl_pct or 0) > 0 else "❌ 直覺失準"
            elif bearish:
                decision = "✅ 直覺正確" if (pnl_pct or 0) < 0 else "❌ 機會錯過"
            else:
                decision = f"📈 漲了 {pnl_pct:+.1f}%" if (pnl_pct or 0) > 3 else (
                           f"📉 跌了 {pnl_pct:+.1f}%" if (pnl_pct or 0) < -3 else "➖ 橫盤")
            e.update({"validated": True, "price_24h": price_now,
                      "pnl_pct": pnl_pct, "decision": decision})
            updated += 1
        except Exception:
            pass
    if updated:
        save_journal(entries)
    return jsonify({"ok": True, "updated": updated, "entries": entries})

@app.route("/api/journal/weekly", methods=["POST"])
def api_journal_weekly():
    entries   = load_journal()
    now       = datetime.now(timezone.utc)
    week_ago  = now - timedelta(days=7)
    week_ents = [e for e in entries if _parse_ts(e.get("ts","")) >= week_ago]
    validated = [e for e in week_ents if e.get("validated")]
    correct   = [e for e in validated if e.get("decision","").startswith("✅")]
    accuracy  = f"{len(correct)}/{len(validated)}" if validated else "尚無驗證"
    coin_cnt  = Counter(e["coin"] for e in week_ents)
    top_coins = "、".join(f"{c}({n}次)" for c, n in coin_cnt.most_common(3)) or "—"
    sent_cnt  = Counter(e["sentiment"] for e in week_ents)
    lines = [
        f"📔 <b>LANA 盤感週報</b>  {now.strftime('%Y/%m/%d')}",
        "",
        f"本週記錄 <b>{len(week_ents)}</b> 筆  |  驗證正確率：<b>{accuracy}</b>",
        f"關注最多：{top_coins}",
        "",
        "情緒分布：" + "  ".join(f"{s} {n}次" for s, n in sent_cnt.most_common()),
    ]
    if validated:
        lines += ["", "近期驗證："]
        for e in validated[:5]:
            lines.append(f"  {e['coin']}  {e['sentiment']}  {e['pnl_pct']:+.1f}%  {e['decision']}")
    lines += ["", "⚠️ 僅供自我覆盤，不構成投資建議"]
    text = "\n".join(lines)
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15)
        d = resp.json()
        if d.get("ok"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": d.get("description","unknown")}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def _parse_ts(ts_str):
    try:
        ts = datetime.fromisoformat(ts_str)
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    except Exception:
        return datetime.min.replace(tzinfo=timezone.utc)

# ── 缺口5：板塊熱度雷達 ───────────────────────────────────────

SECTOR_COINS = {
    '迷因':  ['DOGE','SHIB','PEPE','WIF','BONK','FLOKI','MOG','TRUMP','GIGGLE'],
    'AI':    ['FET','AGIX','RNDR','TAO','WLD','AKT','SKYAI'],
    'DeSci': ['BIO','GRT','VITA','RIF'],
    'L1':    ['SOL','AVAX','APT','SUI','SEI'],
    'L2':    ['ARB','OP','MATIC','STRK','ZK'],
    'RWA':   ['ONDO','OM','LINK','MAKER'],
}
SECTOR_COLORS = {
    '迷因': '#f97316', 'AI': '#3b82f6', 'DeSci': '#22c55e',
    'L1':   '#a855f7', 'L2': '#06b6d4', 'RWA':   '#f0b90b',
}

def compute_sector_heat():
    # 取全部 ticker（不帶 symbol 參數），在本地過濾板塊幣
    # 與 fetch_market_scan() 相同做法，CDN 支援無參數全量查詢
    all_sector_coins = {c for cs in SECTOR_COINS.values() for c in cs}
    try:
        r = _get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
        data = r.json()
        tickers = {}
        for t in data:
            sym = t.get("symbol", "")
            if sym.endswith("USDT"):
                coin = sym[:-4]
                if coin in all_sector_coins:
                    tickers[coin] = float(t.get("priceChangePercent", 0))
    except Exception:
        tickers = {}

    result = []
    for sector, coins in SECTOR_COINS.items():
        changes = [tickers[c] for c in coins if c in tickers]
        if not changes:
            result.append({"sector": sector, "avg_change": 0, "hot_count": 0,
                           "temp": "❄️", "label": "冷卻", "color": SECTOR_COLORS.get(sector,'#9ca3af'), "coins": coins})
            continue
        avg = round(sum(changes) / len(changes), 2)
        hot = sum(1 for c in changes if c >= 5)
        if avg >= 8:   temp, label = "🔥", "噴發"
        elif avg >= 2: temp, label = "🌡", "暖場"
        else:          temp, label = "❄️", "冷卻"
        result.append({
            "sector": sector, "avg_change": avg, "hot_count": hot,
            "temp": temp, "label": label,
            "color": SECTOR_COLORS.get(sector, '#9ca3af'), "coins": coins
        })
    result.sort(key=lambda x: x["avg_change"], reverse=True)
    return result


@app.route("/api/sector_heat")
def api_sector_heat():
    try:
        return jsonify({"ok": True, "data": compute_sector_heat()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


# ── 缺口6：今日進場指數 ───────────────────────────────────────

def compute_entry_index():
    """計算今日進場指數 0-100，多個訊號加總"""
    score = 50
    details = {}

    # 1. BTC 24h 波動（OKX）
    try:
        r = requests.get(
            "https://www.okx.com/api/v5/market/ticker",
            params={"instId": "BTC-USDT-SWAP"}, timeout=10)
        d = r.json().get("data", [{}])[0]
        last = float(d.get("last", 0))
        open24 = float(d.get("open24h", last))
        chg = ((last - open24) / open24 * 100) if open24 else 0
        btc = {"lastPrice": str(last), "priceChangePercent": str(round(chg, 2))}
        vol = abs(chg)
        price = float(btc.get('lastPrice', 0))
        if vol < 1:
            score -= 20; adj = -20; note = '市場死水'
        elif vol <= 3:
            score += 10; adj = +10; note = '健康波動'
        elif vol > 5:
            score -= 10; adj = -10; note = '波動過大'
        else:
            adj = 0; note = '波動適中'
        details['btc'] = {'change': round(chg, 2), 'price': price, 'adj': adj, 'note': note}
    except Exception:
        details['btc'] = {'change': None, 'price': None, 'adj': 0, 'note': '無法取得'}

    # 2. 活躍幣數量（proxy for high LANA score coins）
    SCAN_COINS = [
        'BTC','ETH','SOL','BNB','XRP','DOGE','ADA','AVAX','DOT','LINK',
        'UNI','MATIC','ATOM','APT','SUI','ARB','OP','INJ','SEI','TIA',
        'PEPE','WIF','BONK','FET','TAO','RNDR','GRT','ONDO','JUP','NEAR',
    ]
    try:
        scan_set = {f"{c}USDT" for c in SCAN_COINS}
        r2 = requests.get(
            "https://www.okx.com/api/v5/market/tickers",
            params={"instType": "SWAP"}, timeout=20)
        all_tickers = r2.json().get("data", [])
        def _okx_sym(inst): return inst.replace("-USDT-SWAP","")+"USDT"
        tickers = [t for t in all_tickers if _okx_sym(t.get("instId","")) in scan_set]
        movers = sum(1 for t in tickers if abs(float(t.get('sodUtc8', 0)) * 100) >= 3)
        if movers > 10:
            score += 30; adj2 = +30; note2 = '板塊輪動明顯'
        elif movers >= 5:
            score += 15; adj2 = +15; note2 = '機會適中'
        else:
            score -= 15; adj2 = -15; note2 = '沒得選'
        details['movers'] = {'count': movers, 'adj': adj2, 'note': note2}
    except Exception:
        details['movers'] = {'count': None, 'adj': 0, 'note': '無法計算'}

    # 3. BTC 成交量 vs 7日均量
    try:
        kr = requests.get(
            "https://www.okx.com/api/v5/market/candles",
            params={"instId": "BTC-USDT-SWAP", "bar": "1D", "limit": 8}, timeout=10)
        klines = list(reversed(kr.json().get("data", [])))
        if len(klines) >= 8:
            today_vol = float(klines[-1][5])
            avg7 = sum(float(k[5]) for k in klines[-8:-1]) / 7
            ratio = round(today_vol / avg7, 2) if avg7 > 0 else 1.0
            if ratio > 1.3:
                score += 15; adj3 = +15; note3 = '資金活躍'
            elif ratio < 0.7:
                score -= 15; adj3 = -15; note3 = '資金低迷'
            else:
                adj3 = 0; note3 = '成交量正常'
            details['vol'] = {'ratio': ratio, 'adj': adj3, 'note': note3}
        else:
            details['vol'] = {'ratio': None, 'adj': 0, 'note': ''}
    except Exception:
        details['vol'] = {'ratio': None, 'adj': 0, 'note': '無法取得'}

    # 4. 恐慌貪婪指數
    try:
        fg_r = requests.get('https://api.alternative.me/fng/?limit=1', timeout=10)
        fg_data = fg_r.json()
        fg_val = int(fg_data['data'][0]['value'])
        fg_text = fg_data['data'][0].get('value_classification', '')
        if (20 <= fg_val <= 30) or (70 <= fg_val <= 80):
            score += 10; adj4 = +10; note4 = '情緒適中'
        elif fg_val > 90:
            score -= 20; adj4 = -20; note4 = '極度貪婪'
        elif fg_val < 10:
            score += 5;  adj4 = +5;  note4 = '極度恐慌'
        else:
            adj4 = 0; note4 = fg_text
        details['fg'] = {'val': fg_val, 'text': fg_text, 'adj': adj4, 'note': note4}
    except Exception:
        details['fg'] = {'val': None, 'adj': 0, 'note': '無法取得'}

    score = max(0, min(100, score))

    if score >= 80:
        label = '強進場日'; color = '#22c55e'; emoji = '🟢'
        advice = '市場機會多，可加大倉位'
    elif score >= 50:
        label = '標準日'; color = '#f0b90b'; emoji = '🟡'
        advice = '正常操作'
    elif score >= 30:
        label = '觀望日'; color = '#f97316'; emoji = '🟠'
        advice = '機會少，謹慎操作'
    else:
        label = '不進場日'; color = '#ef4444'; emoji = '🔴'
        advice = '⛔ 建議今日空手'

    return {
        'score': score, 'label': label, 'color': color,
        'emoji': emoji, 'advice': advice, 'details': details,
        'ts': datetime.now(timezone.utc).isoformat()
    }


@app.route("/api/entry_index")
def api_entry_index():
    try:
        return jsonify(compute_entry_index())
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/entry_index/telegram", methods=["POST"])
def api_entry_index_telegram():
    try:
        d = compute_entry_index()
        det = d['details']
        btc = det.get('btc', {})
        vol = det.get('vol', {})
        fg  = det.get('fg', {})
        mv  = det.get('movers', {})
        lines = [
            f"🌅 <b>LANA 早安日報</b>",
            f"今日進場指數：<b>{d['score']} / 100</b>  {d['emoji']}",
            f"建議：{d['advice']}",
            "",
        ]
        if btc.get('change') is not None:
            lines.append(f"• BTC 24h 波動：{abs(btc['change']):.1f}%  {btc['note']}")
        if mv.get('count') is not None:
            lines.append(f"• 活躍幣數量：{mv['count']} 個  {mv['note']}")
        if vol.get('ratio') is not None:
            lines.append(f"• 市場成交量：{vol['ratio']}x 7d均量  {vol['note']}")
        if fg.get('val') is not None:
            lines.append(f"• 恐慌貪婪：{fg['val']}（{fg['text']}）")
        # 板塊熱度
        try:
            sectors = compute_sector_heat()
            if sectors:
                lines.append("")
                lines.append("🎯 <b>板塊溫度</b>")
                for s in sectors[:4]:
                    lines.append(f"{s['temp']} {s['sector']}（均{s['avg_change']:+.1f}%，{s['hot_count']}個活躍幣）")
        except Exception:
            pass
        lines += ["", "⚠️ 僅供參考，不構成投資建議"]
        text = "\n".join(lines)
        resp = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15)
        r = resp.json()
        if r.get("ok"):
            return jsonify({"ok": True})
        return jsonify({"ok": False, "error": r.get("description", "unknown")}), 500
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500




def _do_ai_analyze(coin, price=0, change24h=0):
    """AI 分析核心邏輯，可直接被 webhook 呼叫（避免 HTTP loopback deadlock）"""
    now_ts = time.time()

    # 冷卻檢查
    cached = _ai_analyze_cache.get(coin)
    if cached and (now_ts - cached["ts"]) < AI_ANALYZE_COOLDOWN_SEC:
        return cached["result"]

    # 取技術指標
    try:
        d = analyze_coin(coin)
        rsi_1h  = d.get("rsi", 50) or 50
        vol_r   = d.get("vol_ratio", 1.0) or 1.0
        funding = d.get("funding_rate", 0) or 0
        ma_bull = d.get("ma_bull", False)
        bb_pos  = d.get("bb_position", "middle")
        price   = price or d.get("price", 0)
    except Exception:
        rsi_1h = 50; vol_r = 1.0; funding = 0; ma_bull = False; bb_pos = "middle"; d = {}

    trend = "up" if ma_bull else "neutral"
    sl_long  = round(price * 0.97, 6) if price else 0
    t1_long  = round(price * 1.04, 6) if price else 0
    t2_long  = round(price * 1.08, 6) if price else 0
    sl_short = round(price * 1.03, 6) if price else 0
    t1_short = round(price * 0.96, 6) if price else 0
    t2_short = round(price * 0.92, 6) if price else 0

    try:
        klines_4h = fetch_klines(f"{coin}USDT", "4h", 6)
        if klines_4h and len(klines_4h) >= 4:
            recent_high = max(k["h"] for k in klines_4h[-4:])
            recent_low  = min(k["l"] for k in klines_4h[-4:])
            pct_from_high = ((price - recent_high) / recent_high * 100) if recent_high else 0
            pct_from_low  = ((price - recent_low)  / recent_low  * 100) if recent_low  else 0
            closes = [k["c"] for k in klines_4h[-4:]]
            rising_count = sum(1 for i in range(1, len(closes)) if closes[i] > closes[i-1])
            price_trend = "上升中" if rising_count >= 3 else ("下跌中" if rising_count <= 1 else "震盪")
            kline_context = f"近4根4H K線：{price_trend}，近期高點={recent_high}（現價距高點{pct_from_high:+.1f}%），近期低點={recent_low}（現價距低點{pct_from_low:+.1f}%）"
        else:
            kline_context = "K線資料不足"
    except:
        kline_context = "K線資料取得失敗"

    prompt = f"""你是專業加密貨幣短線交易員。分析 {coin}/USDT。

現價：{price}，24H漲幅：{change24h:+.1f}%
技術指標：RSI={rsi_1h:.0f}，量比={vol_r:.1f}x，趨勢={'上升' if ma_bull else '中性'}，布林位置={bb_pos}，資金費率={funding:+.4f}%
{kline_context}

⚠️ 判斷原則（多空對等看待，不偏多也不偏空）：
- 24H 漲幅 >15% 且量能放大，代表有資金進場，是強勢標的，回調未破關鍵支撐前可考慮順勢 LONG
- 24H 跌幅 >15% 且量能放大，代表有資金出逃，是弱勢標的，反彈未過關鍵壓力前可考慮順勢 SHORT
- 漲勢中的小幅回調（距高點 -5% 到 -10%）屬正常洗盤，若 RSI 未超買（<70）且 MA 仍多頭，可在支撐位 LONG
- 跌勢中的小幅反彈（距低點 +5% 到 +10%）屬正常洗空，若 RSI 未超賣（>30）且 MA 仍空頭，可在壓力位 SHORT
- 距高點跌超 12% 且 4H 明確轉空（連續多根收黑）：趨勢已轉空，優先考慮 SHORT 而非單純 WATCH
- 距低點漲超 12% 且 4H 明確轉多（連續多根收紅）：趨勢已轉多，優先考慮 LONG 而非單純 WATCH
- RSI >75 嚴重超買且量能萎縮（出貨訊號）→ 傾向 SHORT 或至少 WATCH，不要繼續 LONG
- RSI <25 嚴重超賣且量能萎縮（恐慌出清訊號）→ 傾向 LONG 或至少 WATCH，不要繼續 SHORT
- 只有完全無趨勢、量能極低（vol_ratio <0.5）、RSI 落在 40-60 中性區，才判 WATCH

做多參考價位：止損 {sl_long}，目標1 {t1_long}，目標2 {t2_long}
做空參考價位：止損 {sl_short}，目標1 {t1_short}，目標2 {t2_short}

請給出明確交易建議。entry_zone/stop_loss/target_1/target_2 必須是真實數字。
只輸出JSON：
{{"direction":"LONG或SHORT或WATCH","score":0-100,"confidence":"高或中或低","summary":"一句話","reason":"技術原因","entry_zone":數字,"stop_loss":數字,"target_1":數字,"target_2":數字,"timeframe":"持倉時間","risk_note":"風險"}}"""

    anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
    gemini_key    = os.getenv("GEMINI_API_KEY", "")
    result = None

    if gemini_key:
        try:
            r = requests.post(
                "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.0-flash:generateContent",
                headers={"Content-Type": "application/json", "x-goog-api-key": gemini_key},
                json={"contents": [{"parts": [{"text": prompt}]}],
                      "generationConfig": {"temperature": 0.2, "maxOutputTokens": 800,
                                           "responseMimeType": "application/json"}},
                timeout=25
            )
            if r.ok:
                jr = r.json()
                cands = jr.get("candidates", [])
                if cands:
                    parts = cands[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts)
                    text = text.strip().replace("```json","").replace("```","").strip()
                    if text:
                        result = json.loads(text)
                        result["model"] = "gemini-2.0-flash"
        except Exception as e:
            print(f"[Gemini] 例外 {coin}: {e}")

    if not result and anthropic_key:
        try:
            r = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": anthropic_key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                json={"model": "claude-haiku-4-5-20251001", "max_tokens": 500,
                      "messages": [{"role": "user", "content": prompt}]},
                timeout=20
            )
            if r.ok:
                text = r.json()["content"][0]["text"]
                text = text.strip().replace("```json","").replace("```","").strip()
                result = json.loads(text)
                result["model"] = "claude-haiku"
        except Exception:
            pass

    if not result:
        result = {"direction": "WATCH", "score": 50, "model": "rules",
                  "summary": "AI分析暫時無法使用", "reason": "",
                  "entry_zone": 0, "stop_loss": 0, "target_1": 0, "target_2": 0,
                  "timeframe": "N/A", "risk_note": "請稍後再試"}

    # 修正進出場數字合理性
    def fix(v, default):
        try:
            f = float(v)
            return f if f > 0 else default
        except:
            return default

    direction = result.get("direction", "WATCH")
    if price > 0:
        if direction == "SHORT":
            entry = fix(result.get("entry_zone"), round(price * 1.005, 6))
            sl    = fix(result.get("stop_loss"), sl_short)
            if sl <= entry: sl = max(sl_short, round(entry * 1.02, 6))
            result["entry_zone"] = entry; result["stop_loss"] = sl
            result["target_1"]   = fix(result.get("target_1"), t1_short)
            result["target_2"]   = fix(result.get("target_2"), t2_short)
        else:
            entry = fix(result.get("entry_zone"), round(price * 0.995, 6))
            sl    = fix(result.get("stop_loss"), sl_long)
            if sl >= entry: sl = min(sl_long, round(entry * 0.98, 6))
            result["entry_zone"] = entry; result["stop_loss"] = sl
            result["target_1"]   = fix(result.get("target_1"), t1_long)
            result["target_2"]   = fix(result.get("target_2"), t2_long)

    _ai_analyze_cache[coin] = {"ts": now_ts, "result": result}
    return result


@app.route("/api/ai_analyze", methods=["POST", "OPTIONS"])
def api_ai_analyze():
    # CORS
    if request.method == "OPTIONS":
        resp = jsonify({})
        resp.headers["Access-Control-Allow-Origin"] = "*"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type"
        resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
        return resp

    try:
        body = request.get_json() or {}
        coin      = body.get("symbol", "").upper()
        price     = float(body.get("price", 0))
        change24h = float(body.get("change_24h", 0))

        if not coin:
            return jsonify({"error": "symbol required"}), 400

        result = _do_ai_analyze(coin, price, change24h)
        resp = jsonify(result)
        resp.headers["Access-Control-Allow-Origin"] = "*"
        return resp

    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/meme_signals")
def api_meme_signals():
    """回傳 cache，背景更新（避免 timeout）"""
    global _meme_cache_time
    CACHE_TTL = 900  # 15分鐘
    # 如果 cache 過期或是空的，同步更新一次
    if not _meme_cache["results"] or time.time() - _meme_cache_time > CACHE_TTL:
        try:
            t = threading.Thread(target=_refresh_meme_cache)
            t.daemon = True
            t.start()
            # 等最多 25 秒讓第一次掃描完成
            if not _meme_cache["results"]:
                t.join(timeout=25)
        except Exception as e:
            log.warning(f"meme refresh error: {e}")
    results = _meme_cache["results"]
    signals = [r for r in results if r["direction"] == "LONG"]
    resp = jsonify({
        "signals": signals,
        "all_results": results,
        "last_update": _meme_cache["ts"],
        "last_scan": _meme_cache["ts"],
        "scan_count": 1,
        "debug_count": len(results),
    })
    resp.headers["Access-Control-Allow-Origin"] = "*"
    return resp


def _tg_answer_callback(callback_query_id, text=""):
    """回應 TG 按鈕點擊（必須在 answerCallbackQuery 內回覆，否則按鈕會一直轉圈）"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text},
            timeout=5
        )
    except:
        pass

def _tg_send(chat_id, text, reply_markup=None):
    """發送 TG 訊息，可附帶 inline keyboard"""
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json=payload, timeout=15
        )
    except Exception as e:
        log.warning(f"TG 發送失敗: {e}")

def _tg_edit_message(chat_id, message_id, text):
    """編輯已有訊息（用於回填分析結果）"""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageText",
            json={"chat_id": chat_id, "message_id": message_id,
                  "text": text, "parse_mode": "HTML"},
            timeout=15
        )
    except Exception as e:
        log.warning(f"TG 編輯訊息失敗: {e}")

@app.route("/telegram/webhook", methods=["POST"])
def telegram_webhook():
    """處理 TG 按鈕點擊與文字指令"""
    try:
        data = request.get_json(force=True, silent=True) or {}
        taipei = timezone(timedelta(hours=8))

        # ── 處理按鈕點擊 ──
        cq = data.get("callback_query")
        if cq:
            cq_id   = cq["id"]
            chat_id = str(cq["message"]["chat"]["id"])
            msg_id  = cq["message"]["message_id"]
            action  = cq.get("data", "")
            log.info(f"[webhook] callback action={action}")
            _tg_answer_callback(cq_id, "處理中...")

            if action.startswith("analyze:"):
                coin = action.split(":", 1)[1]
                _tg_edit_message(chat_id, msg_id,
                    cq["message"].get("text", "") + "\n\n⏳ 正在進行深度分析，約30秒...")
                try:
                    # 直接呼叫函式，不打 HTTP 自己（避免 gunicorn single-worker deadlock）
                    res = _do_ai_analyze(coin)
                    if res:
                        import html as _html
                        dir_emoji = {"LONG": "🟢", "SHORT": "🔴"}.get(res.get("direction",""), "⚪")
                        dir_text  = {"LONG": "做多 ▲", "SHORT": "做空 ▼"}.get(res.get("direction",""), "觀望")
                        score = res.get("score", 0)
                        conf  = "高 🔥" if score >= 80 else "中 ✅" if score >= 65 else "低 ⚠️"
                        entry = res.get("entry_zone",""); sl = res.get("stop_loss","")
                        t1 = res.get("target_1",""); t2 = res.get("target_2","")
                        lines = [
                            f"{dir_emoji} <b>{coin}/USDT 深度分析</b>",
                            f"方向: {dir_text}  信心: {conf}  AI分數: {score}/100",
                            f"\n📌 {_html.escape(str(res.get('summary','')))}" if res.get("summary") else "",
                            f"<i>{_html.escape(str(res.get('reason','')))}</i>" if res.get("reason") else "",
                            f"\n🎯 入場: {entry}\n🔴 止損: {sl}\n✅ 目標1: {t1}\n🏆 目標2: {t2}" if entry else "",
                            f"⏱ 持倉: {res.get('timeframe','4-8小時')}\n⚠️ {_html.escape(str(res.get('risk_note','嚴控倉位')))}",
                            f"\n⏰ {datetime.now(taipei).strftime('%H:%M')}"
                        ]
                        _tg_send(chat_id, "\n".join(l for l in lines if l))
                    else:
                        _tg_send(chat_id, f"❌ {coin} 分析失敗，請稍後再試")
                except Exception as e:
                    log.error(f"[webhook] analyze error: {e}", exc_info=True)
                    _tg_send(chat_id, f"❌ 分析錯誤: {e}")

            elif action.startswith("pause:"):
                hours = float(action.split(":",1)[1])
                requests.post(f"{WEB_BASE_URL}/api/push_control",
                    json={"action": "pause", "hours": hours}, timeout=8)
                if hours == 0:
                    _tg_send(chat_id, "⏸ 已永久暫停推送，按下方按鈕可恢復",
                        reply_markup={"inline_keyboard": [[
                            {"text": "▶️ 恢復推送", "callback_data": "resume"}]]})
                else:
                    until_t = datetime.now(taipei) + timedelta(hours=hours)
                    _tg_send(chat_id,
                        f"⏸ 已暫停推送 {int(hours)} 小時（到 {until_t.strftime('%H:%M')}）",
                        reply_markup={"inline_keyboard": [[
                            {"text": "▶️ 提早恢復", "callback_data": "resume"}]]})

            elif action == "resume":
                requests.post(f"{WEB_BASE_URL}/api/push_control",
                    json={"action": "resume"}, timeout=8)
                _tg_send(chat_id, "▶️ 已恢復推送訊號 🟢")

            elif action == "status":
                r = requests.get(f"{WEB_BASE_URL}/api/push_control", timeout=8)
                d = r.json() if r.ok else {}
                status = "🟢 推送中" if d.get("should_push") else f"⏸ {d.get('message','暫停中')}"
                _tg_send(chat_id, f"📊 <b>LANA 推送狀態</b>\n\n{status}",
                    reply_markup={"inline_keyboard": [
                        [{"text": "⏸ 暫停4小時", "callback_data": "pause:4"},
                         {"text": "⏸ 暫停8小時", "callback_data": "pause:8"}],
                        [{"text": "⏸ 永久暫停", "callback_data": "pause:0"},
                         {"text": "▶️ 恢復推送", "callback_data": "resume"}]
                    ]})

            return jsonify({"ok": True})

        # ── 處理文字指令 ──
        msg = data.get("message", {})
        text = (msg.get("text") or "").strip()
        chat_id = str(msg.get("chat", {}).get("id", "") or TELEGRAM_CHAT_ID)
        log.info(f"[webhook] message text={text!r}")

        if text == "/status":
            r = requests.get(f"{WEB_BASE_URL}/api/push_control", timeout=8)
            d = r.json() if r.ok else {}
            status = "🟢 推送中" if d.get("should_push") else f"⏸ {d.get('message','暫停中')}"
            _tg_send(chat_id, f"📊 <b>LANA 推送狀態</b>\n\n{status}",
                reply_markup={"inline_keyboard": [
                    [{"text": "⏸ 暫停4小時", "callback_data": "pause:4"},
                     {"text": "⏸ 暫停8小時", "callback_data": "pause:8"}],
                    [{"text": "⏸ 永久暫停", "callback_data": "pause:0"},
                     {"text": "▶️ 恢復推送", "callback_data": "resume"}]
                ]})
        elif text.startswith("/pause"):
            parts = text.split(); hours = 4
            if len(parts) > 1:
                try: hours = float(parts[1])
                except: pass
            requests.post(f"{WEB_BASE_URL}/api/push_control",
                json={"action": "pause", "hours": hours}, timeout=8)
            until_t = datetime.now(taipei) + timedelta(hours=hours)
            _tg_send(chat_id, f"⏸ 已暫停推送 {int(hours)} 小時（到 {until_t.strftime('%H:%M')}）")
        elif text == "/resume":
            requests.post(f"{WEB_BASE_URL}/api/push_control",
                json={"action": "resume"}, timeout=8)
            _tg_send(chat_id, "▶️ 已恢復推送訊號 🟢")

        return jsonify({"ok": True})

    except Exception as e:
        log.error(f"[webhook] 未捕捉例外: {e}", exc_info=True)
        return jsonify({"ok": True})  # 永遠回200，避免Telegram一直重試

@app.route("/telegram/set_webhook", methods=["GET"])
def set_webhook():
    """一次性設定 TG Webhook，瀏覽器打開這個 URL 就能完成設定"""
    webhook_url = f"{WEB_BASE_URL}/telegram/webhook"
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        json={"url": webhook_url, "allowed_updates": ["message", "callback_query"]},
        timeout=10
    )
    return jsonify({"webhook_url": webhook_url, "tg_response": r.json()})


@app.route("/api/push_control", methods=["GET", "POST"])
def api_push_control():
    """推送開關控制 — GET: 查詢目前狀態; POST: 設定暫停/恢復"""
    global _push_control
    now_ts = time.time()
    taipei = timezone(timedelta(hours=8))

    if request.method == "GET":
        # 暫停到期自動恢復
        if _push_control["paused"] and _push_control["pause_until"] and now_ts >= _push_control["pause_until"]:
            _push_control["paused"] = False
            _push_control["pause_until"] = None

        paused = _push_control["paused"]
        until_str = ""
        if paused and _push_control["pause_until"]:
            until_str = datetime.fromtimestamp(_push_control["pause_until"], taipei).strftime("%H:%M")

        return jsonify({
            "should_push": not paused,
            "paused": paused,
            "pause_until": until_str,
            "message": (
                f"手動暫停中（到 {until_str}）" if paused and until_str else
                "手動暫停中（直到恢復）" if paused else
                "推送中"
            )
        })

    # POST: 設定暫停/恢復
    body = request.get_json(force=True, silent=True) or {}
    action = body.get("action", "")
    hours  = float(body.get("hours", 0))

    if action == "pause":
        _push_control["paused"] = True
        _push_control["pause_until"] = (now_ts + hours * 3600) if hours > 0 else None
        until_str = datetime.fromtimestamp(_push_control["pause_until"], taipei).strftime("%H:%M") if _push_control["pause_until"] else "手動恢復為止"
        return jsonify({"ok": True, "message": f"已暫停推送（到 {until_str}）"})
    elif action == "resume":
        _push_control["paused"] = False
        _push_control["pause_until"] = None
        return jsonify({"ok": True, "message": "已恢復推送"})
    else:
        return jsonify({"ok": False, "error": "action 需為 pause 或 resume"}), 400


@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
