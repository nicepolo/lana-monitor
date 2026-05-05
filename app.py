"""
LANA 妖幣監測 Web App - Flask 後端
部署於 Railway
"""

from flask import Flask, jsonify, render_template, request
import requests
import math
import os
import json
from datetime import datetime

app = Flask(__name__)

# ── 設定（Railway 環境變數優先，fallback 到預設值）──────────────
NOTIFY_TO    = os.environ.get("NOTIFY_TO",    "nicepolo1222@gmail.com")
PORT         = int(os.environ.get("PORT", 5000))

# ── Watchlist 存檔路徑（Railway Volume 掛載在 /data/）─────────────
WATCHLIST_FILE = '/data/watchlist.json'
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
        "bb_position": (
            "above_upper" if bb_up and price > bb_up else
            "upper_half"  if bb_up and bb_mid and price > bb_mid else
            "lower_half"  if bb_lo and bb_mid and price > bb_lo else
            "below_lower"
        ),
        "vol_ratio": vr,
        "funding_rate": round(funding * 100, 5) if funding is not None else None,
        "global_ls": round(gl_ls, 3) if gl_ls else None,
        "top_ls":    round(top_ls, 3) if top_ls else None,
        "risks": risks,
        "risk_level": risk_level,
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

@app.route("/health")
def health():
    return jsonify({"status": "ok", "ts": datetime.now().isoformat()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, debug=False)
