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

app = Flask(__name__)

# ── 設定（Railway 環境變數優先，fallback 到預設值）──────────────
NOTIFY_TO        = os.environ.get("NOTIFY_TO",        "nicepolo1222@gmail.com")
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
    if len(volumes) < 14:
        return None
    r = sum(volumes[-7:]) / 7
    p = sum(volumes[-14:-7]) / 7
    return round(r / p, 2) if p else None

def calc_lana_score(ma7, ma30, ma120, rsi_val, vr, bb_pos, risks):
    """LANA Score 0-100 綜合評分"""
    # 趨勢 25分
    if ma7 and ma30 and ma120 and ma7 > ma30 > ma120:
        s_trend = 25
    elif ma7 and ma30 and ma7 > ma30:
        s_trend = 15
    else:
        s_trend = 5
    # RSI 20分
    if rsi_val is None:          s_rsi = 10
    elif 50 <= rsi_val < 70:     s_rsi = 20
    elif 40 <= rsi_val < 50:     s_rsi = 15
    elif 30 <= rsi_val < 40:     s_rsi = 10
    elif 70 <= rsi_val < 80:     s_rsi = 8
    else:                        s_rsi = 0
    # 量能 20分
    if vr is None:   s_vol = 10
    elif vr >= 2.0:  s_vol = 20
    elif vr >= 1.5:  s_vol = 16
    elif vr >= 1.0:  s_vol = 12
    elif vr >= 0.8:  s_vol = 6
    else:            s_vol = 0
    # BB位置 15分
    s_bb = {'lower_half': 15, 'below_lower': 12, 'upper_half': 8, 'above_upper': 0}.get(bb_pos, 8)
    # 風險 20分
    s_risk = [20, 15, 8, 2, 0][min(len(risks), 4)]
    total = s_trend + s_rsi + s_vol + s_bb + s_risk
    return {
        "total":  max(0, min(100, total)),
        "trend":  s_trend,
        "rsi":    s_rsi,
        "vol":    s_vol,
        "bb":     s_bb,
        "risk":   s_risk
    }

# ── Binance API 抓取 ─────────────────────────────────────────
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

def fetch_klines(symbol, interval="1d", limit=150):
    r = _get(f"{BINANCE_BASE}/api/v3/klines",
             params={"symbol": symbol, "interval": interval, "limit": limit})
    return [{"o": float(d[1]), "h": float(d[2]),
             "l": float(d[3]), "c": float(d[4]), "v": float(d[5])}
            for d in r.json()]

def fetch_ticker(symbol):
    r = _get(f"{BINANCE_BASE}/api/v3/ticker/24hr",
             params={"symbol": symbol})
    return r.json()

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

# ── 全市場掃描 ────────────────────────────────────────────────
STABLECOINS = {"USDT","USDC","DAI","BUSD","TUSD","FDUSD","USDP","GUSD","FRAX","USDD"}
WRAPPED     = {"WBTC","WETH","WBNB","STETH","WSTETH","CBBTC","RETH","BETH"}

def fetch_market_scan():
    r = _get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
    results = []
    for t in r.json():
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        coin = sym[:-4]
        if coin in STABLECOINS or coin in WRAPPED:
            continue
        try:
            chg = float(t["priceChangePercent"])
            vol = float(t["quoteVolume"])
            prc = float(t["lastPrice"])
        except:
            continue
        if chg < 5 or chg > 300 or vol < 20_000_000:
            continue
        results.append({"coin": coin, "price": prc,
                        "change": round(chg, 2), "volume": round(vol)})
    results.sort(key=lambda x: x["change"], reverse=True)
    return results[:30]

# ── 深度分析 ────────────────────────────────────────────────
def analyze_coin(coin):
    symbol = coin.upper() + "USDT"
    klines  = fetch_klines(symbol)
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

    c24h  = float(ticker.get("priceChangePercent", 0))
    vol24 = float(ticker.get("quoteVolume", 0))
    hi24  = float(ticker.get("highPrice", price))
    lo24  = float(ticker.get("lowPrice", price))
    c7d   = (price / closes[-8]  - 1) * 100 if len(closes) >= 8  else None
    c30d  = (price / closes[-31] - 1) * 100 if len(closes) >= 31 else None

    # 風險信號
    risks = []
    if r14 and r14 > 70:              risks.append("RSI 超買")
    if bb_up and price > bb_up:       risks.append("突破布林上軌")
    if vr and vr < 0.8:               risks.append("量能萎縮（價升量縮）")
    if gl_ls and top_ls and gl_ls > 1.1 and top_ls < 1.0:
                                       risks.append("散戶追多、大戶偏空")
    if funding and funding > 0.001:   risks.append("資金費率過高")
    if c30d and c30d > 100:           risks.append("30日漲幅逾100%")

    n = len(risks)
    risk_level = "extreme" if n >= 4 else "high" if n >= 3 else "medium" if n >= 2 else "low" if n >= 1 else "safe"

    bb_pos = (
        "above_upper" if bb_up and price > bb_up else
        "upper_half"  if bb_up and bb_mid and price > bb_mid else
        "lower_half"  if bb_lo and bb_mid and price > bb_lo else
        "below_lower"
    )
    ls = calc_lana_score(ma7, ma30, ma120, r14, vr, bb_pos, risks)
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
        "funding_rate": round(funding * 100, 5) if funding is not None else None,
        "global_ls": round(gl_ls, 3) if gl_ls else None,
        "top_ls":    round(top_ls, 3) if top_ls else None,
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

@app.route("/api/scan")
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
        # 觀察名單置頂
        wl_set = set(wl)
        data.sort(key=lambda x: (0 if x['coin'] in wl_set else 1, -x['change']))
        return jsonify({"ok": True, "data": data,
                        "watchlist": wl,
                        "ts": datetime.now().strftime("%Y-%m-%d %H:%M")})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

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

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
