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
    "bitstamp", "gemini", "bitrue", "xt", "huobi", "bitmart"
]

EXCHANGE_NAMES = {
    "binance": "Binance", "okx": "OKX", "coinbase": "Coinbase",
    "kraken": "Kraken", "bybit": "Bybit", "kucoin": "KuCoin",
    "mexc": "MEXC", "bitfinex": "Bitfinex", "bitget": "Bitget",
    "gateio": "Gate.io", "crypto_com": "Crypto.com", "upbit": "Upbit",
    "whitebit": "WhiteBIT", "poloniex": "Poloniex", "bingx": "BingX",
    "lbank": "LBank", "bitstamp": "Bitstamp", "gemini": "Gemini",
    "bitrue": "Bitrue", "xt": "XT", "huobi": "Huobi", "bitmart": "BitMart"
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

# ====================== HELPERS ======================
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
        return "⏳ new", "~unknown"
    trail.append((now, current_profit))
    op_cache[key] = trail[-30:]
    duration = trail[-1][0] - trail[0][0]
    observed = f"⏳ {secs_to_label(duration)} observed"
    hist = lifetime_history.get(key, [])
    if not hist:
        expiry = "~unknown"
    else:
        avg = sum(hist) / len(hist)
        remaining = avg - duration
        expiry = "⚠️ past avg" if remaining <= 0 else f"~{secs_to_label(remaining)} left"
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

# ====================== CORE SCAN ======================
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

    # Load exchanges
    ex_objs = {}
    for ex_id in set(buy + sell):
        opts = {"enableRateLimit": True, "timeout": 15000}
        opts.update(EXTRA_OPTS.get(ex_id, {}))
        ex = getattr(ccxt, ex_id)(opts)
        ex.load_markets()
        ex_objs[ex_id] = ex
        logger(f"✓ Loaded markets → {EXCHANGE_NAMES.get(ex_id, ex_id)}")

    # Fetch tickers
    bulk = {}
    for ex_id, ex in ex_objs.items():
        bulk[ex_id] = fetch_tickers_safe(ex, EXCHANGE_NAMES.get(ex_id, ex_id))
        logger(f"✓ Fetched tickers → {EXCHANGE_NAMES.get(ex_id, ex_id)}")

    results = []
    current_keys = []

    for b_id in buy:
        for s_id in sell:
            if b_id == s_id: continue
            b_ex = ex_objs[b_id]
            s_ex = ex_objs[s_id]
            b_tk = bulk[b_id]
            s_tk = bulk[s_id]

            common = set(b_ex.markets) & set(s_ex.markets)
            symbols = [s for s in common if symbol_ok(b_ex, s) and symbol_ok(s_ex, s)]

            # Sort by liquidity
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

# ====================== HTML + TAILWIND ======================
HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Cross-Exchange Arbitrage Scanner</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<script src="https://cdn.tailwindcss.com"></script>
<link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.5.0/css/all.min.css">
<style>
    body { background: #0a0a0a; color: #e5e5e5; font-family: system-ui, sans-serif; }
    .card { background: #111; border: 1px solid #222; }
    .pill { padding: 4px 12px; border-radius: 9999px; font-size: 12px; font-weight: 700; }
    .pill-green { background: #1b5e20; color: #c8e6c9; }
    .pill-red { background: #7f1d1d; color: #fee2e2; }
    .pill-blue { background: #0d47a1; color: #e3f2fd; }
    table { border-collapse: collapse; }
    th, td { padding: 12px; text-align: left; border-bottom: 1px solid #222; }
    th { background: #1a1a1a; }
    tr:hover { background: #1f1f1f; }
    .log { font-family: monospace; font-size: 13px; line-height: 1.4; }
    .num { text-align: right; font-variant-numeric: tabular-nums; }
</style>
</head>
<body class="min-h-screen p-6">
<div class="max-w-screen-2xl mx-auto">
    <div class="flex justify-between items-center mb-8">
        <div>
            <h1 class="text-4xl font-bold tracking-tight flex items-center gap-3">
                <span class="text-emerald-400">🌍</span> Cross-Exchange Arbitrage Scanner
            </h1>
            <p class="text-zinc-500 mt-1">Real-time • Multi-exchange • Withdrawn-enabled only</p>
        </div>
        <div class="flex items-center gap-6 text-sm">
            <div id="status" class="flex items-center gap-2">
                <div class="w-2 h-2 bg-emerald-400 rounded-full animate-pulse"></div>
                <span class="text-emerald-400">Ready</span>
            </div>
            <button onclick="toggleAutoRefresh()" id="autoBtn"
                class="px-5 py-2 bg-zinc-800 hover:bg-zinc-700 rounded-xl flex items-center gap-2 transition">
                <i class="fas fa-sync"></i>
                <span id="autoText">Auto-refresh: OFF</span>
            </button>
        </div>
    </div>

    <!-- CONTROLS -->
    <div class="card rounded-3xl p-8 mb-8">
        <div class="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-8">
            <!-- Buy / Sell -->
            <div>
                <label class="block text-zinc-400 text-sm mb-2">Buy Exchanges (max 10)</label>
                <select id="buy" multiple class="w-full bg-zinc-900 border border-zinc-700 rounded-2xl p-4 h-48 focus:outline-none focus:border-emerald-500">
                    <!-- populated by JS -->
                </select>
            </div>
            <div>
                <label class="block text-zinc-400 text-sm mb-2">Sell Exchanges (max 10)</label>
                <select id="sell" multiple class="w-full bg-zinc-900 border border-zinc-700 rounded-2xl p-4 h-48 focus:outline-none focus:border-emerald-500">
                    <!-- populated by JS -->
                </select>
            </div>

            <!-- Filters -->
            <div class="grid grid-cols-2 gap-6">
                <div>
                    <label class="block text-zinc-400 text-sm mb-1">Min Profit %</label>
                    <input id="minProfit" type="number" step="0.1" value="1.0" class="w-full bg-zinc-900 border border-zinc-700 rounded-2xl px-4 py-3 focus:outline-none focus:border-emerald-500">
                </div>
                <div>
                    <label class="block text-zinc-400 text-sm mb-1">Max Profit %</label>
                    <input id="maxProfit" type="number" step="0.1" value="20.0" class="w-full bg-zinc-900 border border-zinc-700 rounded-2xl px-4 py-3 focus:outline-none focus:border-emerald-500">
                </div>
                <div>
                    <label class="block text-zinc-400 text-sm mb-1">Min 24h Vol (USD)</label>
                    <input id="minVol" type="number" value="100000" class="w-full bg-zinc-900 border border-zinc-700 rounded-2xl px-4 py-3 focus:outline-none focus:border-emerald-500">
                </div>
                <div>
                    <label class="block text-zinc-400 text-sm mb-2">Exclude Chains</label>
                    <select id="excludeChains" multiple class="w-full bg-zinc-900 border border-zinc-700 rounded-2xl p-4 h-28">
                        <option value="ETH">ETH</option>
                        <option value="TRC20">TRC20</option>
                        <option value="BSC">BSC</option>
                        <option value="SOL">SOL</option>
                        <option value="MATIC">MATIC</option>
                        <option value="ARB">ARB</option>
                        <option value="OP">OP</option>
                        <option value="TON">TON</option>
                        <option value="AVAX">AVAX</option>
                    </select>
                    <label class="flex items-center gap-2 mt-3 text-sm">
                        <input id="includeAll" type="checkbox" class="w-4 h-4 accent-emerald-400">
                        <span class="text-zinc-400">Include all chains</span>
                    </label>
                </div>
            </div>
        </div>

        <div class="flex gap-4 mt-10">
            <button onclick="startScan()" 
                class="flex-1 bg-emerald-500 hover:bg-emerald-600 transition py-4 rounded-3xl font-semibold text-lg flex items-center justify-center gap-3">
                <i class="fas fa-bolt"></i> SCAN NOW
            </button>
            <button onclick="downloadCSV()" 
                class="px-8 bg-zinc-800 hover:bg-zinc-700 transition rounded-3xl flex items-center gap-2">
                <i class="fas fa-download"></i> CSV
            </button>
        </div>
    </div>

    <!-- LOG + RESULTS -->
    <div class="grid grid-cols-1 lg:grid-cols-12 gap-8">
        <!-- Log -->
        <div class="lg:col-span-4">
            <div class="card rounded-3xl p-6 h-full flex flex-col">
                <div class="flex justify-between mb-4">
                    <div class="uppercase text-xs tracking-widest text-zinc-500">Live Log</div>
                    <button onclick="clearLog()" class="text-zinc-500 hover:text-white text-xs">CLEAR</button>
                </div>
                <div id="log" class="log flex-1 overflow-auto bg-black/50 p-5 rounded-2xl text-emerald-300 whitespace-pre-wrap"></div>
            </div>
        </div>

        <!-- Table -->
        <div class="lg:col-span-8">
            <div class="card rounded-3xl overflow-hidden">
                <div class="px-8 py-5 border-b border-zinc-800 flex justify-between items-center">
                    <div class="font-semibold">Opportunities <span id="count" class="text-emerald-400">(0)</span></div>
                    <div id="lastScan" class="text-xs text-zinc-500">Last scan: never</div>
                </div>
                <div class="overflow-auto max-h-[70vh]" id="tableContainer">
                    <!-- Table injected by JS -->
                </div>
            </div>
        </div>
    </div>
</div>

<script>
let autoRefresh = false;
let autoInterval = null;
let lastResults = [];

const exchanges = {{ TOP_EXCHANGES | tojson }};
const names = {{ EXCHANGE_NAMES | tojson }};

function populateSelects() {
    const buySel = document.getElementById('buy');
    const sellSel = document.getElementById('sell');
    exchanges.forEach(ex => {
        const opt1 = document.createElement('option');
        opt1.value = ex;
        opt1.textContent = names[ex] || ex;
        buySel.appendChild(opt1);

        const opt2 = opt1.cloneNode(true);
        sellSel.appendChild(opt2);
    });
}

function getSettings() {
    return {
        buy_exchanges: Array.from(document.getElementById('buy').selectedOptions).map(o => o.value),
        sell_exchanges: Array.from(document.getElementById('sell').selectedOptions).map(o => o.value),
        min_profit: parseFloat(document.getElementById('minProfit').value),
        max_profit: parseFloat(document.getElementById('maxProfit').value),
        min_24h_vol_usd: parseFloat(document.getElementById('minVol').value),
        exclude_chains: Array.from(document.getElementById('excludeChains').selectedOptions).map(o => o.value),
        include_all_chains: document.getElementById('includeAll').checked
    };
}

function log(msg) {
    const logEl = document.getElementById('log');
    const ts = new Date().toLocaleTimeString('en-US', {hour12:false});
    logEl.innerHTML += `<span class="text-zinc-500">[${ts}]</span> ${msg}<br>`;
    logEl.scrollTop = logEl.scrollHeight;
}

function clearLog() {
    document.getElementById('log').innerHTML = '';
}

function renderTable(results) {
    lastResults = results;
    document.getElementById('count').textContent = `(${results.length})`;

    let html = `
        <table class="w-full text-sm">
            <thead>
                <tr class="bg-zinc-900 text-zinc-400">
                    <th class="px-8 py-4 text-left">#</th>
                    <th class="px-8 py-4 text-left">Pair</th>
                    <th class="px-4 py-4">Quote</th>
                    <th class="px-8 py-4">Buy@</th>
                    <th class="px-8 py-4 text-right">Buy Price</th>
                    <th class="px-8 py-4">Sell@</th>
                    <th class="px-8 py-4 text-right">Sell Price</th>
                    <th class="px-8 py-4 text-right">Spread %</th>
                    <th class="px-8 py-4 text-right">Profit %</th>
                    <th class="px-8 py-4 text-right">Buy Vol</th>
                    <th class="px-8 py-4 text-right">Sell Vol</th>
                    <th class="px-6 py-4">WD</th>
                    <th class="px-6 py-4">DP</th>
                    <th class="px-8 py-4">Chain</th>
                    <th class="px-8 py-4">Stability</th>
                    <th class="px-8 py-4">Expiry</th>
                </tr>
            </thead>
            <tbody class="divide-y divide-zinc-800">
    `;

    results.forEach((r, i) => {
        const profitClass = r["Profit % After Fees"] >= 0 ? "text-emerald-400" : "text-red-400";
        html += `
            <tr class="hover:bg-zinc-900/70 transition">
                <td class="px-8 py-5 font-mono text-zinc-500">${i+1}</td>
                <td class="px-8 py-5 font-semibold">${r.Pair}</td>
                <td class="px-4 py-5 text-zinc-400">${r.Quote}</td>
                <td class="px-8 py-5">${r["Buy@"]}</td>
                <td class="px-8 py-5 text-right font-mono">${r["Buy Price"]}</td>
                <td class="px-8 py-5">${r["Sell@"]}</td>
                <td class="px-8 py-5 text-right font-mono">${r["Sell Price"]}</td>
                <td class="px-8 py-5 text-right font-mono text-sky-400">${r["Spread %"]}%</td>
                <td class="px-8 py-5 text-right font-mono \( {profitClass}"> \){r["Profit % After Fees"]}%</td>
                <td class="px-8 py-5 text-right font-mono">${r["Buy Vol (24h)"]}</td>
                <td class="px-8 py-5 text-right font-mono">${r["Sell Vol (24h)"]}</td>
                <td class="px-6 py-5"><span class="pill \( {r["Withdraw?"]==="✅"?"pill-green":"pill-red"}"> \){r["Withdraw?"]}</span></td>
                <td class="px-6 py-5"><span class="pill \( {r["Deposit?"]==="✅"?"pill-green":"pill-red"}"> \){r["Deposit?"]}</span></td>
                <td class="px-8 py-5"><span class="pill pill-blue">${r.Blockchain}</span></td>
                <td class="px-8 py-5 text-xs text-zinc-400">${r.Stability}</td>
                <td class="px-8 py-5 text-xs text-zinc-400">${r["Est. Expiry"]}</td>
            </tr>
        `;
    });

    html += `</tbody></table>`;
    document.getElementById('tableContainer').innerHTML = html;
}

function downloadCSV() {
    if (!lastResults.length) return;
    const headers = Object.keys(lastResults[0]).join(",");
    const rows = lastResults.map(r => Object.values(r).join(",")).join("\n");
    const csv = headers + "\n" + rows;
    const blob = new Blob([csv], {type: "text/csv"});
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "arbitrage_opportunities.csv";
    a.click();
}

async function startScan() {
    const settings = getSettings();
    log("🔍 Starting scan...");
    document.getElementById('status').innerHTML = `<div class="w-2 h-2 bg-amber-400 rounded-full animate-pulse"></div><span class="text-amber-400">Scanning...</span>`;

    try {
        const res = await fetch("/api/scan", {
            method: "POST",
            headers: {"Content-Type": "application/json"},
            body: JSON.stringify(settings)
        });
        const data = await res.json();

        data.logs.forEach(l => log(l));
        renderTable(data.results);

        document.getElementById('lastScan').textContent = `Last scan: ${new Date().toLocaleTimeString()}`;
        document.getElementById('status').innerHTML = `<div class="w-2 h-2 bg-emerald-400 rounded-full animate-pulse"></div><span class="text-emerald-400">Idle</span>`;
    } catch(e) {
        log("❌ Scan failed: " + e.message);
    }
}

function toggleAutoRefresh() {
    autoRefresh = !autoRefresh;
    const btn = document.getElementById('autoBtn');
    if (autoRefresh) {
        btn.classList.add("bg-emerald-600", "text-white");
        document.getElementById('autoText').textContent = "Auto-refresh: ON";
        autoInterval = setInterval(startScan, 20000);
        log("🔄 Auto-refresh enabled (every 20s)");
        startScan();
    } else {
        btn.classList.remove("bg-emerald-600", "text-white");
        document.getElementById('autoText').textContent = "Auto-refresh: OFF";
        clearInterval(autoInterval);
        log("⏹️ Auto-refresh disabled");
    }
}

// Init
window.onload = () => {
    populateSelects();
    // Load saved defaults if you want (localStorage)
    log("👋 Scanner ready. Click SCAN NOW to begin.");
};
</script>
</body>
</html>
"""

# ====================== FLASK ROUTES ======================
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
        print(line)                     # Render Logs
        logs.append(line)

    results = run_scan(settings, logger)
    save_settings(settings)
    return jsonify({"results": results, "logs": logs})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
