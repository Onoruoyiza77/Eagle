"""
Solana wallet scout + alert bot (ALERTS ONLY - never trades, never touches keys).

python bot.py watch   -> check watched wallets for new trades, send Telegram alerts
python bot.py scout   -> find new winning wallets, drop weak ones, send scoreboard
python bot.py check   -> print the scoreboard

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
SOL_MINT = "So11111111111111111111111111111111111111112"

HERE = os.path.dirname(os.path.abspath(__file__))
STATE = os.path.join(HERE, "state")
os.makedirs(STATE, exist_ok=True)
WALLETS_FILE = os.path.join(STATE, "wallets.json")
SEEN_FILE = os.path.join(STATE, "seen.json")
LOG_FILE = os.path.join(STATE, "alerts.jsonl")


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
            "liq": (best.get("liquidity") or {}).get("usd", 0) or 0,
            "created": best.get("pairCreatedAt"),
            "url": best.get("url", ""),
        }
    except Exception:
        return None


def rug_risks(mint):
    try:
        r = requests.get(f"https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary", timeout=20).json()
        return [x.get("name", "?") for x in r.get("risks", []) if x.get("level") == "danger"]
    except Exception:
        return None


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
def handle_tx(tx, wallet, label):
    if time.time() - tx.get("timestamp", time.time()) > 1800:
        return  # too old to be useful
    s = parse_swap(tx, wallet)
    if not s or s["sol"] < MIN_SOL:
        return
    info = dex_info(s["mint"])
    risks = rug_risks(s["mint"]) if s["side"] == "BUY" else []
    v, flags = verdict(info, risks)
    sym = info["symbol"] if info else "?"
    price = info["price"] if info else 0
    tg_send(
        f"{'🟢 BUY' if s['side'] == 'BUY' else '🔴 SELL'}  {sym}\n"
        f"Wallet: {label}\nSize: {s['sol']:.2f} SOL\nPrice now: ${price}\n{v}\n"
        f"{info['url'] if info else 'https://dexscreener.com/solana/' + s['mint']}"
    )
    with open(LOG_FILE, "a") as f:
        f.write(json.dumps({
            "t": time.time(), "wallet": wallet, "label": label, "side": s["side"],
            "mint": s["mint"], "symbol": sym, "sol": s["sol"],
            "entry_price": price, "flagged": bool(flags),
        }) + "\n")


def watch():
    wallets = load_json(WALLETS_FILE, {})
    if not wallets:
        print("No wallets yet. Run the 'scout' workflow first.")
        return
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
                handle_tx(tx, addr, info.get("label", short(addr)))
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
        d = out.setdefault(r["wallet"], {"label": r["label"], "pcts": []})
        d["pcts"].append(pct - SLIPPAGE_PENALTY)
    return out


def check_report(send=False):
    perf = perf_by_wallet()
    if not perf:
        print("Nothing scored yet (alerts need 1h+ to age).")
        return
    lines = [f"📊 Wallet scoreboard (after {SLIPPAGE_PENALTY:.0f}% slippage)"]
    for d in sorted(perf.values(), key=lambda d: -sum(d["pcts"]) / len(d["pcts"])):
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
    if len(wallets) < MAX_WALLETS:
        scored = []
        rejected = []
        cand_list = find_candidates()
        for addr in cand_list:
            if addr in wallets:
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
            if len(wallets) >= MAX_WALLETS:
                break
            wallets[addr] = {"label": short(addr), "added": time.time()}
            report.append(
                f"✅ Added {short(addr)}: {st['closed']} coins, "
                f"{st['winrate'] * 100:.0f}% win, {st['net']:+.1f} SOL"
            )

    save_json(WALLETS_FILE, wallets)
    tg_send("🔎 Daily scout\n" + ("\n".join(report) if report else "No changes.") + f"\nFollowing {len(wallets)} wallets.\n\n" + "\n".join(DEBUG))
    check_report(send=True)


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "watch"
    if mode != "check" and not (HELIUS_KEY and BOT_TOKEN):
        print("Set HELIUS_KEY and WALLET_BOT_TOKEN.")
        sys.exit(1)
    {"watch": watch, "scout": scout, "check": check_report}[mode]()
