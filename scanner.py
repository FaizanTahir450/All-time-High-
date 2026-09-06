"""
ATH Zone Scanner — CoinGecko → Telegram
----------------------------------------
Finds coins that trade close to their all-time high (ATH) while still being
small/mid caps with most of their supply locked, and that are listed on the
chosen exchange(s). Sends one consolidated Telegram message per run and marks
coins that are NEW to the zone since the previous run.

Rules (env-configurable; defaults are the owner's spec):
  * Drawdown from ATH between ATH_DD_MIN and ATH_DD_MAX percent   (0 – 40 %)
  * Market cap between MCAP_MIN and MCAP_MAX USD                    ($10M – $500M)
  * Locked supply >= MIN_LOCKED_PCT percent                         (60 %)
        locked % = (1 - circulating / (max_supply or total_supply)) * 100
  * Listed (any live spot pair) on every exchange in EXCHANGES      (binance)

Data: CoinGecko public API — free, no key required. A free *Demo* API key
(COINGECKO_API_KEY) raises the rate limit and is recommended on GitHub runners.
Exchange listings come from CoinGecko's per-exchange tickers, so any CoinGecko
exchange id (or a friendly alias, see EXCHANGE_ALIASES) works in EXCHANGES.

Files written each run (all committed back by the GitHub Action):
  state.json          coins currently in the zone per exchange + first-seen date
  alerts_archive.txt  plain-text copy of every Telegram message
  matches.jsonl       one JSON line per (run, exchange, coin) match

Env vars: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required unless DRY_RUN=1);
EXCHANGES, ATH_DD_MIN, ATH_DD_MAX, MCAP_MIN, MCAP_MAX, MIN_LOCKED_PCT,
COINGECKO_API_KEY, REQUEST_PAUSE, STATE_FILE, ALERTS_ARCHIVE, MATCHES_LOG,
DRY_RUN (optional).
"""

import os
import re
import sys
import json
import time
from datetime import datetime, date, timezone

import requests

# Windows consoles default to a legacy code page; the message contains emoji.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:                       # not a real console (e.g. piped) — fine
    pass


def _env_float(name, default):
    return float(os.environ.get(name, default))


# ── Strategy rules ─────────────────────────────────────────────────
ATH_DD_MIN     = _env_float("ATH_DD_MIN", "0")        # % below ATH — lower bound (0 = at ATH)
ATH_DD_MAX     = _env_float("ATH_DD_MAX", "40")       # % below ATH — upper bound
MCAP_MIN       = _env_float("MCAP_MIN", "10000000")   # $10M
MCAP_MAX       = _env_float("MCAP_MAX", "500000000")  # $500M
MIN_LOCKED_PCT = _env_float("MIN_LOCKED_PCT", "60")   # ≥60 % of supply NOT circulating

# Exchanges to require a listing on. Comma-separated CoinGecko exchange ids or
# aliases; each gets its own section in the message. Default: Binance only.
EXCHANGES_RAW = os.environ.get("EXCHANGES", "binance")

# ── Runtime / data config ──────────────────────────────────────────
CG_BASE           = "https://api.coingecko.com/api/v3"
COINGECKO_API_KEY = os.environ.get("COINGECKO_API_KEY", "").strip()
REQUEST_PAUSE     = _env_float("REQUEST_PAUSE", "2.0" if COINGECKO_API_KEY else "6.0")
                                                          # s between CoinGecko calls: Demo key ≈ 30/min, keyless ≈ 10/min
PER_PAGE          = 250                                   # /coins/markets page size (max)
MAX_MARKET_PAGES  = int(os.environ.get("MAX_MARKET_PAGES", "12"))  # safety cap (12 × 250 = 3,000 coins)
TICKER_BATCH      = 25                                    # coin ids per exchange-tickers request
DRY_RUN           = os.environ.get("DRY_RUN", "0") == "1" # print instead of Telegram; write nothing

STATE_FILE     = os.environ.get("STATE_FILE", "state.json")
ALERTS_ARCHIVE = os.environ.get("ALERTS_ARCHIVE", "alerts_archive.txt")
MATCHES_LOG    = os.environ.get("MATCHES_LOG", "matches.jsonl")

# Friendly names → CoinGecko exchange ids (anything not listed is passed through as-is)
EXCHANGE_ALIASES = {
    "binance": "binance", "mexc": "mxc", "mxc": "mxc", "kucoin": "kucoin",
    "bybit": "bybit_spot", "bybit_spot": "bybit_spot", "okx": "okex", "okex": "okex",
    "gate": "gate", "gateio": "gate", "gate.io": "gate", "coinbase": "gdax", "gdax": "gdax",
    "kraken": "kraken", "bitget": "bitget", "htx": "huobi", "huobi": "huobi",
    "crypto.com": "crypto_com", "cryptocom": "crypto_com", "crypto_com": "crypto_com",
    "upbit": "upbit", "bingx": "bingx", "lbank": "lbank",
}
EXCHANGE_NAMES = {
    "binance": "Binance", "mxc": "MEXC", "kucoin": "KuCoin", "bybit_spot": "Bybit",
    "okex": "OKX", "gate": "Gate", "gdax": "Coinbase", "kraken": "Kraken", "bitget": "Bitget",
    "huobi": "HTX", "crypto_com": "Crypto.com", "upbit": "Upbit", "bingx": "BingX", "lbank": "LBank",
}

# Preferred quote for the pair shown in the message (first match wins)
QUOTE_PREF = ["USDT", "USDC", "FDUSD", "USD1", "USD", "BTC", "ETH", "BNB", "EUR", "TRY"]

# Assets that are never a "coin near ATH" in the intended sense. CoinGecko category
# ids, fetched at runtime (one call each); override with EXCLUDE_CATEGORIES=a,b,c.
EXCLUDE_CATEGORIES = tuple(c.strip() for c in os.environ.get(
    "EXCLUDE_CATEGORIES",
    "stablecoins,tokenized-products,tokenized-stock,tokenized-private-credit,bittensor-subnets"
).split(",") if c.strip())
STABLE_SYMBOLS = {"USDT", "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "USDE", "USD1", "USDD",
                  "PYUSD", "FRAX", "LUSD", "GUSD", "EURC", "EURI", "AEUR", "XUSD", "USDS", "RLUSD"}
_EXCLUDE_NAME = re.compile(r"\b(wrapped|bridged|staked|restaked|liquid staking|tokeni[sz]ed|xstock)\b", re.I)

session = requests.Session()
session.headers.update({"User-Agent": "ath-zone-scanner/1.0", "Accept": "application/json"})
if COINGECKO_API_KEY:
    session.headers["x-cg-demo-api-key"] = COINGECKO_API_KEY


# ── CoinGecko client ───────────────────────────────────────────────
def cg_get(path, params=None, retries=6):
    """GET a CoinGecko endpoint with 429/5xx back-off and a pause after each call."""
    url = CG_BASE + path
    for attempt in range(retries):
        try:
            r = session.get(url, params=params, timeout=30)
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            print(f"  network error on {path}: {type(e).__name__} — retrying")
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After", "")
            wait = float(ra) if ra.isdigit() else min(60.0, 10.0 * (attempt + 1))
            wait = max(wait, 5.0)                       # CoinGecko sometimes answers Retry-After: 0
            print(f"  HTTP {r.status_code} on {path} — waiting {wait:.0f}s")
            time.sleep(wait)
            continue
        r.raise_for_status()
        time.sleep(REQUEST_PAUSE)
        return r.json()
    raise RuntimeError(f"CoinGecko still failing after {retries} attempts: {path}")


def fetch_universe():
    """Every coin with market cap >= MCAP_MIN, walking /coins/markets by market cap (desc)."""
    rows, seen = [], set()
    for page in range(1, MAX_MARKET_PAGES + 1):
        batch = cg_get("/coins/markets", {"vs_currency": "usd", "order": "market_cap_desc",
                                          "per_page": PER_PAGE, "page": page, "sparkline": "false"})
        if not batch:
            break
        for r in batch:
            if r.get("id") and r["id"] not in seen:      # pages can shift between calls → dedupe
                seen.add(r["id"])
                rows.append(r)
        mcaps = [r["market_cap"] for r in batch if r.get("market_cap")]
        floor = min(mcaps) if mcaps else 0
        print(f"  markets page {page}: {len(batch)} coins, floor {fmt_money(floor)}")
        if len(batch) < PER_PAGE or floor < MCAP_MIN:
            break
    return rows


def fetch_excluded_ids():
    """Coin ids in the EXCLUDE_CATEGORIES (stablecoins, tokenized stocks, ...).

    Pages by market cap and stops once the page floor is under MCAP_MIN — anything
    smaller can't be in the funnel anyway. A failing category is skipped (the
    name/symbol exclusions still apply).
    """
    ids = set()
    for cat in EXCLUDE_CATEGORIES:
        try:
            for page in range(1, 4):
                rows = cg_get("/coins/markets", {"vs_currency": "usd", "category": cat, "order": "market_cap_desc",
                                                 "per_page": PER_PAGE, "page": page, "sparkline": "false"})
                ids |= {r["id"] for r in rows if r.get("id")}
                mcaps = [r["market_cap"] for r in rows if r.get("market_cap")]
                if len(rows) < PER_PAGE or not mcaps or min(mcaps) < MCAP_MIN:
                    break
        except Exception as e:
            print(f"  category '{cat}' unavailable ({type(e).__name__}) — skipped")
    return ids


def exchange_listings(ex_id, coin_ids):
    """coin_id -> set of (BASE, TARGET) live spot pairs on the exchange, for the given coins only.

    Uses /exchanges/{id}/tickers?coin_ids=... in batches. CoinGecko matches the
    filter on base OR target, so unrelated pairs can come back — they are ignored.
    Stale / anomalous tickers don't count as a listing.
    """
    listings = {}
    ids = sorted(coin_ids)
    for i in range(0, len(ids), TICKER_BATCH):
        chunk = ids[i:i + TICKER_BATCH]
        page = 1
        while page <= 20:
            data = cg_get(f"/exchanges/{ex_id}/tickers", {"coin_ids": ",".join(chunk), "page": page})
            tickers = data.get("tickers") or []
            for t in tickers:
                cid = t.get("coin_id")
                if cid in coin_ids and not t.get("is_stale") and not t.get("is_anomaly"):
                    listings.setdefault(cid, set()).add((t.get("base") or "", t.get("target") or ""))
            if len(tickers) < 100:                      # tickers pages are 100 long
                break
            page += 1
    return listings


# ── Rules ──────────────────────────────────────────────────────────
def ath_drawdown_pct(row):
    """% below ATH (0 = at/above ATH). None if CoinGecko has no ATH/price."""
    ath, px = row.get("ath"), row.get("current_price")
    if not ath or not px or ath <= 0:
        return None
    return max(0.0, (ath - px) / ath * 100.0)


def locked_pct(row):
    """% of supply not yet circulating, against max_supply (else total_supply). None if unknown."""
    circ = row.get("circulating_supply")
    denom = row.get("max_supply") or row.get("total_supply")
    if not circ or not denom or denom <= 0:
        return None
    return max(0.0, min(100.0, (1.0 - circ / denom) * 100.0))


def is_excluded(row, excluded_ids):
    sym = (row.get("symbol") or "").upper()
    return (row.get("id") in excluded_ids or sym in STABLE_SYMBOLS
            or bool(_EXCLUDE_NAME.search(row.get("name") or "")))


def check_rules(row, excluded_ids):
    """(passes, reason) — reason names the FIRST rule that fails (used for 'left the zone')."""
    if is_excluded(row, excluded_ids):
        return False, "excluded (stable/wrapped)"
    mcap = row.get("market_cap") or 0
    if not (MCAP_MIN <= mcap <= MCAP_MAX):
        return False, f"MC {fmt_money(mcap)}"
    dd = ath_drawdown_pct(row)
    if dd is None:
        return False, "no ATH data"
    if not (ATH_DD_MIN <= dd <= ATH_DD_MAX):
        return False, f"▼{dd:.0f}% ATH"
    lp = locked_pct(row)
    if lp is None:
        return False, "supply unknown"
    if lp < MIN_LOCKED_PCT:
        return False, f"{lp:.0f}% locked"
    return True, ""


def pick_pair(pairs):
    """Most useful pair to show: preferred quote first, else alphabetical."""
    by_target = {t: b for b, t in pairs}
    for q in QUOTE_PREF:
        if q in by_target:
            return f"{by_target[q]}/{q}"
    b, t = sorted(pairs)[0]
    return f"{b}/{t}"


def build_match(row, ex_id, pairs, first_seen, today):
    fs = date.fromisoformat(first_seen)
    return {
        "coin_id": row["id"], "symbol": (row.get("symbol") or "").upper(), "name": row.get("name") or "",
        "exchange": ex_id, "pair": pick_pair(pairs), "pairs": len(pairs),
        "price": row.get("current_price"), "ath": row.get("ath"), "ath_date": (row.get("ath_date") or "")[:10],
        "dd_pct": round(ath_drawdown_pct(row), 2), "mcap": row.get("market_cap"),
        "mcap_rank": row.get("market_cap_rank"), "locked_pct": round(locked_pct(row), 2),
        "circulating": row.get("circulating_supply"),
        "supply_base": row.get("max_supply") or row.get("total_supply"),
        "volume_24h": row.get("total_volume"),
        "first_seen": first_seen, "days_in_zone": (today - fs).days, "new": fs == today,
    }


# ── State / logs ───────────────────────────────────────────────────
def load_state(path=STATE_FILE):
    if not os.path.exists(path):
        return {"exchanges": {}}
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    st.setdefault("exchanges", {})
    return st


def save_state(state, path=STATE_FILE):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.write("\n")


def append_archive(text, path=ALERTS_ARCHIVE):
    divider = ("\n" + "=" * 50 + "\n\n") if os.path.exists(path) and os.path.getsize(path) else ""
    with open(path, "a", encoding="utf-8") as f:
        f.write(divider + text.rstrip() + "\n")


def log_matches(matches, run_date, path=MATCHES_LOG):
    with open(path, "a", encoding="utf-8") as f:
        for m in matches:
            rec = {"run": run_date.isoformat(), **{k: v for k, v in m.items() if k != "days_in_zone"}}
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


# ── Telegram + formatting ──────────────────────────────────────────
def _chunks(text, limit=4000):
    """Split on line boundaries so no coin entry is cut in half (Telegram max 4096)."""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + 1 + len(line) > limit:
            parts.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    if cur:
        parts.append(cur)
    return parts


def send_telegram(text):
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat_id = os.environ["TELEGRAM_CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for part in _chunks(text):
        resp = session.post(url, json={"chat_id": chat_id, "text": part,
                                       "disable_web_page_preview": True}, timeout=20)
        resp.raise_for_status()


def fmt_money(x):
    x = float(x or 0)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(x) >= div:
            v = x / div
            return f"${v:,.2f}{unit}" if v < 10 else f"${v:,.1f}{unit}" if v < 100 else f"${v:,.0f}{unit}"
    return f"${x:,.0f}"


def fmt_price(p):
    p = float(p or 0)
    if p == 0:
        return "0"
    if p < 1:
        return f"{p:.8f}".rstrip("0").rstrip(".")
    return f"{p:,.4f}".rstrip("0").rstrip(".") if p < 100 else f"{p:,.2f}"


def fmt_ath_date(iso):
    """'2026-09-02' → '2 Sep 26'; empty string if unknown."""
    try:
        d = date.fromisoformat((iso or "")[:10])
        return f"{d.day} {d.strftime('%b %y')}"
    except ValueError:
        return ""


def fmt_match(m):
    flag = "🆕" if m["new"] else "•"
    zone = "new today" if m["new"] else f"in zone {m['days_in_zone']}d"
    when = fmt_ath_date(m.get("ath_date"))
    ath = f"ATH ${fmt_price(m['ath'])}" + (f" ({when})" if when else "")   # date shows if the ATH is a fresh listing
    l1 = f"{flag} {m['symbol']} ({m['name']}) — ${fmt_price(m['price'])}  [{m['pair']}]"
    l2 = (f"     ▼{m['dd_pct']:.1f}% from {ath} · MC {fmt_money(m['mcap'])}"
          f" · {m['locked_pct']:.0f}% locked · {zone}")
    return f"{l1}\n{l2}"


def rules_line():
    return (f"Rules: {ATH_DD_MIN:g}–{ATH_DD_MAX:g}% below ATH · MC {fmt_money(MCAP_MIN)}–{fmt_money(MCAP_MAX)}"
            f" · ≥{MIN_LOCKED_PCT:g}% supply locked")


def render_message(date_str, blocks, funnel):
    parts = [f"🏔 ATH Zone Scan — {date_str}\n{rules_line()}"]
    for b in blocks:
        if "note" in b:
            parts.append(b["note"])
            continue
        ms = b["matches"]
        n_new = sum(1 for m in ms if m["new"])
        head = f"— {b['name'].upper()} — {len(ms)} in zone" + (f" ({n_new} new)" if n_new else "")
        body = "\n".join(fmt_match(m) for m in ms) if ms else "No coins in the zone today."
        parts.append(f"{head}\n{body}")
        if b["left"]:
            parts.append(f"↩️ Left the zone on {b['name']}: " + ", ".join(b["left"]))
    ex_counts = " · ".join(f"{name} {n}" for name, n in funnel["exchanges"])
    parts.append(f"Funnel: {funnel['mcap']:,} coins {fmt_money(MCAP_MIN)}–{fmt_money(MCAP_MAX)}"
                 f" → {funnel['ath']:,} within {ATH_DD_MAX:g}% of ATH"
                 f" → {funnel['supply']:,} with ≥{MIN_LOCKED_PCT:g}% locked"
                 + (f" → {ex_counts}" if ex_counts else ""))
    return "\n\n".join(parts)


def resolve_exchanges(raw):
    out = []
    for tok in raw.split(","):
        t = tok.strip().lower()
        if t:
            ex = EXCHANGE_ALIASES.get(t, t)
            if ex not in out:
                out.append(ex)
    return out or ["binance"]


# ── Main ───────────────────────────────────────────────────────────
def main():
    today = datetime.now(timezone.utc).date()
    date_str = today.strftime("%d %b %Y")
    exchanges = resolve_exchanges(EXCHANGES_RAW)
    print(f"ATH Zone Scan {today.isoformat()} | {rules_line()} | exchanges={exchanges}"
          f" | key={'yes' if COINGECKO_API_KEY else 'no'} | dry_run={DRY_RUN}")

    print("Fetching market universe from CoinGecko...")
    universe = fetch_universe()
    by_id = {r["id"]: r for r in universe}
    excluded_ids = fetch_excluded_ids()
    print(f"  {len(universe)} coins fetched, {len(excluded_ids)} ids excluded by category")

    in_mcap = [r for r in universe
               if MCAP_MIN <= (r.get("market_cap") or 0) <= MCAP_MAX and not is_excluded(r, excluded_ids)]
    in_ath = [r for r in in_mcap
              if (dd := ath_drawdown_pct(r)) is not None and ATH_DD_MIN <= dd <= ATH_DD_MAX]
    in_supply = [r for r in in_ath
                 if (lp := locked_pct(r)) is not None and lp >= MIN_LOCKED_PCT]
    cand_ids = {r["id"] for r in in_supply}
    print(f"Funnel: {len(in_mcap)} in mcap range → {len(in_ath)} near ATH → {len(in_supply)} supply-locked")

    state = load_state()
    prev_ex = state["exchanges"]
    blocks, all_matches, ex_funnel = [], [], []

    for ex in exchanges:
        name = EXCHANGE_NAMES.get(ex, ex)
        try:
            listings = exchange_listings(ex, cand_ids) if cand_ids else {}
        except Exception as e:
            print(f"[{name}] listing check failed: {type(e).__name__}: {e}")
            blocks.append({"note": f"⚠️ {name}: listing check unavailable ({type(e).__name__}) — previous state kept."})
            continue
        prev = prev_ex.get(ex, {})
        matches = []
        for r in in_supply:
            pairs = listings.get(r["id"])
            if not pairs:
                continue
            first_seen = prev.get(r["id"], {}).get("first_seen") or today.isoformat()
            matches.append(build_match(r, ex, pairs, first_seen, today))
        matches.sort(key=lambda m: (not m["new"], m["dd_pct"]))   # new first, then closest to ATH

        cur_ids = {m["coin_id"] for m in matches}
        left = []
        for cid, info in sorted(prev.items()):
            if cid in cur_ids:
                continue
            row = by_id.get(cid)
            if row is None:
                reason = f"MC < {fmt_money(MCAP_MIN)} or untracked"
            else:
                ok, why = check_rules(row, excluded_ids)
                reason = why if not ok else f"no live pair on {name}"
            left.append(f"{(info.get('symbol') or cid).upper()} ({reason})")

        print(f"[{name}] {len(matches)} in zone ({sum(m['new'] for m in matches)} new), {len(left)} left")
        blocks.append({"exchange": ex, "name": name, "matches": matches, "left": left})
        ex_funnel.append((name, len(matches)))
        prev_ex[ex] = {m["coin_id"]: {"symbol": m["symbol"], "first_seen": m["first_seen"]} for m in matches}
        all_matches += matches

    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    funnel = {"mcap": len(in_mcap), "ath": len(in_ath), "supply": len(in_supply), "exchanges": ex_funnel}
    text = render_message(date_str, blocks, funnel)

    if DRY_RUN:
        print("\n----- DRY RUN: message that would be sent -----\n")
        print(text)
        print("\n----- (no Telegram, no files written) -----")
        return

    send_telegram(text)
    print("Telegram message sent.")
    append_archive(text)
    log_matches(all_matches, today)
    save_state(state)
    print(f"Wrote {STATE_FILE}, {ALERTS_ARCHIVE}, {MATCHES_LOG} ({len(all_matches)} matches).")


if __name__ == "__main__":
    main()
