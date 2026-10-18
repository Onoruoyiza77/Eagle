"""
Solana wallet scout + alert bot (ALERTS ONLY - never trades, never touches keys).

python bot.py watch   -> check watched wallets for new trades, send Telegram alerts
python bot.py scout   -> find new winning wallets, drop weak ones, send scoreboard
python bot.py check   -> print the scoreboard
python bot.py coins   -> scan trending coins, send BUY SIGNALS, track how past signals did
python bot.py signals -> print how past coin signals performed

Extra filters on coin signals: holder concentration (RugCheck), listed socials (DexScreener),
SOL market-mood pause. KOL wallets: put "ADDRESS name" lines in state/kols.txt.

Needs env vars: HELIUS_KEY, WALLET_BOT_TOKEN, CHAT_ID
State lives in the state/ folder (wallets.json, seen.json, alerts.jsonl).
"""
import os, sys, time, json, requests

HELIUS_KEY = os.environ.get("HELIUS_KEY", "")
BOT_TOKEN = os.environ.get("WALLET_BOT_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")

MIN_LIQ = float(os.environ.get("MIN_LIQUIDITY_USD", "8000"))   # coin safety + scouting filter
MIN_SOL = float(os.environ.get("MIN_TRADE_SOL", "0.5"))         # ignore tiny trades
MAX_WALLETS = int(os.environ.get("MAX_WALLETS", "8"))           # how many wallets to follow
HISTORY_TXS = int(os.environ.get("HISTORY_TXS", "200"))         # past txs studied per wallet
MIN_CLOSED = int(os.environ.get("MIN_CLOSED", "4"))             # min coins bought AND sold
MIN_WINRATE = float(os.environ.get("MIN_WINRATE", "0.45"))
MIN_NET_SOL = float(os.environ.get("MIN_NET_SOL", "0.5"))         # min total profit in SOL
MAX_TRADES_PER_DAY = float(os.environ.get("MAX_TRADES_PER_DAY", "120"))  # above this = another bot
MAX_TOP_SHARE = float(os.environ.get("MAX_TOP_SHARE", "0.7"))   # one coin can't be >60% of profit
MIN_PUMP_PCT = float(os.environ.get("MIN_PUMP_PCT", "30"))      # coin must be up this % in 24h
POOLS_TO_SCAN = int(os.environ.get("POOLS_TO_SCAN", "10"))
MAX_CANDIDATES = int(os.environ.get("MAX_CANDIDATES", "60"))
SLIPPAGE_PENALTY = 3.0

# coin-signal filters (all tunable)
SIG_MIN_LIQ = float(os.environ.get("SIG_MIN_LIQ", "15000"))
SIG_MIN_AGE_H = float(os.environ.get("SIG_MIN_AGE_H", "1"))
SIG_MAX_AGE_H = float(os.environ.get("SIG_MAX_AGE_H", "72"))
SIG_MIN_H1 = float(os.environ.get("SIG_MIN_H1", "5"))            # 1h change: momentum started...
SIG_MAX_H1 = float(os.environ.get("SIG_MAX_H1", "150"))          # ...but not already parabolic
SIG_MAX_H24 = float(os.environ.get("SIG_MAX_H24", "800"))
SIG_MIN_BUYERS = int(os.environ.get("SIG_MIN_BUYERS", "30"))     # different buyers in last hour
SIG_MIN_BUY_RATIO = float(os.environ.get("SIG_MIN_BUY_RATIO", "0.55"))
SIG_MIN_VOL_LIQ = float(os.environ.get("SIG_MIN_VOL_LIQ", "0.3"))
SIG_MIN_MCAP = float(os.environ.get("SIG_MIN_MCAP", "50000"))
SIG_MAX_MCAP = float(os.environ.get("SIG_MAX_MCAP", "10000000"))
SIG_MAX_PER_RUN = int(os.environ.get("SIG_MAX_PER_RUN", "3"))
SIG_COOLDOWN_H = float(os.environ.get("SIG_COOLDOWN_H", "24"))   # don't repeat a coin within this
SIG_MAX_TOP10 = float(os.environ.get("SIG_MAX_TOP10", "50"))     # top-10 holders may own at most this %
MOOD_MIN_H1 = float(os.environ.get("MOOD_MIN_H1", "-3"))         # pause signals if SOL falls more than this in 1h
MOOD_MIN_H6 = float(os.environ.get("MOOD_MIN_H6", "-6"))         # ...or this much in 6h
CONFLUENCE_MIN = int(os.environ.get("CONFLUENCE_MIN", "2"))      # followed wallets buying same coin
CONFLUENCE_HOURS = float(os.environ.get("CONFLUENCE_HOURS", "6"))
SOL_MINT = "So11111111111111111111111111111111111111112"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
os.makedirs(STATE, exist_ok=True)
WALLETS_FILE = os.path.join(STATE, "wallets.json")
SEEN_FILE = os.path.join(STATE, "seen.json")
LOG_FILE = os.path.join(STATE, "alerts.jsonl")
SIGNALS_FILE = os.path.join(STATE, "signals.jsonl")
KOLS_FILE = os.path.join(STATE, "kols.txt")
MOOD_FILE = os.path.join(STATE, "mood.json")


# ---------- small helpers ----------
def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=1)


def tg_send(text):
    global CHAT_ID
    if not CHAT_ID:
        r = requests.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates", timeout=20).json()
        for u in reversed(r.get("result", [])):
            if u.get("message"):
                CHAT_ID = str(u["message"]["chat"]["id"])
                break
        if not CHAT_ID:
            print("No CHAT_ID. Send /start to your bot and set CHAT_ID.")
            return
    requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data={"chat_id": CHAT_ID, "text": text[:4000], "disable_web_page_preview": True},
        timeout=20,
    )


def wallet_list_text(wallets):
    lines = [f"👀 Following {len(wallets)} wallets:"]
    for i, (addr, info) in enumerate(wallets.items(), 1):
        lines.append(f"{i}. {info.get('label', short(addr))}\n{addr}")
    return "\n".join(lines)


def fmt_usd(n):
    if not n:
        return "unknown"
    for div, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= div:
            return f"${n / div:.1f}{suffix}"
    return f"${n:.0f}"


def short(addr):
    return addr[:4] + ".." + addr[-4:]


# ---------- Helius ----------
def rpc(method, params):
    r = requests.post(
        f"https://mainnet.helius-rpc.com/?api-key={HELIUS_KEY}",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=30,
    )
    r.raise_for_status()
    return r.json().get("result") or []


def get_sigs(wallet, limit, before=None, until=None):
    opts = {"limit": limit}
    if before:
        opts["before"] = before
    if until:
        opts["until"] = until
    return rpc("getSignaturesForAddress", [wallet, opts])


def parse_txs(sigs):
    out = []
    for i in range(0, len(sigs), 100):
        r = requests.post(
            "https://api.helius.xyz/v0/transactions",
            params={"api-key": HELIUS_KEY},
            json={"transactions": sigs[i:i + 100]},
            timeout=60,
        )
        r.raise_for_status()
        out += r.json()
    return out


def tok_amount(t):
    raw = t.get("rawTokenAmount", {})
    try:
        return int(raw.get("tokenAmount", 0)) / (10 ** int(raw.get("decimals", 0)))
    except Exception:
        return 0.0


def fallback_parse(tx, wallet):
    """For trades Helius did not label as a swap (pump.fun etc): read balance changes."""
    sol = 0.0
    for a in tx.get("accountData", []) or []:
        if a.get("account") == wallet:
            sol += (a.get("nativeBalanceChange") or 0) / 1e9
    got, sent, wsol = [], [], 0.0
    for t in tx.get("tokenTransfers", []) or []:
        mint, amt = t.get("mint"), t.get("tokenAmount") or 0
        if mint == SOL_MINT:
            if t.get("toUserAccount") == wallet:
                wsol += amt
            if t.get("fromUserAccount") == wallet:
                wsol -= amt
        elif t.get("toUserAccount") == wallet:
            got.append(mint)
        elif t.get("fromUserAccount") == wallet:
            sent.append(mint)
    if abs(sol) < 0.01:
        sol = wsol
    if sol <= -0.01 and got:
        return {"side": "BUY", "mint": got[0], "sol": -sol}
    if sol >= 0.01 and sent:
        return {"side": "SELL", "mint": sent[0], "sol": sol}
    return None


def parse_swap(tx, wallet):
    sw = (tx.get("events") or {}).get("swap")
    if not sw:
        return fallback_parse(tx, wallet)
    sol_in = sol_out = 0.0
    ni, no = sw.get("nativeInput"), sw.get("nativeOutput")
    if ni and ni.get("account") == wallet:
        sol_in += int(ni.get("amount", 0)) / 1e9
    if no and no.get("account") == wallet:
        sol_out += int(no.get("amount", 0)) / 1e9
    t_in = [t for t in sw.get("tokenInputs", []) if t.get("userAccount") == wallet]
    t_out = [t for t in sw.get("tokenOutputs", []) if t.get("userAccount") == wallet]
    for t in t_in:
        if t.get("mint") == SOL_MINT:
            sol_in += tok_amount(t)
    for t in t_out:
        if t.get("mint") == SOL_MINT:
            sol_out += tok_amount(t)
    t_in = [t for t in t_in if t.get("mint") != SOL_MINT]
    t_out = [t for t in t_out if t.get("mint") != SOL_MINT]
    if sol_in > 0 and t_out:
        return {"side": "BUY", "mint": t_out[0]["mint"], "sol": sol_in}
    if sol_out > 0 and t_in:
        return {"side": "SELL", "mint": t_in[0]["mint"], "sol": sol_out}
    return None


# ---------- coin info / safety ----------
def dex_info(mint):
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{mint}", timeout=20).json()
        pairs = r.get("pairs") or []
        if not pairs:
            return None
        best = max(pairs, key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        return {
            "symbol": best.get("baseToken", {}).get("symbol", "?"),
            "price": float(best.get("priceUsd") or 0),
            "mcap": best.get("marketCap") or best.get("fdv") or 0,
            "liq": (best.get("liquidity") or {}).get("usd", 0) or 0,
            "created": best.get("pairCreatedAt"),
            "url": best.get("url", ""),
            "socials": [str(x.get("type", "")).lower() for x in ((best.get("info") or {}).get("socials") or [])],
            "website": bool((best.get("info") or {}).get("websites")),
        }
    except Exception:
        return None


def rug_report(mint):
    """RugCheck full report: danger flags + holder concentration. None if unavailable."""
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report", timeout=25).json()
        if not isinstance(r, dict) or ("risks" not in r and "topHolders" not in r):
            return None
        danger = [x.get("name", "?") for x in (r.get("risks") or []) if x.get("level") == "danger"]
        if r.get("rugged"):
            danger.append("already rugged")
        pools, lp_locked = set(), None
        for mk in r.get("markets") or []:
            for k in ("pubkey", "liquidityA", "liquidityB"):
                if mk.get(k):
                    pools.add(mk[k])
            lp = mk.get("lp") or {}
            if lp.get("lpLockedPct") is not None:
                lp_locked = max(lp_locked or 0, float(lp["lpLockedPct"]))
        top = r.get("topHolders")
        holders = [h for h in (top or []) if h.get("address") not in pools and h.get("owner") not in pools]
        return {
            "danger": danger,
            "top10": sum(float(h.get("pct") or 0) for h in holders[:10]) if top else None,
            "insiders": sum(1 for h in holders[:20] if h.get("insider")),
            "lp_locked": lp_locked,
        }
    except Exception:
        return None


def rug_risks(mint):
    rep = rug_report(mint)
    return rep["danger"] if rep else None


def holder_line(rep):
    if not rep or rep.get("top10") is None:
        return ""
    t = f"👥 Top 10 holders own {rep['top10']:.0f}%"
    if rep.get("insiders"):
        t += f" ({rep['insiders']} flagged insider{'s' if rep['insiders'] != 1 else ''})"
    if rep.get("lp_locked") is not None:
        t += f", LP locked {rep['lp_locked']:.0f}%"
    return t + "\n"


def social_line(info):
    if not info:
        return ""
    names = [{"twitter": "X", "telegram": "Telegram"}.get(x, x.capitalize()) for x in info.get("socials", []) if x]
    if info.get("website"):
        names.append("website")
    return ("🔗 " + ", ".join(names) if names else "🔗 No socials listed") + "\n"


def verdict(info, risks):
    flags = []
    if not info:
        return "⚠️ No market data found", ["no data"]
    if info["liq"] < MIN_LIQ:
        flags.append(f"low liquidity (${info['liq']:,.0f})")
    if info["created"]:
        age_h = (time.time() * 1000 - info["created"]) / 3_600_000
        if age_h < 2:
            flags.append(f"very new ({age_h:.1f}h old)")
    if risks:
        flags.append("RugCheck danger: " + ", ".join(risks[:3]))
    if risks is None:
        flags.append("rug check unavailable")
    return ("✅ Looks OK" if not flags else "⚠️ " + "; ".join(flags)), flags


# ---------- WATCH ----------
def handle_tx(tx, wallet, label, group=""):
    if time.time() - tx.get("timestamp", time.time()) > 1800:
        return  # too old to be useful
    s = parse_swap(tx, wallet)
    if not s or s["sol"] < MIN_SOL:
        return
    if group == "kol":
        label = "🎤 " + label
    info = dex_info(s["mint"])
    rep_ = rug_report(s["mint"]) if s["side"] == "BUY" else None
    risks = ((rep_["danger"] if rep_ else None) if s["side"] == "BUY" else [])
    v, flags = verdict(info, risks)
    if rep_ and rep_.get("top10") is not None and rep_["top10"] > SIG_MAX_TOP10:
        v = v.replace("✅ Looks OK", "⚠️ Holders concentrated")
        flags.append("holders concentrated")
    extra = (holder_line(rep_) + social_line(info)) if s["side"] == "BUY" else ""
    sym = info["symbol"] if info else "?"
    price = info["price"] if info else 0
    tg_send(
        f"{'🟢 BUY' if s['side'] == 'BUY' else '🔴 SELL'}  {sym}\n"
        f"Wallet: {label}\n{wallet}\nSize: {s['sol']:.2f} SOL\nMarket cap: {fmt_usd(info['mcap'] if info else 0)}\n{v}\n{extra}"
        f"{info['url'] if info else 'https://dexscreener.com/solana/' + s['mint']}"
    )
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({
            "t": time.time(), "wallet": wallet, "label": label, "side": s["side"],
            "mint": s["mint"], "symbol": sym, "sol": s["sol"],
            "entry_price": price, "flagged": bool(flags), "group": group,
        }) + "\n")
    if s["side"] == "BUY" and info:
        confluence_check(s["mint"], sym, info, v)


def load_kols():
    """state/kols.txt: one 'ADDRESS name' per line. These are never dropped by the scout."""
    out = {}
    try:
        with open(KOLS_FILE) as f:
            for line in f:
                parts = line.strip().split()
                if parts and not parts[0].startswith("#"):
                    out[parts[0]] = {"label": parts[1] if len(parts) > 1 else short(parts[0]),
                                     "manual": True, "group": "kol"}
    except FileNotFoundError:
        pass
    return out


def n_auto(wallets):
    return sum(1 for w in wallets.values() if not w.get("manual"))


def watch():
    wallets = load_json(WALLETS_FILE, {})
    wallets.update(load_kols())
    if not wallets:
        print("No wallets yet. Run the 'scout' workflow first.")
        return
    if os.environ.get("SEND_LIST") == "true":
        tg_send(wallet_list_text(wallets))
    seen = load_json(SEEN_FILE, {})
    for addr, info in wallets.items():
        try:
            last = seen.get(addr)
            if not last:  # first time: just remember the latest tx, no alerts
                page = get_sigs(addr, 1)
                if page:
                    seen[addr] = page[0]["signature"]
                continue
            page = get_sigs(addr, 20, until=last)
            if not page:
                continue
            seen[addr] = page[0]["signature"]
            sigs = [s["signature"] for s in reversed(page) if not s.get("err")]
            txs = sorted(parse_txs(sigs), key=lambda t: t.get("timestamp", 0))
            for tx in txs:
                handle_tx(tx, addr, info.get("label", short(addr)), info.get("group", ""))
        except Exception as e:
            print(f"{short(addr)}: error {e}")
    save_json(SEEN_FILE, seen)


# ---------- SCOUT ----------
def wallet_stats(wallet):
    """Study a wallet's past trades. Returns stats dict or None."""
    sigs, before = [], None
    while len(sigs) < HISTORY_TXS:
        page = get_sigs(wallet, min(100, HISTORY_TXS - len(sigs)), before=before)
        if not page:
            break
        sigs += [s["signature"] for s in page if not s.get("err")]
        before = page[-1]["signature"]
        if len(page) < 100:
            break
    if not sigs:
        return None
    per, times, swaps = {}, [], 0
    for tx in parse_txs(sigs):
        s = parse_swap(tx, wallet)
        if not s:
            continue
        swaps += 1
        times.append(tx.get("timestamp", 0))
        d = per.setdefault(s["mint"], {"spent": 0, "got": 0, "buys": 0, "sells": 0})
        if s["side"] == "BUY":
            d["spent"] += s["sol"]
            d["buys"] += 1
        else:
            d["got"] += s["sol"]
            d["sells"] += 1
    closed = {m: d for m, d in per.items() if d["buys"] and d["sells"]}
    if not closed:
        return {"closed": 0, "winrate": 0, "net": 0, "top_share": 1, "tpd": 0, "swaps": swaps}
    profits = [d["got"] - d["spent"] for d in closed.values()]
    wins = [p for p in profits if p > 0]
    span_days = max((max(times) - min(times)) / 86400, 0.05)
    return {
        "closed": len(closed),
        "winrate": len(wins) / len(closed),
        "net": sum(profits),
        "top_share": (max(wins) / sum(wins)) if wins else 1,
        "tpd": swaps / span_days,
        "swaps": swaps,
    }


def why(st):
    r = []
    if st["closed"] < MIN_CLOSED:
        r.append("few coins")
    if st["winrate"] < MIN_WINRATE:
        r.append("low win%")
    if st["net"] < MIN_NET_SOL:
        r.append("low profit")
    if st["top_share"] > MAX_TOP_SHARE:
        r.append("one lucky coin")
    if st["tpd"] > MAX_TRADES_PER_DAY:
        r.append("too fast")
    return ", ".join(r)


def passes(st):
    return bool(
        st and st["closed"] >= MIN_CLOSED and st["winrate"] >= MIN_WINRATE
        and st["net"] >= MIN_NET_SOL and st["top_share"] <= MAX_TOP_SHARE
        and st["tpd"] <= MAX_TRADES_PER_DAY
    )


DEBUG = []


def gt(path, params=None):
    for attempt in range(5):
        time.sleep(4 + attempt * 8)  # wait longer after each block
        r = requests.get(
            "https://api.geckoterminal.com/api/v2" + path, params=params,
            headers={"Accept": "application/json;version=20230302"}, timeout=30,
        )
        if r.status_code == 429:
            print("rate limited, waiting...")
            continue
        r.raise_for_status()
        return r.json()
    raise Exception("GeckoTerminal kept rate limiting")


def find_candidates():
    """Wallets that made profit on coins that pumped in the last 24h."""
    pools = []
    for page in (1, 2):
        try:
            pools += gt("/networks/solana/trending_pools", {"page": page, "duration": "24h"}).get("data", [])
        except Exception as e:
            print("trending error", e)
    good = []
    for p in pools:
        a = p.get("attributes", {})
        ch = float((a.get("price_change_percentage") or {}).get("h24") or 0)
        liq = float(a.get("reserve_in_usd") or 0)
        if ch >= MIN_PUMP_PCT and liq >= MIN_LIQ:
            good.append((ch, a["address"]))
    good.sort(reverse=True)
    DEBUG.append(f"Trending coins: {len(pools)}, pumped enough: {len(good)}")
    errs = 0
    cands = {}
    for ch, addr in good[:POOLS_TO_SCAN]:
        try:
            trades = gt(f"/networks/solana/pools/{addr}/trades", {"trade_volume_in_usd_greater_than": 50}).get("data", [])
        except Exception as e:
            print("trades error", e)
            errs += 1
            continue
        agg = {}
        for t in trades:
            a = t.get("attributes", {})
            who, kind = a.get("tx_from_address"), a.get("kind")
            if not who or kind not in ("buy", "sell"):
                continue
            agg.setdefault(who, {"buy": 0, "sell": 0})[kind] += float(a.get("volume_in_usd") or 0)
        for who, d in agg.items():
            if d["buy"] > 0 and d["sell"] > d["buy"] * 1.2:  # bought, then sold higher
                c = cands.setdefault(who, [0, 0.0])
                c[0] += 1  # number of pumped coins this wallet profited on
                c[1] += d["sell"] - d["buy"]
    DEBUG.append(f"Coin lookups failed: {errs}, profitable wallets found: {len(cands)}")
    # wallets that profited on MORE different coins come first (consistency beats one big win)
    return sorted(cands, key=lambda w: (cands[w][0], cands[w][1]), reverse=True)[:MAX_CANDIDATES]


def perf_by_wallet():
    """How did coins bought after our alerts perform? (after slippage penalty)"""
    try:
        with open(LOG_FILE) as f:
            rows = [json.loads(l) for l in f if l.strip()]
    except FileNotFoundError:
        return {}
    cache, out = {}, {}
    for r in rows:
        if r["side"] != "BUY" or not r["entry_price"] or time.time() - r["t"] < 3600:
            continue
        if r["mint"] not in cache:
            cache[r["mint"]] = dex_info(r["mint"])
        info = cache[r["mint"]]
        pct = -100.0 if not info or not info["price"] else (info["price"] / r["entry_price"] - 1) * 100
        d = out.setdefault(r["wallet"], {"label": r["label"], "pcts": [], "group": r.get("group", "")})
        d["pcts"].append(pct - SLIPPAGE_PENALTY)
    return out


def check_report(send=False):
    perf = perf_by_wallet()
    if not perf:
        print("Nothing scored yet (alerts need 1h+ to age).")
        return
    lines = [f"📊 Wallet scoreboard (after {SLIPPAGE_PENALTY:.0f}% slippage)"]
    for grp, title in (("", "Scouted wallets"), ("kol", "🎤 KOL wallets")):
        items = [d for d in perf.values() if d.get("group", "") == grp]
        if not items:
            continue
        lines.append(f"-- {title} --")
        for d in sorted(items, key=lambda d: -sum(d["pcts"]) / len(d["pcts"])):
            v = d["pcts"]
            wins = sum(1 for p in v if p > 0) / len(v) * 100
            lines.append(f"{d['label']}: {len(v)} buys, {wins:.0f}% win, avg {sum(v) / len(v):+.1f}%")
    text = "\n".join(lines)
    print(text)
    if send:
        tg_send(text)


def scout():
    wallets = load_json(WALLETS_FILE, {})
    perf = perf_by_wallet()
    report = []

    # 1) drop weak wallets
    for addr, info in list(wallets.items()):
        if info.get("manual"):
            continue
        try:
            st = wallet_stats(addr)
        except Exception as e:
            print("recheck error", e)
            continue
        p = perf.get(addr, {}).get("pcts", [])
        bad_history = st is not None and not passes(st)
        bad_alerts = len(p) >= 5 and sum(p) / len(p) < -10
        if bad_history or bad_alerts:
            del wallets[addr]
            report.append(f"❌ Dropped {info.get('label', short(addr))}")

    # 2) find and add new winners
    kols = load_kols()
    if n_auto(wallets) < MAX_WALLETS:
        scored = []
        rejected = []
        cand_list = find_candidates()
        for addr in cand_list:
            if addr in wallets or addr in kols:
                continue
            try:
                st = wallet_stats(addr)
            except Exception as e:
                print("stats error", e)
                continue
            if passes(st):
                scored.append((st["winrate"] * st["net"], addr, st))
            elif st and len(rejected) < 6:
                rejected.append(
                    f"{short(addr)}: {st['closed']} coins, {st['winrate'] * 100:.0f}% win, "
                    f"{st['net']:+.1f} SOL, {st['swaps']} swaps -> {why(st)}"
                )
        DEBUG.append(f"Wallets studied: {len(cand_list)}, passed: {len(scored)}")
        if rejected:
            DEBUG.append("Examples rejected:\n" + "\n".join(rejected))
        scored.sort(reverse=True)
        for _, addr, st in scored:
            if n_auto(wallets) >= MAX_WALLETS:
                break
            wallets[addr] = {"label": short(addr), "added": time.time()}
            report.append(
                f"✅ Added {addr}: {st['closed']} coins, "
                f"{st['winrate'] * 100:.0f}% win, {st['net']:+.1f} SOL"
            )

    save_json(WALLETS_FILE, wallets)
    tg_send("🔎 Daily scout\n" + ("\n".join(report) if report else "No changes.") + "\n\n" + wallet_list_text({**wallets, **kols}) + "\n\n" + "\n".join(DEBUG))
    check_report(send=True)
    update_outcomes()
    signal_report(send=True)



# ---------- COIN SIGNALS ----------
def load_signals():
    rows = []
    try:
        with open(SIGNALS_FILE) as f:
            for l in f:
                if l.strip():
                    rows.append(json.loads(l))
    except FileNotFoundError:
        pass
    return rows


def save_signals(rows):
    with open(SIGNALS_FILE, "w") as f:
        for r in rows[-600:]:
            f.write(json.dumps(r) + "\n")


def add_signal(row):
    with open(SIGNALS_FILE, "a") as f:
        f.write(json.dumps(row) + "\n")


def followed_buys():
    """mint -> set of followed-wallet labels that bought it recently."""
    out = {}
    try:
        with open(LOG_FILE) as f:
            for l in f:
                if not l.strip():
                    continue
                r = json.loads(l)
                if r["side"] == "BUY" and time.time() - r["t"] < CONFLUENCE_HOURS * 3600:
                    out.setdefault(r["mint"], set()).add(r["label"])
    except FileNotFoundError:
        pass
    return out


def confluence_check(mint, sym, info, v):
    """If several followed wallets bought the same coin, send a stronger signal."""
    who = followed_buys().get(mint, set())
    if len(who) < CONFLUENCE_MIN:
        return
    cutoff = time.time() - 12 * 3600
    if any(r["mint"] == mint and r["type"] == "confluence" and r["t"] > cutoff for r in load_signals()):
        return
    tg_send(
        f"🔥 STRONG BUY SIGNAL  {sym}\n"
        f"{len(who)} of your followed wallets bought it within {CONFLUENCE_HOURS:.0f}h:\n"
        f"{', '.join(sorted(who))}\n"
        f"Market cap: {fmt_usd(info['mcap'])}   Liquidity: {fmt_usd(info['liq'])}\n{v}\n{info['url']}\n\n"
        f"Paper-test only. Memecoins can fall 50%+ in minutes."
    )
    add_signal({"t": time.time(), "type": "confluence", "mint": mint, "symbol": sym,
                "entry_price": info["price"], "entry_mcap": info["mcap"]})


def _age_hours(created):
    try:
        from datetime import datetime
        dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
        return (time.time() - dt.timestamp()) / 3600
    except Exception:
        return None


def pct(x):
    try:
        return float(x)
    except Exception:
        return 0.0


def sol_trend():
    """(1h %, 6h %, 24h %) change of SOL, or None."""
    try:
        r = requests.get(f"https://api.dexscreener.com/latest/dex/tokens/{SOL_MINT}", timeout=20).json()
        best = max(r.get("pairs") or [], key=lambda p: (p.get("liquidity") or {}).get("usd", 0) or 0)
        ch = best.get("priceChange") or {}
        return pct(ch.get("h1")), pct(ch.get("h6")), pct(ch.get("h24"))
    except Exception:
        return None


def scan_coins():
    """Look at trending Solana pools, keep ones with healthy momentum and safe-looking numbers."""
    trend = sol_trend()
    paused = bool(trend and (trend[0] <= MOOD_MIN_H1 or trend[1] <= MOOD_MIN_H6))
    was = load_json(MOOD_FILE, {}).get("paused", False)
    if paused != was:
        save_json(MOOD_FILE, {"paused": paused})
        tg_send("⏸ Coin signals paused: SOL is dropping (%+.1f%% 1h, %+.1f%% 6h)." % (trend[0], trend[1])
                if paused else "▶️ Coin signals resumed: SOL has stabilised.")
    if paused:
        print(f"scan skipped: SOL is falling ({trend[0]:+.1f}% 1h, {trend[1]:+.1f}% 6h)")
        return
    pools = []
    for path in ("/networks/solana/trending_pools", "/networks/solana/new_pools"):
        try:
            pools += gt(path, {"page": 1}).get("data", [])
        except Exception as e:
            print("scan error", e)
    recent = {r["mint"] for r in load_signals() if time.time() - r["t"] < SIG_COOLDOWN_H * 3600}
    followed = followed_buys()
    seen, cands, why_not = set(), [], {}

    def no(reason):
        why_not[reason] = why_not.get(reason, 0) + 1

    for p in pools:
        a = p.get("attributes", {}) or {}
        base = ((p.get("relationships", {}).get("base_token", {}) or {}).get("data", {}) or {}).get("id", "")
        mint = base.replace("solana_", "")
        if not mint or mint in seen:
            continue
        seen.add(mint)
        if mint in recent:
            no("already signalled"); continue
        liq = pct(a.get("reserve_in_usd"))
        mcap = pct(a.get("market_cap_usd")) or pct(a.get("fdv_usd"))
        chg = a.get("price_change_percentage") or {}
        h1, h24 = pct(chg.get("h1")), pct(chg.get("h24"))
        tx = (a.get("transactions") or {}).get("h1") or {}
        buys, sells, buyers = tx.get("buys", 0) or 0, tx.get("sells", 0) or 0, tx.get("buyers", 0) or 0
        ratio = buys / (buys + sells) if (buys + sells) else 0
        vol1 = pct((a.get("volume_usd") or {}).get("h1"))
        age = _age_hours(a.get("pool_created_at", ""))
        if liq < SIG_MIN_LIQ:
            no("low liquidity"); continue
        if age is None or age < SIG_MIN_AGE_H or age > SIG_MAX_AGE_H:
            no("age out of range"); continue
        if not (SIG_MIN_MCAP <= mcap <= SIG_MAX_MCAP):
            no("market cap out of range"); continue
        if not (SIG_MIN_H1 <= h1 <= SIG_MAX_H1):
            no("1h move out of range"); continue
        if h24 > SIG_MAX_H24:
            no("already pumped too much"); continue
        if buyers < SIG_MIN_BUYERS:
            no("few buyers"); continue
        if ratio < SIG_MIN_BUY_RATIO:
            no("sellers dominate"); continue
        if vol1 / liq < SIG_MIN_VOL_LIQ:
            no("low volume"); continue
        dinfo = dex_info(mint)
        has_social = bool(dinfo and (dinfo["socials"] or dinfo["website"]))
        score = (ratio * 40 + min(vol1 / liq, 3) * 10 + min(buyers, 300) / 10
                 + (15 if mint in followed else 0) + (5 if has_social else 0))
        cands.append((score, mint, a, dict(liq=liq, mcap=mcap, h1=h1, h24=h24, buyers=buyers, dinfo=dinfo,
                                          ratio=ratio, age=age, price=pct(a.get("base_token_price_usd")))))
    cands.sort(key=lambda c: -c[0])

    sent = 0
    for score, mint, a, d in cands:
        if sent >= SIG_MAX_PER_RUN:
            break
        rep_ = rug_report(mint)
        if rep_ and rep_["danger"]:
            no("RugCheck danger"); continue
        if rep_ and rep_["top10"] is not None and rep_["top10"] > SIG_MAX_TOP10:
            no("holders concentrated"); continue
        sym = (a.get("name") or "?").split(" / ")[0]
        flag = "✅ No rug flags" if rep_ is not None else "⚠️ Rug check unavailable"
        who = followed.get(mint)
        tg_send(
            f"🚀 BUY SIGNAL  {sym}\n"
            f"Market cap: {fmt_usd(d['mcap'])}   Liquidity: {fmt_usd(d['liq'])}\n"
            f"Age: {d['age']:.1f}h\n"
            f"1h: {d['h1']:+.0f}%   24h: {d['h24']:+.0f}%\n"
            f"1h buyers: {d['buyers']} ({d['ratio'] * 100:.0f}% of trades are buys)\n"
            + (f"👀 Also bought by your wallets: {', '.join(sorted(who))}\n" if who else "")
            + f"{flag}\n{holder_line(rep_)}{social_line(d['dinfo'])}"
            + (f"Market: SOL {trend[1]:+.1f}% (6h)\n" if trend else "")
            + f"https://dexscreener.com/solana/{a.get('address', mint)}\n\n"
            f"Paper-test only. Memecoins can fall 50%+ in minutes."
        )
        add_signal({"t": time.time(), "type": "scan", "mint": mint, "symbol": sym,
                    "entry_price": d["price"], "entry_mcap": d["mcap"]})
        sent += 1
    print(f"scan: {len(pools)} pools, {len(cands)} passed filters, {sent} signals sent. Rejected: {why_not}")


def update_outcomes():
    """Fill in the price 1h / 6h / 24h after each signal."""
    rows = load_signals()
    cache, changed = {}, False
    for r in rows:
        age = time.time() - r["t"]
        for key, secs in (("p1", 3600), ("p6", 21600), ("p24", 86400)):
            if r.get(key) is None and age >= secs:
                if r["mint"] not in cache:
                    cache[r["mint"]] = dex_info(r["mint"])
                info = cache[r["mint"]]
                r[key] = info["price"] if info and info["price"] else 0
                changed = True
    if changed:
        save_signals(rows)


def signal_report(send=False):
    rows = load_signals()
    lines = []
    for typ, label in (("scan", "Coin scanner"), ("confluence", "Wallet confluence")):
        for key, name in (("p1", "1h"), ("p6", "6h"), ("p24", "24h")):
            vals = []
            for r in rows:
                if r["type"] != typ or r.get(key) is None or not r.get("entry_price"):
                    continue
                move = -100.0 if r[key] == 0 else (r[key] / r["entry_price"] - 1) * 100
                vals.append(move - SLIPPAGE_PENALTY)
            if vals:
                up = sum(1 for v in vals if v > 0) / len(vals) * 100
                lines.append(f"{label} after {name}: {len(vals)} signals, {up:.0f}% up, avg {sum(vals) / len(vals):+.1f}%")
    text = "📈 Coin signal results (after slippage)\n" + "\n".join(lines) if lines else "📈 Coin signals: nothing old enough to score yet."
    print(text)
    if send:
        tg_send(text)


def coins():
    update_outcomes()
    scan_coins()


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if mode != "check" and not (HELIUS_KEY and BOT_TOKEN):
        print("Set HELIUS_KEY and WALLET_BOT_TOKEN.")
        sys.exit(1)
    {"watch": watch, "scout": scout, "check": check_report, "coins": coins, "signals": signal_report}[mode]()
