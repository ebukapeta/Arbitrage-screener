import time, re, ccxt, json, os
import pandas as pd
from flask import Flask, request, jsonify, render_template_string

app = Flask(__name__)

# ====================== SETTINGS ======================
SETTINGS_FILE = "settings.json"

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE) as f:
                return json.load(f)
        except:
            pass
    return {}

def save_settings(settings):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(settings, f, indent=2)
    except:
        pass

saved = load_settings()

# ====================== CONSTANTS ======================
TOP_EXCHANGES = [
    "binance", "okx", "coinbase", "kraken", "bybit", "kucoin",
    "mexc", "bitfinex", "bitget", "gateio", "crypto_com",
    "upbit", "whitebit", "poloniex", "bingx", "lbank",
    "bitstamp", "gemini", "bitrue", "xt", "huobi"
]  # bitmart removed - causes ccxt crash on Render free

EXCHANGE_NAMES = {
    "binance": "Binance", "okx": "OKX", "coinbase": "Coinbase",
    "kraken": "Kraken", "bybit": "Bybit", "kucoin": "KuCoin",
    "mexc": "MEXC", "bitfinex": "Bitfinex", "bitget": "Bitget",
    "gateio": "Gate.io", "crypto_com": "Crypto.com", "upbit": "Upbit",
    "whitebit": "WhiteBIT", "poloniex": "Poloniex", "bingx": "BingX",
    "lbank": "LBank", "bitstamp": "Bitstamp", "gemini": "Gemini",
    "bitrue": "Bitrue", "xt": "XT", "huobi": "Huobi"
}

EXTRA_OPTS = {
    "bybit": {"options": {"defaultType": "spot"}},
    "okx": {"options": {"defaultType": "spot"}},
    "bingx": {"options": {"defaultType": "spot"}},
    "mexc": {"options": {"defaultType": "spot"}},
    "bitrue": {"options": {"defaultType": "spot"}},
    "xt": {"options": {"defaultType": "spot"}},
    "huobi": {"options": {"defaultType": "spot"}},
}

USD_QUOTES = {"USDT", "USD", "USDC", "BUSD"}
LOW_FEE_CHAIN_PRIORITY = ["TRC20", "BSC", "SOL", "MATIC", "ARB", "OP", "TON", "AVAX", "ETH"]

LEV_REGEX = re.compile(r"\b(\d+[LS]|UP|DOWN|BULL|BEAR)\b", re.IGNORECASE)

CHAIN_ALIASES = {
    "BEP20": "BSC", "BSC": "BEP20",
    "MATIC": "Polygon", "Polygon": "MATIC",
    "OP": "Optimism", "Optimism": "OP",
    "ARB": "Arbitrum", "Arbitrum": "ARB",
    "TRC20": "TRON", "TRON": "TRC20",
}

def normalize_chain(name: str) -> str:
    n = name.upper().strip()
    return CHAIN_ALIASES.get(n, n)

# ====================== RUNTIME STATE ======================
op_cache = {}
lifetime_history = {}
last_seen_keys = set()

# ====================== HELPERS (unchanged) ======================
def parse_symbol(symbol: str):
    base, quote = symbol.split("/")[0], symbol.split("/")[1].split(":")[0]
    return base, quote

def market_price_from_ticker(t):
    if not t: return None
    last = t.get("last")
    if last is not None:
        try: return float(last)
        except: pass
    bid, ask = t.get("bid"), t.get("ask")
    if bid is not None and ask is not None:
        try: return (float(bid) + float(ask)) / 2.0
        except: return None
    return None

def is_ticker_fresh(t, max_age_sec=300):
    ts = t.get("timestamp")
    if ts is None: return True
    return (int(time.time() * 1000) - int(ts)) <= max_age_sec * 1000

def fmt_usd(x):
    try:
        x = float(x or 0)
        if x >= 1e9: return f"${x/1e9:.2f}B"
        if x >= 1e6: return f"${x/1e6:.2f}M"
        if x >= 1e3: return f"${x/1e3:.0f}K"
        return f"${x:,.0f}"
    except: return "$0"

def secs_to_label(secs):
    return f"{int(secs)}s" if secs < 90 else f"{secs/60:.1f}m"

def update_lifetime_for_disappeared(current_keys):
    global last_seen_keys
    gone = last_seen_keys - set(current_keys)
    for key in gone:
        trail = op_cache.get(key, [])
        if trail:
            duration = trail[-1][0] - trail[0][0]
            if duration > 0:
                lifetime_history.setdefault(key, []).append(duration)
    last_seen_keys = set(current_keys)

def stability_and_expiry(key, current_profit):
    now = time.time()
    trail = op_cache.get(key, [])
    if not trail:
        op_cache[key] = [(now, current_profit)]
        return "⏳ new", "\~unknown"
    trail.append((now, current_profit))
    op_cache[key] = trail[-30:]
    duration = trail[-1][0] - trail[0][0]
    observed = f"⏳ {secs_to_label(duration)} observed"
    hist = lifetime_history.get(key, [])
    if not hist:
        expiry = "\~unknown"
    else:
        avg = sum(hist) / len(hist)
        remaining = avg - duration
        expiry = "⚠️ past avg" if remaining <= 0 else f"\~{secs_to_label(remaining)} left"
    return observed, expiry

INFO_VOLUME_CANDIDATES = [
    "quoteVolume", "baseVolume", "vol", "vol24h", "volCcy24h", "volValue",
    "turnover", "turnover24h", "quoteVolume24h", "amount", "value",
    "acc_trade_price_24h", "quote_volume_24h", "base_volume_24h",
]

def safe_usd_volume(ex_id, symbol, ticker, price, all_tickers):
    try:
        base, quote = parse_symbol(symbol)
        q_upper = quote.upper()
        qvol = ticker.get("quoteVolume")
        bvol = ticker.get("baseVolume")
        if q_upper in USD_QUOTES and qvol:
            return float(qvol)
        if bvol and price:
            return float(bvol) * float(price)

        info = ticker.get("info") or {}
        raw = None
        for k in INFO_VOLUME_CANDIDATES:
            val = info.get(k)
            if val is not None:
                try:
                    fval = float(val)
                    if fval > 0:
                        raw = fval
                        break
                except: continue
        if raw is not None:
            if q_upper in USD_QUOTES:
                return float(raw)
            for conv in ["USDT", "USDC", "USD"]:
                conv_sym = f"{q_upper}/{conv}"
                conv_t = all_tickers.get(conv_sym)
                conv_px = market_price_from_ticker(conv_t)
                if conv_px:
                    return float(raw) * float(conv_px)
        if qvol:
            for conv in ["USDT", "USDC", "USD"]:
                conv_sym = f"{q_upper}/{conv}"
                conv_t = all_tickers.get(conv_sym)
                conv_px = market_price_from_ticker(conv_t)
                if conv_px:
                    return float(qvol) * float(conv_px)
        return 0.0
    except:
        return 0.0

def symbol_ok(ex_obj, symbol):
    m = ex_obj.markets.get(symbol, {})
    if not m or not m.get("spot", True): return False
    base, quote = parse_symbol(symbol)
    if quote.upper() not in USD_QUOTES: return False
    if LEV_REGEX.search(symbol): return False
    if m.get("active") is False: return False
    return True

def choose_common_chain(ex1, ex2, coin, exclude_chains, include_all_chains):
    try:
        c1 = ex1.currencies.get(coin, {}) or {}
        c2 = ex2.currencies.get(coin, {}) or {}
        nets1_raw = c1.get("networks", {}) or {}
        nets2_raw = c2.get("networks", {}) or {}

        nets1_norm = {normalize_chain(k): (k, v) for k, v in nets1_raw.items()}
        nets2_norm = {normalize_chain(k): (k, v) for k, v in nets2_raw.items()}

        common_norm = set(nets1_norm.keys()) & set(nets2_norm.keys())
        if not common_norm:
            return "❌ No chain", "❌", "❌"

        exclude_norm = {normalize_chain(c) for c in exclude_chains} if not include_all_chains else set()
        preferred_norm = [normalize_chain(n) for n in LOW_FEE_CHAIN_PRIORITY if normalize_chain(n) not in exclude_norm]

        best_norm = next((p for p in preferred_norm if p in common_norm), None)
        if not best_norm:
            candidates = [c for c in common_norm if c not in exclude_norm]
            if not candidates: return "❌ No chain", "❌", "❌"
            best_norm = sorted(candidates)[0]

        orig_key1, info1 = nets1_norm[best_norm]
        orig_key2, info2 = nets2_norm[best_norm]
        w_ok = "✅" if info1.get("withdraw") else "❌"
        d_ok = "✅" if info2.get("deposit") else "❌"
        return best_norm, w_ok, d_ok
    except:
        return "❌ Unknown", "❌", "❌"

def fetch_tickers_safe(ex, name):
    for attempt in range(3):
        try:
            return ex.fetch_tickers()
        except Exception as e:
            if attempt == 2:
                return {}
            time.sleep((2 ** attempt) * 1.5)
    return {}

# ====================== CORE SCAN (with safe loading) ======================
def run_scan(settings, logger):
    buy = settings.get("buy_exchanges", [])
    sell = settings.get("sell_exchanges", [])
    min_p = settings.get("min_profit", 1.0)
    max_p = settings.get("max_profit", 20.0)
    min_vol = settings.get("min_24h_vol_usd", 100000.0)
    exclude = settings.get("exclude_chains", ["ETH"])
    include_all = settings.get("include_all_chains", False)

    logger("🚀 Starting scan")
    logger(f"Buy: {buy} | Sell: {sell}")

    if not buy or not sell:
        logger("❌ Need at least one buy & sell exchange")
        return []

    ex_objs = {}
    for ex_id in set(buy + sell):
        try:
            opts = {"enableRateLimit": True, "timeout": 15000}
            opts.update(EXTRA_OPTS.get(ex_id, {}))
            ex = getattr(ccxt, ex_id)(opts)
            ex.load_markets()
            ex_objs[ex_id] = ex
            logger(f"✓ Loaded markets → {EXCHANGE_NAMES.get(ex_id, ex_id)}")
        except Exception as e:
            logger(f"⚠️ Skipped {EXCHANGE_NAMES.get(ex_id, ex_id)} (load failed)")
            continue

    bulk = {}
    for ex_id, ex in ex_objs.items():
        try:
            bulk[ex_id] = fetch_tickers_safe(ex, EXCHANGE_NAMES.get(ex_id, ex_id))
            logger(f"✓ Fetched tickers → {EXCHANGE_NAMES.get(ex_id, ex_id)}")
        except Exception as e:
            logger(f"⚠️ Skipped tickers for {EXCHANGE_NAMES.get(ex_id, ex_id)}")
            continue

    results = []
    current_keys = []

    for b_id in buy:
        for s_id in sell:
            if b_id == s_id: continue
            if b_id not in ex_objs or s_id not in ex_objs: continue
            b_ex = ex_objs[b_id]
            s_ex = ex_objs[s_id]
            b_tk = bulk.get(b_id, {})
            s_tk = bulk.get(s_id, {})

            common = set(b_ex.markets) & set(s_ex.markets)
            symbols = [s for s in common if symbol_ok(b_ex, s) and symbol_ok(s_ex, s)]

            def vol_score(sym):
                bt = b_tk.get(sym)
                st_ = s_tk.get(sym)
                pb = market_price_from_ticker(bt) or 0
                ps = market_price_from_ticker(st_) or 0
                return safe_usd_volume(b_id, sym, bt, pb, b_tk) + safe_usd_volume(s_id, sym, st_, ps, s_tk)
            symbols.sort(key=vol_score, reverse=True)
            symbols = symbols[:1000]

            for sym in symbols:
                bt = b_tk.get(sym)
                st_ = s_tk.get(sym)
                if not bt or not st_ or not is_ticker_fresh(bt) or not is_ticker_fresh(st_):
                    continue

                bp = market_price_from_ticker(bt)
                sp = market_price_from_ticker(st_)
                if not bp or not sp: continue

                if abs(sp - bp) / bp > 0.5: continue

                b_fee = b_ex.markets.get(sym, {}).get("taker", 0.001)
                s_fee = s_ex.markets.get(sym, {}).get("taker", 0.001)

                spread = (sp - bp) / bp * 100
                profit = spread - (b_fee * 100 + s_fee * 100)
                if profit < min_p or profit > max_p: continue

                b_vol = safe_usd_volume(b_id, sym, bt, bp, b_tk)
                s_vol = safe_usd_volume(s_id, sym, st_, sp, s_tk)
                if b_vol < min_vol or s_vol < min_vol: continue

                base, quote = parse_symbol(sym)
                chain, w_ok, d_ok = choose_common_chain(b_ex, s_ex, base, exclude, include_all)
                if not include_all and (chain.startswith("❌") or normalize_chain(chain) in {normalize_chain(c) for c in exclude}):
                    continue
                if w_ok != "✅" or d_ok != "✅": continue

                key = f"{sym}|{b_id}>{s_id}"
                current_keys.append(key)
                obs, exp = stability_and_expiry(key, profit)

                results.append({
                    "Pair": sym,
                    "Quote": quote,
                    "Buy@": EXCHANGE_NAMES.get(b_id, b_id),
                    "Buy Price": round(bp, 10),
                    "Sell@": EXCHANGE_NAMES.get(s_id, s_id),
                    "Sell Price": round(sp, 10),
                    "Spread %": round(spread, 4),
                    "Profit % After Fees": round(profit, 4),
                    "Buy Vol (24h)": fmt_usd(b_vol),
                    "Sell Vol (24h)": fmt_usd(s_vol),
                    "Withdraw?": w_ok,
                    "Deposit?": d_ok,
                    "Blockchain": chain,
                    "Stability": obs,
                    "Est. Expiry": exp,
                })

    update_lifetime_for_disappeared(current_keys)
    logger(f"✅ Scan finished — {len(results)} opportunities")
    return results

# ====================== HTML — EXACT STREAMLIT MATCH ======================
<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<title>Cross-Exchange Arbitrage Scanner</title>
<script src="https://cdn.tailwindcss.com"></script>
<style>
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap');
    
    * { box-sizing: border-box; }
    
    body { 
        background: linear-gradient(135deg, #0f0f1e 0%, #1a1a2e 100%);
        color: #e8eaed;
        font-family: 'Inter', system-ui, sans-serif;
        min-height: 100vh;
    }
    
    .glass-card {
        background: rgba(26, 26, 46, 0.7);
        backdrop-filter: blur(20px);
        border: 1px solid rgba(255, 255, 255, 0.05);
        border-radius: 16px;
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.4);
    }
    
    .exchange-selector {
        background: rgba(15, 15, 30, 0.6);
        border: 2px solid transparent;
        border-radius: 12px;
        transition: all 0.3s ease;
        max-height: 400px;
        overflow-y: auto;
    }
    
    .exchange-selector::-webkit-scrollbar {
        width: 8px;
    }
    
    .exchange-selector::-webkit-scrollbar-track {
        background: rgba(255, 255, 255, 0.05);
        border-radius: 4px;
    }
    
    .exchange-selector::-webkit-scrollbar-thumb {
        background: rgba(139, 92, 246, 0.5);
        border-radius: 4px;
    }
    
    .exchange-selector:focus {
        border-color: rgba(139, 92, 246, 0.5);
        outline: none;
        box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.1);
    }
    
    .exchange-selector option {
        padding: 12px;
        background: #1a1a2e;
        border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        cursor: pointer;
    }
    
    .exchange-selector option:checked {
        background: linear-gradient(90deg, rgba(139, 92, 246, 0.3) 0%, rgba(59, 130, 246, 0.3) 100%);
        color: #fff;
        font-weight: 600;
    }
    
    .exchange-selector option:hover {
        background: rgba(139, 92, 246, 0.2);
    }
    
    .input-field {
        background: rgba(15, 15, 30, 0.6);
        border: 2px solid rgba(255, 255, 255, 0.08);
        color: #e8eaed;
        border-radius: 10px;
        padding: 12px 16px;
        transition: all 0.3s ease;
        font-weight: 500;
    }
    
    .input-field:focus {
        border-color: rgba(139, 92, 246, 0.5);
        outline: none;
        box-shadow: 0 0 0 3px rgba(139, 92, 246, 0.1);
        background: rgba(15, 15, 30, 0.8);
    }
    
    .btn-primary {
        background: linear-gradient(135deg, #8b5cf6 0%, #3b82f6 100%);
        border: none;
        color: white;
        font-weight: 600;
        transition: all 0.3s ease;
        box-shadow: 0 4px 16px rgba(139, 92, 246, 0.4);
    }
    
    .btn-primary:hover {
        transform: translateY(-2px);
        box-shadow: 0 6px 24px rgba(139, 92, 246, 0.5);
    }
    
    .btn-primary:active {
        transform: translateY(0);
    }
    
    .btn-secondary {
        background: rgba(255, 255, 255, 0.05);
        border: 2px solid rgba(255, 255, 255, 0.1);
        color: #e8eaed;
        font-weight: 600;
        transition: all 0.3s ease;
    }
    
    .btn-secondary:hover {
        background: rgba(255, 255, 255, 0.1);
        border-color: rgba(255, 255, 255, 0.2);
    }
    
    .data-table {
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
    }
    
    .data-table thead {
        position: sticky;
        top: 0;
        z-index: 10;
    }
    
    .data-table th {
        background: linear-gradient(180deg, rgba(26, 26, 46, 0.95) 0%, rgba(15, 15, 30, 0.95) 100%);
        backdrop-filter: blur(10px);
        color: #a8b3cf;
        padding: 16px 12px;
        text-align: left;
        font-weight: 600;
        font-size: 12px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        border-bottom: 2px solid rgba(139, 92, 246, 0.3);
    }
    
    .data-table td {
        padding: 16px 12px;
        border-bottom: 1px solid rgba(255, 255, 255, 0.05);
        font-size: 14px;
    }
    
    .data-table tbody tr {
        background: rgba(26, 26, 46, 0.3);
        transition: all 0.2s ease;
    }
    
    .data-table tbody tr:hover {
        background: rgba(139, 92, 246, 0.1);
        transform: scale(1.01);
    }
    
    .status-badge {
        display: inline-flex;
        align-items: center;
        padding: 4px 12px;
        border-radius: 20px;
        font-size: 11px;
        font-weight: 700;
        letter-spacing: 0.3px;
    }
    
    .badge-success {
        background: rgba(16, 185, 129, 0.15);
        color: #10b981;
        border: 1px solid rgba(16, 185, 129, 0.3);
    }
    
    .badge-danger {
        background: rgba(239, 68, 68, 0.15);
        color: #ef4444;
        border: 1px solid rgba(239, 68, 68, 0.3);
    }
    
    .badge-info {
        background: rgba(59, 130, 246, 0.15);
        color: #3b82f6;
        border: 1px solid rgba(59, 130, 246, 0.3);
    }
    
    .profit-positive {
        color: #10b981;
        font-weight: 700;
    }
    
    .profit-negative {
        color: #ef4444;
        font-weight: 700;
    }
    
    .spread-value {
        color: #60a5fa;
        font-weight: 700;
    }
    
    .log-container {
        background: rgba(0, 0, 0, 0.4);
        border-radius: 12px;
        padding: 16px;
        font-family: 'Courier New', monospace;
        font-size: 13px;
        line-height: 1.6;
        max-height: 500px;
        overflow-y: auto;
    }
    
    .log-container::-webkit-scrollbar {
        width: 8px;
    }
    
    .log-container::-webkit-scrollbar-track {
        background: rgba(255, 255, 255, 0.05);
        border-radius: 4px;
    }
    
    .log-container::-webkit-scrollbar-thumb {
        background: rgba(139, 92, 246, 0.5);
        border-radius: 4px;
    }
    
    .log-entry {
        color: #10b981;
        margin-bottom: 4px;
    }
    
    .log-timestamp {
        color: #6b7280;
        margin-right: 8px;
    }
    
    .metric-card {
        background: linear-gradient(135deg, rgba(139, 92, 246, 0.1) 0%, rgba(59, 130, 246, 0.1) 100%);
        border: 1px solid rgba(139, 92, 246, 0.2);
        border-radius: 12px;
        padding: 20px;
        text-align: center;
    }
    
    .metric-value {
        font-size: 32px;
        font-weight: 700;
        background: linear-gradient(135deg, #8b5cf6 0%, #3b82f6 100%);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        background-clip: text;
    }
    
    .metric-label {
        font-size: 12px;
        color: #a8b3cf;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        margin-top: 8px;
    }
    
    .section-header {
        font-size: 14px;
        font-weight: 600;
        color: #8b5cf6;
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-bottom: 12px;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    
    .glow-effect {
        box-shadow: 0 0 20px rgba(139, 92, 246, 0.3);
    }
    
    .mono-font {
        font-family: 'Courier New', monospace;
    }
    
    .text-right {
        text-align: right;
    }
    
    .empty-state {
        padding: 80px 40px;
        text-align: center;
        color: #6b7280;
    }
    
    .empty-state-icon {
        font-size: 64px;
        margin-bottom: 16px;
        opacity: 0.3;
    }
</style>
</head>
<body class="p-8">

<!-- Header -->
<div class="mb-10">
    <h1 class="text-4xl font-bold mb-2 flex items-center gap-4">
        <span class="text-5xl">🌍</span>
        <span class="bg-gradient-to-r from-purple-400 to-blue-400 bg-clip-text text-transparent">
            Cross-Exchange Arbitrage Scanner
        </span>
    </h1>
    <p class="text-gray-400 ml-16">Real-time cryptocurrency arbitrage opportunity detection</p>
</div>

<!-- Configuration Panel -->
<div class="glass-card p-8 mb-8">
    <div class="grid lg:grid-cols-3 gap-8">
        
        <!-- Buy Exchanges -->
        <div>
            <div class="section-header">
                <span>📥</span> Buy Exchanges
            </div>
            <p class="text-xs text-gray-400 mb-3">Select up to 10 exchanges to buy from</p>
            <select id="buy" multiple class="exchange-selector w-full p-3"></select>
            <p class="text-xs text-gray-500 mt-2">
                <span id="buyCount">0</span> selected
            </p>
        </div>
        
        <!-- Sell Exchanges -->
        <div>
            <div class="section-header">
                <span>📤</span> Sell Exchanges
            </div>
            <p class="text-xs text-gray-400 mb-3">Select up to 10 exchanges to sell on</p>
            <select id="sell" multiple class="exchange-selector w-full p-3"></select>
            <p class="text-xs text-gray-500 mt-2">
                <span id="sellCount">0</span> selected
            </p>
        </div>
        
        <!-- Filters -->
        <div class="space-y-4">
            <div class="section-header">
                <span>⚙️</span> Filters & Settings
            </div>
            
            <!-- Profit Range -->
            <div class="grid grid-cols-2 gap-4">
                <div>
                    <label class="block text-xs text-gray-400 mb-2">Min Profit %</label>
                    <input id="minProfit" type="number" step="0.1" value="1.0" class="input-field w-full">
                </div>
                <div>
                    <label class="block text-xs text-gray-400 mb-2">Max Profit %</label>
                    <input id="maxProfit" type="number" step="0.1" value="20.0" class="input-field w-full">
                </div>
            </div>
            
            <!-- Volume -->
            <div>
                <label class="block text-xs text-gray-400 mb-2">Min 24h Volume (USD)</label>
                <input id="minVol" type="number" value="100000" class="input-field w-full">
            </div>
            
            <!-- Exclude Chains -->
            <div>
                <label class="block text-xs text-gray-400 mb-2">Exclude Blockchains</label>
                <select id="exclude" multiple size="4" class="exchange-selector w-full p-2 text-sm">
                    <option value="ETH">Ethereum (ETH)</option>
                    <option value="TRC20">TRON (TRC20)</option>
                    <option value="BSC">BNB Chain (BSC)</option>
                    <option value="SOL">Solana (SOL)</option>
                    <option value="MATIC">Polygon (MATIC)</option>
                    <option value="ARB">Arbitrum (ARB)</option>
                    <option value="OP">Optimism (OP)</option>
                    <option value="TON">TON</option>
                    <option value="AVAX">Avalanche (AVAX)</option>
                </select>
            </div>
            
            <!-- Include All Chains -->
            <label class="flex items-center gap-3 cursor-pointer">
                <input id="includeAll" type="checkbox" checked class="w-5 h-5 rounded border-2 border-purple-500 bg-transparent checked:bg-purple-500">
                <span class="text-sm text-gray-300">Include all available chains</span>
            </label>
        </div>
    </div>
</div>

<!-- Action Buttons -->
<div class="flex gap-4 mb-8">
    <button onclick="startScan()" class="btn-primary flex-1 py-4 rounded-xl text-lg flex items-center justify-center gap-3">
        <span class="text-2xl">🚀</span>
        <span>START SCANNING</span>
    </button>
    <button onclick="downloadCSV()" class="btn-secondary px-8 rounded-xl flex items-center gap-3">
        <span>⬇️</span>
        <span>Export CSV</span>
    </button>
</div>

<!-- Metrics Dashboard -->
<div id="metricsPanel" class="grid grid-cols-4 gap-6 mb-8" style="display: none;">
    <div class="metric-card">
        <div class="metric-value" id="totalOpps">0</div>
        <div class="metric-label">Total Opportunities</div>
    </div>
    <div class="metric-card">
        <div class="metric-value" id="avgProfit">0%</div>
        <div class="metric-label">Avg Profit</div>
    </div>
    <div class="metric-card">
        <div class="metric-value" id="maxProfit">0%</div>
        <div class="metric-label">Max Profit</div>
    </div>
    <div class="metric-card">
        <div class="metric-value" id="totalPairs">0</div>
        <div class="metric-label">Unique Pairs</div>
    </div>
</div>

<!-- Results Section -->
<div class="grid lg:grid-cols-12 gap-8">
    
    <!-- Log Panel -->
    <div class="lg:col-span-4 glass-card p-6">
        <div class="section-header mb-4">
            <span>📊</span> Live Activity Log
        </div>
        <div id="log" class="log-container"></div>
    </div>
    
    <!-- Results Table -->
    <div class="lg:col-span-8 glass-card overflow-hidden">
        <div class="px-6 py-5 border-b border-white/5 flex justify-between items-center">
            <div class="flex items-center gap-3">
                <span class="text-lg font-semibold">Arbitrage Opportunities</span>
                <span id="count" class="status-badge badge-info">0 found</span>
            </div>
            <div id="lastScan" class="text-sm text-gray-500">Never scanned</div>
        </div>
        <div id="tableContainer" class="overflow-auto" style="max-height: 600px;"></div>
    </div>
    
</div>

<script>
const exList = {{ TOP_EXCHANGES | tojson }};
const exNames = {{ EXCHANGE_NAMES | tojson }};
let currentResults = [];

function populateSelects() {
    const buy = document.getElementById('buy');
    const sell = document.getElementById('sell');
    
    exList.forEach(ex => {
        let optBuy = document.createElement('option');
        optBuy.value = ex;
        optBuy.text = exNames[ex] || ex;
        buy.appendChild(optBuy);
        
        let optSell = document.createElement('option');
        optSell.value = ex;
        optSell.text = exNames[ex] || ex;
        sell.appendChild(optSell);
    });
    
    // Default selections
    if (buy.options.length > 0) buy.options[0].selected = true;
    if (sell.options.length > 1) sell.options[1].selected = true;
    
    updateSelectionCounts();
    
    // Add event listeners for selection counts
    buy.addEventListener('change', updateSelectionCounts);
    sell.addEventListener('change', updateSelectionCounts);
}

function updateSelectionCounts() {
    const buyCount = document.getElementById('buy').selectedOptions.length;
    const sellCount = document.getElementById('sell').selectedOptions.length;
    document.getElementById('buyCount').textContent = buyCount;
    document.getElementById('sellCount').textContent = sellCount;
}

function log(msg) {
    const logEl = document.getElementById('log');
    const ts = new Date().toLocaleTimeString([], {hour:'2-digit', minute:'2-digit', second:'2-digit'});
    const entry = document.createElement('div');
    entry.className = 'log-entry';
    entry.innerHTML = `<span class="log-timestamp">[${ts}]</span>${msg}`;
    logEl.appendChild(entry);
    logEl.scrollTop = logEl.scrollHeight;
}

function updateMetrics(results) {
    if (results.length === 0) {
        document.getElementById('metricsPanel').style.display = 'none';
        return;
    }
    
    document.getElementById('metricsPanel').style.display = 'grid';
    
    const profits = results.map(r => r["Profit % After Fees"]);
    const avgProfit = (profits.reduce((a, b) => a + b, 0) / profits.length).toFixed(2);
    const maxProfitVal = Math.max(...profits).toFixed(2);
    const uniquePairs = new Set(results.map(r => r.Pair)).size;
    
    document.getElementById('totalOpps').textContent = results.length;
    document.getElementById('avgProfit').textContent = avgProfit + '%';
    document.getElementById('maxProfit').textContent = maxProfitVal + '%';
    document.getElementById('totalPairs').textContent = uniquePairs;
}

function renderTable(results) {
    currentResults = results;
    document.getElementById('count').textContent = `${results.length} found`;
    updateMetrics(results);
    
    if (results.length === 0) {
        document.getElementById('tableContainer').innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">🔍</div>
                <h3 class="text-xl font-semibold mb-2">No Opportunities Found</h3>
                <p class="text-gray-500">Try adjusting your filters or selecting different exchanges</p>
            </div>
        `;
        return;
    }
    
    let html = `
        <table class="data-table">
            <thead>
                <tr>
                    <th class="text-right">#</th>
                    <th>Pair</th>
                    <th>Quote</th>
                    <th>Buy @</th>
                    <th class="text-right">Buy Price</th>
                    <th>Sell @</th>
                    <th class="text-right">Sell Price</th>
                    <th class="text-right">Spread %</th>
                    <th class="text-right">Net Profit %</th>
                    <th class="text-right">Buy Vol</th>
                    <th class="text-right">Sell Vol</th>
                    <th>W</th>
                    <th>D</th>
                    <th>Chain</th>
                    <th>Stability</th>
                    <th>Expiry</th>
                </tr>
            </thead>
            <tbody>
    `;
    
    results.forEach((r, i) => {
        const profitClass = r["Profit % After Fees"] >= 0 ? "profit-positive" : "profit-negative";
        const wBadge = r["Withdraw?"] === "✅" ? "badge-success" : "badge-danger";
        const dBadge = r["Deposit?"] === "✅" ? "badge-success" : "badge-danger";
        
        html += `
            <tr>
                <td class="text-right mono-font" style="color: #6b7280;">${i + 1}</td>
                <td class="mono-font" style="font-weight: 600;">${r.Pair}</td>
                <td style="color: #a8b3cf;">${r.Quote}</td>
                <td>${r["Buy@"]}</td>
                <td class="text-right mono-font">${r["Buy Price"]}</td>
                <td>${r["Sell@"]}</td>
                <td class="text-right mono-font">${r["Sell Price"]}</td>
                <td class="text-right spread-value">${r["Spread %"]}%</td>
                <td class="text-right ${profitClass}">${r["Profit % After Fees"]}%</td>
                <td class="text-right mono-font">${r["Buy Vol (24h)"]}</td>
                <td class="text-right mono-font">${r["Sell Vol (24h)"]}</td>
                <td><span class="status-badge ${wBadge}">${r["Withdraw?"]}</span></td>
                <td><span class="status-badge ${dBadge}">${r["Deposit?"]}</span></td>
                <td><span class="status-badge badge-info">${r.Blockchain}</span></td>
                <td style="font-size: 12px; color: #9ca3af;">${r.Stability}</td>
                <td style="font-size: 12px; color: #9ca3af;">${r["Est. Expiry"]}</td>
            </tr>
        `;
    });
    
    html += `</tbody></table>`;
    document.getElementById('tableContainer').innerHTML = html;
}

async function startScan() {
    const settings = {
        buy_exchanges: Array.from(document.getElementById('buy').selectedOptions).map(o => o.value),
        sell_exchanges: Array.from(document.getElementById('sell').selectedOptions).map(o => o.value),
        min_profit: parseFloat(document.getElementById('minProfit').value),
        max_profit: parseFloat(document.getElementById('maxProfit').value),
        min_24h_vol_usd: parseFloat(document.getElementById('minVol').value),
        exclude_chains: Array.from(document.getElementById('exclude').selectedOptions).map(o => o.value),
        include_all_chains: document.getElementById('includeAll').checked
    };
    
    if (!settings.buy_exchanges.length || !settings.sell_exchanges.length) {
        log("❌ Error: Please select at least one buy and one sell exchange");
        return;
    }
    
    if (settings.buy_exchanges.length > 10 || settings.sell_exchanges.length > 10) {
        log("❌ Error: Maximum 10 exchanges per side");
        return;
    }
    
    log("🔍 Starting scan...");
    document.getElementById('tableContainer').innerHTML = `
        <div class="empty-state">
            <div class="empty-state-icon">⏳</div>
            <h3 class="text-xl font-semibold mb-2">Scanning exchanges...</h3>
            <p class="text-gray-500">Please wait while we analyze arbitrage opportunities</p>
        </div>
    `;
    
    try {
        const res = await fetch("/api/scan", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(settings)
        });
        
        const data = await res.json();
        
        data.logs.forEach(msg => log(msg));
        renderTable(data.results);
        
        const now = new Date().toLocaleTimeString([], {
            hour: '2-digit',
            minute: '2-digit',
            second: '2-digit'
        });
        document.getElementById('lastScan').textContent = `Last scan: ${now}`;
        
    } catch(e) {
        log(`❌ Error: ${e.message}`);
        document.getElementById('tableContainer').innerHTML = `
            <div class="empty-state">
                <div class="empty-state-icon">⚠️</div>
                <h3 class="text-xl font-semibold mb-2">Scan Failed</h3>
                <p class="text-gray-500">${e.message}</p>
            </div>
        `;
    }
}

function downloadCSV() {
    if (currentResults.length === 0) {
        log("⚠️ No data to export");
        return;
    }
    
    const headers = Object.keys(currentResults[0]);
    const csv = [
        headers.join(','),
        ...currentResults.map(row => 
            headers.map(header => {
                const value = row[header];
                return typeof value === 'string' && value.includes(',') 
                    ? `"${value}"` 
                    : value;
            }).join(',')
        )
    ].join('\n');
    
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `arbitrage_opportunities_${Date.now()}.csv`;
    a.click();
    window.URL.revokeObjectURL(url);
    
    log("✅ CSV exported successfully");
}

window.onload = () => {
    populateSelects();
    log("✨ Scanner initialized and ready");
    log("💡 Select your exchanges and click START SCANNING to begin");
};
</script>

</body>
</html>
        

# ====================== ROUTES ======================
@app.route('/')
def index():
    return render_template_string(HTML, TOP_EXCHANGES=TOP_EXCHANGES, EXCHANGE_NAMES=EXCHANGE_NAMES)

@app.route('/api/scan', methods=['POST'])
def api_scan():
    settings = request.get_json() or {}
    logs = []

    def logger(msg):
        ts = time.strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line)
        logs.append(line)

    results = run_scan(settings, logger)
    save_settings(settings)
    return jsonify({"results": results, "logs": logs})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
