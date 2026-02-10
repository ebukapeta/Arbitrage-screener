import time, re, ccxt, json, os
import pandas as pd
import streamlit as st

# ------------------- Page Config -------------------
st.set_page_config(page_title="Cross-Exchange Arbitrage Scanner", layout="wide")

# ------------------- Custom Dark Theme -------------------
st.markdown("""
    <style>
    body, .stApp { background-color: #111111; color: #E0E0E0; }
    .stDataFrame th { background-color: #222 !important; color: #EEE !important; font-weight: 600; }
    .stDataFrame td { color: #EEE !important; }
    .stDataFrame tbody tr:nth-child(even) { background-color: #1E1E1E !important; }
    .stDataFrame tbody tr:hover { background-color: #2A2A2A !important; }
    .good { color: #4CAF50; font-weight: 600; }
    .bad { color: #FF5252; font-weight: 600; }
    .spread { color: #42A5F5; font-weight: 600; }
    .stButton>button {
        background-color: #1976D2; color: white; border-radius: 8px;
        padding: 0.6em 1.2em; font-size: 16px; font-weight: 600;
        border: none; cursor: pointer;
        transition: background-color 0.3s ease;
    }
    .stButton>button:hover { background-color: #1565C0; }
    </style>
""", unsafe_allow_html=True)

st.title("🌍 Cross-Exchange Arbitrage Scanner")

# ------------------- Settings Persistence -------------------
SETTINGS_FILE = "settings.json"

def load_settings():
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_settings(settings):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)

saved = load_settings()

# ------------------- Exchange List -------------------
TOP_EXCHANGES = [
    "binance", "okx", "coinbase", "kraken", "bybit", "kucoin",
    "mexc", "bitfinex", "bitget", "gateio", "crypto_com",
    "upbit", "whitebit", "poloniex", "bingx", "lbank",
    "bitstamp", "gemini", "bitrue", "xt", "huobi", "bitmart",
]

EXCHANGE_NAMES = {
    "binance": "Binance", "okx": "OKX", "coinbase": "Coinbase",
    "kraken": "Kraken", "bybit": "Bybit", "kucoin": "KuCoin",
    "mexc": "MEXC", "bitfinex": "Bitfinex", "bitget": "Bitget",
    "gateio": "Gate.io", "crypto_com": "Crypto.com", "upbit": "Upbit",
    "whitebit": "WhiteBIT", "poloniex": "Poloniex", "bingx": "BingX",
    "lbank": "LBank", "bitstamp": "Bitstamp", "gemini": "Gemini",
    "bitrue": "Bitrue", "xt": "XT", "huobi": "Huobi", "bitmart": "BitMart",
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

LEV_PATTERNS = [r"\b\d+[LS]\b", r"\bUP\b", r"\bDOWN\b", r"\bBULL\b", r"\bBEAR\b"]
LEV_REGEX = re.compile("|".join(LEV_PATTERNS), re.IGNORECASE)

# ------------------- Chain Normalization -------------------
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

# ------------------- Main Controls (No Sidebar) -------------------
st.header("Scanner Controls")

col1, col2 = st.columns(2)

with col1:
    buy_exchanges = st.multiselect(
        "Buy Exchanges (up to 10)",
        TOP_EXCHANGES,
        default=saved.get("buy_exchanges", ["binance", "okx"]),
        max_selections=10,
        format_func=lambda x: EXCHANGE_NAMES.get(x, x),
    )

with col2:
    sell_exchanges = st.multiselect(
        "Sell Exchanges (up to 10)",
        TOP_EXCHANGES,
        default=saved.get("sell_exchanges", ["binance", "okx"]),
        max_selections=10,
        format_func=lambda x: EXCHANGE_NAMES.get(x, x),
    )

col_a, col_b = st.columns(2)

with col_a:
    min_profit = st.number_input("Minimum Profit % (after fees)", 0.0, 100.0, saved.get("min_profit", 1.0), 0.1)
    max_profit = st.number_input("Maximum Profit % (after fees)", 0.0, 200.0, saved.get("max_profit", 20.0), 0.1)
    min_24h_vol_usd = st.number_input("Min 24h Volume (USD)", 0.0, 1_000_000_000.0, saved.get("min_24h_vol_usd", 100000.0), 50000.0)

with col_b:
    exclude_chains = st.multiselect(
        "Exclude Blockchains",
        ["ETH", "TRC20", "BSC", "SOL", "MATIC", "ARB", "OP", "Polygon", "TON", "AVAX"],
        default=saved.get("exclude_chains", ["ETH"])
    )
    include_all_chains = st.checkbox("Include all blockchains (ignore exclusion)", value=saved.get("include_all_chains", False))

auto_refresh = st.checkbox("🔄 Auto Refresh Every 20s", value=saved.get("auto_refresh", False))
scan_now = st.button("🚀 Scan Now", type="primary")

# ------------------- Save settings on every change -------------------
current_settings = {
    "buy_exchanges": buy_exchanges,
    "sell_exchanges": sell_exchanges,
    "min_profit": min_profit,
    "max_profit": max_profit,
    "min_24h_vol_usd": min_24h_vol_usd,
    "exclude_chains": exclude_chains,
    "include_all_chains": include_all_chains,
    "auto_refresh": auto_refresh,
}
save_settings(current_settings)

# ------------------- Extra Styling -------------------
st.markdown("""
    <style>
    .pill { padding: 2px 10px; border-radius: 999px; font-weight: 700; font-size: 12px; }
    .pill-green { background: #1B5E20; color: #E8F5E9; border: 1px solid #2E7D32; }
    .pill-red { background: #7F1D1D; color: #FEE2E2; border: 1px solid #991B1B; }
    .pill-blue { background: #0D47A1; color: #E3F2FD; border: 1px solid #1565C0; }
    .table-wrap { overflow-x: auto; border-radius: 10px; border: 1px solid #2A2A2A; }
    table.arb-table { width: 100%; border-collapse: collapse; }
    table.arb-table th, table.arb-table td { padding: 8px 10px; border-bottom: 1px solid #222; }
    table.arb-table th { background: #1D1D1D; text-align: left; }
    table.arb-table tr:nth-child(even) { background: #161616; }
    table.arb-table tr:hover { background: #202020; }
    .num { text-align: right; white-space: nowrap; }
    .mono { font-variant-numeric: tabular-nums; font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
    .small { color: #BDBDBD; font-size: 12px; }
    </style>
""", unsafe_allow_html=True)

# ---------- Runtime state ----------
if "op_cache" not in st.session_state:
    st.session_state.op_cache = {}
if "lifetime_history" not in st.session_state:
    st.session_state.lifetime_history = {}
if "last_seen_keys" not in st.session_state:
    st.session_state.last_seen_keys = set()
if "last_auto_scan" not in st.session_state:
    st.session_state.last_auto_scan = 0.0

# ---------- Helpers ----------
def parse_symbol(symbol: str):
    base, quote = symbol.split("/")[0], symbol.split("/")[1].split(":")[0]
    return base, quote

def market_price_from_ticker(t):
    if not t:
        return None
    last = t.get("last")
    if last is not None:
        try:
            return float(last)
        except:
            pass
    bid, ask = t.get("bid"), t.get("ask")
    if bid is not None and ask is not None:
        try:
            return (float(bid) + float(ask)) / 2.0
        except:
            return None
    return None

def is_ticker_fresh(t, max_age_sec=300):
    ts = t.get("timestamp")
    if ts is None:
        return True
    now = int(time.time() * 1000)
    return (now - int(ts)) <= max_age_sec * 1000

def fmt_usd(x):
    try:
        x = float(x or 0)
        if x >= 1e9: return f"${x/1e9:.2f}B"
        if x >= 1e6: return f"${x/1e6:.2f}M"
        if x >= 1e3: return f"${x/1e3:.0f}K"
        return f"${x:,.0f}"
    except:
        return "$0"

def secs_to_label(secs):
    return f"{int(secs)}s" if secs < 90 else f"{secs/60:.1f}m"

def update_lifetime_for_disappeared(current_keys):
    gone = st.session_state.last_seen_keys - set(current_keys)
    for key in gone:
        trail = st.session_state.op_cache.get(key, [])
        if trail:
            duration = trail[-1][0] - trail[0][0]
            if duration > 0:
                st.session_state.lifetime_history.setdefault(key, []).append(duration)
    st.session_state.last_seen_keys = set(current_keys)

def stability_and_expiry(key, current_profit):
    now = time.time()
    trail = st.session_state.op_cache.get(key, [])
    if not trail:
        st.session_state.op_cache[key] = [(now, current_profit)]
        return "⏳ new", "~unknown"
    trail.append((now, current_profit))
    st.session_state.op_cache[key] = trail[-30:]
    duration = trail[-1][0] - trail[0][0]
    observed = f"⏳ {secs_to_label(duration)} observed"
    hist = st.session_state.lifetime_history.get(key, [])
    if not hist:
        expiry = "~unknown"
    else:
        avg_life = sum(hist) / len(hist)
        remaining = avg_life - duration
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
        for key in INFO_VOLUME_CANDIDATES:
            val = info.get(key)
            if val is not None:
                try:
                    fval = float(val)
                    if fval > 0:
                        raw = fval
                        break
                except:
                    continue
        if raw is not None:
            if q_upper in USD_QUOTES:
                return float(raw)
            for conv_quote in ["USDT", "USDC", "USD"]:
                conv_sym = f"{q_upper}/{conv_quote}"
                conv_t = all_tickers.get(conv_sym)
                conv_px = market_price_from_ticker(conv_t)
                if conv_px:
                    return float(raw) * float(conv_px)
        if qvol:
            for conv_quote in ["USDT", "USDC", "USD"]:
                conv_sym = f"{q_upper}/{conv_quote}"
                conv_t = all_tickers.get(conv_sym)
                conv_px = market_price_from_ticker(conv_t)
                if conv_px:
                    return float(qvol) * float(conv_px)
        return 0.0
    except:
        return 0.0

def symbol_ok(ex_obj, symbol):
    m = ex_obj.markets.get(symbol, {})
    if not m:
        return False
    if not m.get("spot", True):
        return False
    base, quote = parse_symbol(symbol)
    if quote.upper() not in USD_QUOTES:
        return False
    if LEV_REGEX.search(symbol):
        return False
    if m.get("active") is False:
        return False
    return True

def choose_common_chain(ex1, ex2, coin, exclude_chains, include_all_chains):
    try:
        c1 = ex1.currencies.get(coin, {}) or {}
        c2 = ex2.currencies.get(coin, {}) or {}
        nets1_raw = c1.get("networks", {}) or {}
        nets2_raw = c2.get("networks", {}) or {}

        # Normalize for comparison
        nets1_norm = {normalize_chain(k): (k, v) for k, v in nets1_raw.items()}
        nets2_norm = {normalize_chain(k): (k, v) for k, v in nets2_raw.items()}

        common_norm = set(nets1_norm.keys()) & set(nets2_norm.keys())
        if not common_norm:
            return "❌ No chain", "❌", "❌"

        exclude_norm = {normalize_chain(c) for c in exclude_chains} if not include_all_chains else set()

        preferred_norm = [normalize_chain(n) for n in LOW_FEE_CHAIN_PRIORITY if normalize_chain(n) not in exclude_norm]

        best_norm = None
        for pnorm in preferred_norm:
            if pnorm in common_norm:
                best_norm = pnorm
                break

        if not best_norm:
            candidates = [c for c in common_norm if c not in exclude_norm]
            if not candidates:
                return "❌ No chain", "❌", "❌"
            best_norm = sorted(candidates)[0]

        # Get original info
        orig_key1, info1 = nets1_norm[best_norm]
        orig_key2, info2 = nets2_norm[best_norm]

        w_ok = "✅" if info1.get("withdraw") else "❌"
        d_ok = "✅" if info2.get("deposit") else "❌"

        display_chain = best_norm  # normalized is fine for display
        return display_chain, w_ok, d_ok
    except:
        return "❌ Unknown", "❌", "❌"

def fetch_tickers_safe(ex, ex_name):
    for attempt in range(3):
        try:
            return ex.fetch_tickers()
        except Exception as e:
            if attempt == 2:
                st.warning(f"⚠️ {ex_name} fetch_tickers failed after 3 tries: {e}")
                return {}
            time.sleep((2 ** attempt) * 1.5)
    return {}

# ---------- Core Scan ----------
def run_scan():
    if not buy_exchanges or not sell_exchanges:
        st.warning("Please select at least one Buy and one Sell exchange.")
        return

    try:
        # 1) init exchanges
        ex_objs = {}
        for ex_id in set(buy_exchanges + sell_exchanges):
            opts = {"enableRateLimit": True, "timeout": 15000}
            opts.update(EXTRA_OPTS.get(ex_id, {}))
            ex = getattr(ccxt, ex_id)(opts)
            ex.load_markets()
            ex_objs[ex_id] = ex

        # 2) bulk tickers with retry
        bulk_tickers = {}
        for ex_id, ex in ex_objs.items():
            bulk_tickers[ex_id] = fetch_tickers_safe(ex, EXCHANGE_NAMES.get(ex_id, ex_id))

        results = []
        current_keys = []

        # 3) compare
        for buy_id in buy_exchanges:
            for sell_id in sell_exchanges:
                if buy_id == sell_id:
                    continue
                buy_ex = ex_objs[buy_id]
                sell_ex = ex_objs[sell_id]
                buy_tk = bulk_tickers[buy_id]
                sell_tk = bulk_tickers[sell_id]

                common = set(buy_ex.markets.keys()) & set(sell_ex.markets.keys())
                symbols = [s for s in common if symbol_ok(buy_ex, s) and symbol_ok(sell_ex, s)]

                # Sort by combined volume (best liquidity first)
                def combined_vol(sym):
                    bt = buy_tk.get(sym)
                    st_ = sell_tk.get(sym)
                    px_b = market_price_from_ticker(bt) or 0
                    px_s = market_price_from_ticker(st_) or 0
                    v1 = safe_usd_volume(buy_id, sym, bt, px_b, buy_tk)
                    v2 = safe_usd_volume(sell_id, sym, st_, px_s, sell_tk)
                    return v1 + v2

                symbols.sort(key=combined_vol, reverse=True)
                symbols = symbols[:1000]  # generous but safe

                for sym in symbols:
                    bt = buy_tk.get(sym)
                    st_ = sell_tk.get(sym)
                    if not bt or not st_:
                        continue
                    if not is_ticker_fresh(bt) or not is_ticker_fresh(st_):
                        continue

                    buy_px = market_price_from_ticker(bt)
                    sell_px = market_price_from_ticker(st_)
                    if not buy_px or not sell_px:
                        continue

                    gap = abs(sell_px - buy_px) / buy_px
                    if gap > 0.5:
                        continue

                    buy_fee = buy_ex.markets.get(sym, {}).get("taker", 0.001) or 0.001
                    sell_fee = sell_ex.markets.get(sym, {}).get("taker", 0.001) or 0.001

                    spread = (sell_px - buy_px) / buy_px * 100.0
                    profit_after = spread - (buy_fee * 100 + sell_fee * 100)
                    if profit_after < min_profit or profit_after > max_profit:
                        continue

                    buy_vol_usd = safe_usd_volume(buy_id, sym, bt, buy_px, buy_tk)
                    sell_vol_usd = safe_usd_volume(sell_id, sym, st_, sell_px, sell_tk)
                    if buy_vol_usd < min_24h_vol_usd or sell_vol_usd < min_24h_vol_usd:
                        continue

                    base, quote = parse_symbol(sym)
                    chain, w_ok, d_ok = choose_common_chain(
                        buy_ex, sell_ex, base, exclude_chains, include_all_chains
                    )

                    if not include_all_chains and (chain.startswith("❌") or normalize_chain(chain) in {normalize_chain(c) for c in exclude_chains}):
                        continue
                    if w_ok != "✅" or d_ok != "✅":
                        continue

                    key = f"{sym}|{buy_id}>{sell_id}"
                    current_keys.append(key)
                    observed, expiry = stability_and_expiry(key, profit_after)

                    results.append({
                        "#": None,
                        "Pair": sym,
                        "Quote": quote,
                        "Buy@": EXCHANGE_NAMES.get(buy_id, buy_id),
                        "Buy Price": round(float(buy_px), 10),
                        "Sell@": EXCHANGE_NAMES.get(sell_id, sell_id),
                        "Sell Price": round(float(sell_px), 10),
                        "Spread %": round(spread, 4),
                        "Profit % After Fees": round(profit_after, 4),
                        "Buy Vol (24h)": fmt_usd(buy_vol_usd),
                        "Sell Vol (24h)": fmt_usd(sell_vol_usd),
                        "Withdraw?": w_ok,
                        "Deposit?": d_ok,
                        "Blockchain": chain,
                        "Stability": observed,
                        "Est. Expiry": expiry,
                    })

        update_lifetime_for_disappeared(current_keys)

        if results:
            df = pd.DataFrame(results).sort_values(
                ["Profit % After Fees", "Spread %"], ascending=False
            ).reset_index(drop=True)
            df["#"] = range(1, len(df) + 1)

            def pill(val, ok=True):
                return f'<span class="pill {"pill-green" if ok else "pill-red"}">{val}</span>'

            def color_profit(p):
                return f'<span class="good mono">{p:.4f}%</span>' if p >= 0 else f'<span class="bad mono">{p:.4f}%</span>'

            def color_spread(s):
                return f'<span class="spread mono">{s:.4f}%</span>'

            headers = ["#", "Pair", "Quote", "Buy@", "Buy Price", "Sell@", "Sell Price",
                       "Spread %", "Profit % After Fees", "Buy Vol (24h)", "Sell Vol (24h)",
                       "Withdraw?", "Deposit?", "Blockchain", "Stability", "Est. Expiry"]

            html = '<div class="table-wrap"><table class="arb-table"><thead><tr>'
            for h in headers:
                html += f"<th>{h}</th>"
            html += "</tr></thead><tbody>"

            for _, r in df.iterrows():
                html += "<tr>"
                html += f'<td class="num mono">{int(r["#"])}</td>'
                html += f'<td class="mono">{r["Pair"]}</td>'
                html += f'<td>{r["Quote"]}</td>'
                html += f'<td>{r["Buy@"]}</td>'
                html += f'<td class="num mono">{r["Buy Price"]}</td>'
                html += f'<td>{r["Sell@"]}</td>'
                html += f'<td class="num mono">{r["Sell Price"]}</td>'
                html += f'<td class="num">{color_spread(r["Spread %"])}</td>'
                html += f'<td class="num">{color_profit(r["Profit % After Fees"])}</td>'
                html += f'<td class="num mono">{r["Buy Vol (24h)"]}</td>'
                html += f'<td class="num mono">{r["Sell Vol (24h)"]}</td>'
                html += f'<td>{pill("✅", True) if r["Withdraw?"]=="✅" else pill("❌", False)}</td>'
                html += f'<td>{pill("✅", True) if r["Deposit?"]=="✅" else pill("❌", False)}</td>'
                html += f'<td><span class="pill pill-blue">{r["Blockchain"]}</span></td>'
                html += f'<td class="small">{r["Stability"]}</td>'
                html += f'<td class="small">{r["Est. Expiry"]}</td>'
                html += "</tr>"

            html += "</tbody></table></div>"

            st.subheader("✅ Profitable Arbitrage Opportunities")
            st.markdown(html, unsafe_allow_html=True)

            csv_df = df.copy()
            st.download_button("⬇️ Download CSV", csv_df.to_csv(index=False), "arbitrage_opportunities.csv", "text/csv")
        else:
            st.info("No opportunities matched your filters right now.")

    except Exception as e:
        st.error(f"Unexpected error: {e}")

# ---------- Trigger ----------
trigger_scan = scan_now

if auto_refresh:
    if time.time() - st.session_state.last_auto_scan >= 20:
        st.session_state.last_auto_scan = time.time()
        trigger_scan = True

if trigger_scan:
    with st.spinner("🔍 Scanning exchanges…"):
        run_scan()

if auto_refresh:
    remaining = max(0, 20 - (time.time() - st.session_state.last_auto_scan))
    st.caption(f"🔄 Next auto-refresh in {int(remaining)} seconds")
