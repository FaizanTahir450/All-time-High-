"""
PSX ATH Zone Scanner — Pakistan Stock Exchange → Telegram
----------------------------------------------------------
One rule, maximum coverage: every listed PSX equity whose last close is
0–40 % below its split/bonus-adjusted ALL-TIME HIGH. Sends one Telegram
message per session, flags 🆕 entrants, lists stocks that left the zone.

Why "adjusted": PSX price history is unadjusted for bonus shares, splits and
rights. Lucky Cement shows a raw high of 1,796 (pre 2025 split) against a
~430 price — 76 % "below" — while its real high is ~530. PSX caps daily moves
at ±10 % (or Re 1), so an overnight DROP bigger than 1.5× that cap can only be a
corporate action: the scanner detects those gaps and rescales the older history
(see is_corp_action / adjusted_ath). Small bonuses under the cap slip through and
make a stock look slightly further from its high than it is.

Data (all free, no keys). The official PSX Data Portal (dps.psx.com.pk) stopped
serving its data routes (/symbols, /market-watch, POST /historical all answer an
HTML 404) around 2026-09-15, so the scanner now runs on:
  * Prices    — SCS Trade chart API: raw exchange OHLCV, whole history in one
                call (mostly from 2006) and the same call for the daily catch-up.
                Verified identical to the exchange prints / TradingView.
  * Fallback  — TradingView's PSX screener: one call returns every listed common
                stock with its latest session bar (date, OHLCV, change %). Used
                for the daily bar when SCS Trade is down or late, and for the
                session date when SCS Trade has none.
  * Universe  — committed psx_universe.json (seeded from the portal's last symbol
                list), merged every run with TradingView's stock list and, if the
                portal ever answers again, with portal /symbols. Symbols are only
                ever added; preference/rights classes of a listed company
                (ASLPS, GCWLPRS, GCWLR) are dropped.
  * Session   — the last SCS Trade bar of a bellwether (OGDC/MEBL/HUBC/PSO). On a
                weekday after the close the run waits up to PSX_SESSION_WAIT for
                the EOD bar to be published before calling the day a holiday.

Files (committed back by the Action):
  psx_ath_cache.json     per-symbol running adjusted ATH, last close/date, corp-action events
  psx_universe.json      {symbol: name} — every equity ever seen, grows automatically
  psx_state.json         stocks currently in the zone + first-seen date (drives 🆕)
  psx_alerts_archive.txt copy of every Telegram message
  psx_matches.jsonl      one JSON line per (session, stock) in zone

The first run(s) BACKFILL history: PSX_BACKFILL_LIMIT symbols per run within
PSX_BACKFILL_BUDGET seconds, resumable, cache saved every 25 symbols. Until the
backfill is complete the message shows how many stocks are still pending.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required unless DRY_RUN=1);
PSX_ATH_DD_MIN, PSX_ATH_DD_MAX, PSX_MAX_STALE_DAYS, PSX_ROLL_WINDOW_DAYS,
PSX_RECHECK_DAYS, PSX_SESSION_WAIT, PSX_MAX_FAIL_RATIO, PSX_BACKFILL_LIMIT,
PSX_BACKFILL_BUDGET, PSX_REQUEST_PAUSE, PSX_CACHE_FILE, PSX_UNIVERSE_FILE,
PSX_STATE_FILE, PSX_ALERTS_ARCHIVE, PSX_MATCHES_LOG, DRY_RUN (optional).
DRY_RUN=1 sends nothing and leaves state/archive/log untouched, but the history
cache and universe ARE saved so a backfill run is never wasted.
"""

import os
import re
import sys
import json
import time
import collections
from datetime import datetime, date, timedelta, timezone
from datetime import time as dtime

import requests

import scanner as core          # shared Telegram / formatting helpers (same repo)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _env(name, default):
    return os.environ.get(name, default)


# ── Rule & runtime config ──────────────────────────────────────────
ATH_DD_MIN       = float(_env("PSX_ATH_DD_MIN", "0"))        # % below ATH, lower bound
ATH_DD_MAX       = float(_env("PSX_ATH_DD_MAX", "40"))       # % below ATH, upper bound
MAX_STALE_DAYS   = int(_env("PSX_MAX_STALE_DAYS", "10"))     # stock must have traded within N calendar days
ROLL_WINDOW_DAYS = int(_env("PSX_ROLL_WINDOW_DAYS", "45"))   # traded within N days → refreshed every run
RECHECK_DAYS     = int(_env("PSX_RECHECK_DAYS", "30"))       # dormant / no-data symbols re-probed every N days
SESSION_WAIT     = float(_env("PSX_SESSION_WAIT", "1800"))   # s to wait for today's EOD bar on a trading day
SESSION_POLL     = 300                                       # s between session probes while waiting
MAX_FAIL_RATIO   = float(_env("PSX_MAX_FAIL_RATIO", "0.25")) # > this share of refresh failures ⇒ source outage
BACKFILL_LIMIT   = int(_env("PSX_BACKFILL_LIMIT", "400"))    # symbols to backfill per run
BACKFILL_BUDGET  = float(_env("PSX_BACKFILL_BUDGET", "2400"))  # seconds of backfill per run (40 min)
REQUEST_PAUSE    = float(_env("PSX_REQUEST_PAUSE", "0.2"))   # s between SCS Trade requests
GAP_LIMIT_MULT   = 1.5                                       # gap > 1.5× the daily limit ⇒ corporate action
DRY_RUN          = _env("DRY_RUN", "0") == "1"

CACHE_FILE    = _env("PSX_CACHE_FILE", "psx_ath_cache.json")
UNIVERSE_FILE = _env("PSX_UNIVERSE_FILE", "psx_universe.json")
STATE_FILE    = _env("PSX_STATE_FILE", "psx_state.json")
ARCHIVE       = _env("PSX_ALERTS_ARCHIVE", "psx_alerts_archive.txt")
MATCHES_LOG   = _env("PSX_MATCHES_LOG", "psx_matches.jsonl")

PSX_BASE = "https://dps.psx.com.pk"                          # data routes dead since ~2026-09-15; probed once per run
SCS_BASE = "https://www.scstrade.com"
TV_SCAN  = "https://scanner.tradingview.com/pakistan/scan"
PKT = timezone(timedelta(hours=5))
BELLWETHERS = ("OGDC", "MEBL", "HUBC", "PSO")             # any of these trades every session
SESSION_CUTOFF = dtime(16, 45)      # PKT: before this, today's bar may still be forming → dropped

_NON_EQUITY = re.compile(r"\((?:r\d*|right|prs|ptc|[^)]*pref[^)]*)\)", re.I)   # portal name tags: rights / prefs
_SHARE_CLASS_SUFFIXES = ("CPS", "PRS", "PS", "R")   # preference / rights tickers of a listed company (ASLPS, GCWLR)

TV_COLUMNS = ["name", "description", "type", "subtype", "time",
              "open", "high", "low", "close", "volume", "change"]

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (psx-ath-scanner)"})


def now_pkt():
    return datetime.now(PKT)


def session_closed_now():
    """True once today's session is final (after SESSION_CUTOFF PKT)."""
    return now_pkt().time() >= SESSION_CUTOFF


def drop_forming(rows):
    """Remove today's bar from a history list while today's session may still be forming."""
    if session_closed_now():
        return rows
    today = now_pkt().date().isoformat()
    return [r for r in rows if r[0] < today]


def _ms_to_pkt_date(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(PKT).date().isoformat()


# ── SCS Trade (price source) ───────────────────────────────────────
def scs_history(sym, start="01/01/1990", tries=3):
    """Daily bars from `start` (MM/DD/YYYY) to tomorrow -> ascending rows (date, o, h, l, c, v).

    Raw exchange prices, one call for any span. Empty list = SCS Trade answered and
    has no prices for the symbol. Transport / 5xx / bad-JSON errors are retried
    `tries` times and then raised.
    """
    end = (now_pkt() + timedelta(days=1)).strftime("%m/%d/%Y")
    err = None
    for attempt in range(tries):
        try:
            r = session.post(f"{SCS_BASE}/stockscreening/SS_CompanySnapShotHP.aspx/chart",
                             json={"par": sym, "date1": start, "date2": end},
                             headers={"Content-Type": "application/json"}, timeout=(15, 60))
            if r.status_code == 429 or r.status_code >= 500:
                raise requests.HTTPError(f"HTTP {r.status_code}", response=r)
            r.raise_for_status()
            data = r.json().get("d") or []
            break
        except (requests.RequestException, ValueError) as e:
            err = e
            if attempt < tries - 1:
                time.sleep(2.0 * (attempt + 1))
    else:
        raise err
    rows = {}
    for x in data:
        m = re.search(r"-?\d+", x.get("trading_Date") or "")
        c, h = x.get("trading_close"), x.get("trading_high")
        if not m or not c or c <= 0:
            continue
        d = _ms_to_pkt_date(int(m.group()))
        rows[d] = (d, float(x.get("trading_open") or 0), float(h or c), float(x.get("trading_low") or 0),
                   float(c), int(x.get("trading_vol") or 0))
    return [rows[k] for k in sorted(rows)]


# ── TradingView screener (universe + fallback daily bar) ───────────
def tradingview_scan(tries=2):
    """(stocks {sym: name}, bars {sym: (date, o, h, l, c, v, prev_close)}) for every PSX common stock.

    One POST. `time` is the latest bar's open time (UTC); its PKT date is the session
    the OHLC belongs to. A bar for today is dropped while the session may still be
    forming. prev_close is derived from `change` %: on ex-dates TradingView's reference
    is the adjusted previous close, which is exactly what apply_bar() wants as `ldcp`.
    """
    body = {"columns": TV_COLUMNS, "filter": [{"left": "type", "operation": "equal", "right": "stock"}],
            "range": [0, 5000], "sort": {"sortBy": "name", "sortOrder": "asc"}}
    err = None
    for attempt in range(tries):
        try:
            r = session.post(TV_SCAN, json=body, timeout=(15, 60))
            r.raise_for_status()
            data = r.json().get("data") or []
            break
        except (requests.RequestException, ValueError) as e:
            err = e
            if attempt < tries - 1:
                time.sleep(3.0)
    else:
        raise err
    stocks, bars = {}, {}
    today = now_pkt().date().isoformat()
    final = session_closed_now()
    for x in data:
        d = x.get("d") or []
        if len(d) < len(TV_COLUMNS):
            continue
        sym, desc, typ, sub, t, o, h, l, c, v, chg = d[:len(TV_COLUMNS)]
        if typ != "stock" or sub != "common" or not sym:
            continue
        stocks[sym] = (desc or "").strip()
        if not t or not c or c <= 0:
            continue
        bar_date = datetime.fromtimestamp(int(t), tz=timezone.utc).astimezone(PKT).date().isoformat()
        if bar_date == today and not final:
            continue
        prev = c / (1 + chg / 100.0) if (chg is not None and chg > -100) else 0.0
        bars[sym] = (bar_date, float(o or 0), float(h or c), float(l or 0), float(c), int(v or 0), round(prev, 4))
    return stocks, bars


def tv_session_date(tv_bars, min_rows=50):
    """Most common bar date across TradingView rows, if at least `min_rows` share it."""
    if not tv_bars:
        return None
    d, n = collections.Counter(b[0] for b in tv_bars.values()).most_common(1)[0]
    return date.fromisoformat(d) if n >= min_rows else None


# ── Universe ───────────────────────────────────────────────────────
def portal_symbols():
    """Portal /symbols if it ever answers again (JSON list); None otherwise (HTML 404 since 2026-09-15)."""
    try:
        r = session.get(f"{PSX_BASE}/symbols", timeout=15)
        if r.status_code != 200 or "json" not in (r.headers.get("Content-Type") or "").lower():
            return None
        rows = r.json()
    except Exception:
        return None
    out = {}
    for x in rows if isinstance(rows, list) else []:
        if x.get("isDebt") or x.get("isETF") or not x.get("symbol"):
            continue
        name = x.get("name") or ""
        if _NON_EQUITY.search(name):
            continue
        out[x["symbol"]] = name
    return out or None


def is_share_class(sym, universe):
    """True for a preference / rights ticker of a company already in the universe (ASLPS → ASL, GCWLR → GCWL)."""
    for suf in _SHARE_CLASS_SUFFIXES:
        if sym.endswith(suf) and len(sym) > len(suf) and sym[:-len(suf)] in universe:
            return True
    return False


def merge_universe(universe, *sources):
    """Add new symbols from each {sym: name} source (names fill in when missing), drop share classes.

    Returns (universe, added) — symbols are never removed by a source going quiet.
    """
    added = []
    for src in sources:
        for sym, name in (src or {}).items():
            if sym not in universe:
                universe[sym] = name or ""
                added.append(sym)
            elif not universe[sym] and name:
                universe[sym] = name
    for sym in [s for s in universe if is_share_class(s, universe)]:
        universe.pop(sym)
    return universe, [s for s in added if s in universe]


def load_universe(path=UNIVERSE_FILE):
    data = load_json(path, {"symbols": {}})
    return dict(data.get("symbols") or {})


def save_universe(universe, path=UNIVERSE_FILE):
    save_json({"updated": now_pkt().date().isoformat(), "count": len(universe),
               "symbols": dict(sorted(universe.items()))}, path)


# ── Session date ───────────────────────────────────────────────────
def scs_bellwether_date():
    """Newest closed session per SCS Trade (first bellwether with bars), or None if SCS Trade is unreachable."""
    start = (now_pkt().date() - timedelta(days=14)).strftime("%m/%d/%Y")
    for sym in BELLWETHERS:
        try:
            rows = drop_forming(scs_history(sym, start=start, tries=2))
        except Exception as e:
            print(f"  session probe {sym}: {type(e).__name__}")
            continue
        if rows:
            return date.fromisoformat(rows[-1][0])
    return None


def latest_session_date(tv_bars):
    """(session_date, source) — the newest CLOSED session and which source vouches for it.

    SCS Trade is authoritative. On a weekday after the cutoff a session is expected
    today: if SCS Trade does not have it yet but TradingView does, wait (polling SCS
    Trade every SESSION_POLL s, up to SESSION_WAIT) and finally fall back to
    TradingView. If neither has a newer session than SCS Trade's last bar, the day is
    a holiday and there is nothing to wait for. (None, None) = both sources down.
    """
    today = now_pkt().date()
    expect_today = today.weekday() < 5 and session_closed_now()
    deadline = time.time() + (SESSION_WAIT if expect_today else 0)
    tv_date = tv_session_date(tv_bars)
    while True:
        d = scs_bellwether_date()
        if d is not None and (d >= today or not expect_today):
            return d, "scstrade"
        if d is not None and tv_date is not None and tv_date <= d:
            return d, "scstrade"                       # TradingView has no newer session either → holiday
        if time.time() < deadline:                     # SCS Trade late or down on a trading day → wait
            print(f"  waiting for today's EOD bar (SCS Trade {d}, TradingView {tv_date}) — retry in {SESSION_POLL}s")
            time.sleep(SESSION_POLL)
            continue
        if tv_date is not None and (d is None or tv_date > d):
            return tv_date, "tradingview"
        return d, ("scstrade" if d else None)


# ── Corporate-action adjustment ────────────────────────────────────
def is_corp_action(prev_close, ref):
    """True if the overnight DROP from prev_close to ref exceeds 1.5× PSX's daily limit (10 % or Re 1).

    Such a drop cannot happen through trading, so it is an ex-bonus / split / rights
    adjustment. Upward gaps are ignored (resumed trading after suspension, etc.).
    """
    if prev_close <= 0 or ref <= 0:
        return False
    limit = max(0.10 * prev_close, 1.0)
    return (prev_close - ref) > GAP_LIMIT_MULT * limit


def adjusted_ath(rows):
    """(ath, ath_date, events) over ascending rows, rescaling history at each corporate action.

    Walks newest→oldest keeping a cumulative factor: after a gap between bar i-1 and
    bar i (ratio r = open_i / close_{i-1}), every older bar is multiplied by r.
    """
    factor, ath, ath_date, events = 1.0, 0.0, None, []
    for i in range(len(rows) - 1, -1, -1):
        d, o, h, l, c, v = rows[i]
        if h * factor > ath:
            ath, ath_date = h * factor, d
        if i > 0:
            prev_c = rows[i - 1][4]
            ref = o if o > 0 else c
            if is_corp_action(prev_c, ref):
                r = ref / prev_c
                factor *= r
                events.append([d, round(r, 4)])
    return ath, ath_date, events


def entry_from_rows(rows, source, partial):
    if not rows:
        return None
    ath, ath_date, events = adjusted_ath(rows)
    if ath <= 0:
        return None
    last = rows[-1]
    return {"last_date": last[0], "last_close": last[4], "ath": round(ath, 4), "ath_date": ath_date,
            "hist_start": rows[0][0], "bars": len(rows), "events": events[:8],
            "source": source, "partial": partial}


def backfill_symbol(sym):
    """(entry, status) from SCS Trade: "ok", or "nodata" when it answered and has no prices.

    Transport errors propagate (the caller leaves the symbol uncached and retries next run).
    """
    rows = drop_forming(scs_history(sym))
    if rows:
        return entry_from_rows(rows, "scstrade", False), "ok"
    return None, "nodata"


def apply_bar(entry, d, o, h, c, ldcp=0.0):
    """Roll the entry forward by one session bar, detecting a corporate action against the previous close."""
    prev = entry["last_close"]
    ref = ldcp if (ldcp > 0 and prev > 0 and abs(ldcp / prev - 1) > 0.01) else (o if o > 0 else c)
    if is_corp_action(prev, ref):
        r = ref / prev
        entry["ath"] = round(entry["ath"] * r, 4)
        entry.setdefault("events", []).insert(0, [d, round(r, 4)])
        entry["events"] = entry["events"][:8]
    if h > entry["ath"]:
        entry["ath"], entry["ath_date"] = round(h, 4), d
    entry["last_close"], entry["last_date"] = c, d
    entry["bars"] = entry.get("bars", 0) + 1


def catch_up(sym, entry):
    """Pull every bar after last_date from SCS Trade and apply it. Returns bars applied."""
    start = (date.fromisoformat(entry["last_date"]) - timedelta(days=3)).strftime("%m/%d/%Y")
    rows = drop_forming(scs_history(sym, start=start))
    n = 0
    for d, o, h, l, c, v in rows:
        if d > entry["last_date"]:
            apply_bar(entry, d, o, h, c)
            n += 1
    return n


def roll_forward(syms, universe, sess, tv_bars, scs_ok=True):
    """Bring cached symbols up to `sess`: SCS Trade catch-up per symbol, TradingView's bar as fallback.

    Symbols that traded within ROLL_WINDOW_DAYS are refreshed every run; staler
    (dormant / suspended) ones are re-probed every RECHECK_DAYS so a stock that
    resumes trading comes back by itself. Returns counters.
    """
    today = now_pkt().date()
    sess_iso = sess.isoformat()
    n = {"tried": 0, "scs": 0, "tv": 0, "bars": 0, "fail": 0, "dormant_skipped": 0}
    for s in sorted(syms):
        e = syms[s]
        if e.get("nodata") or s not in universe:
            continue
        ld = date.fromisoformat(e["last_date"])
        if ld >= sess:
            continue
        if (sess - ld).days > ROLL_WINDOW_DAYS:
            chk = e.get("checked")
            if chk and (today - date.fromisoformat(chk)).days < RECHECK_DAYS:
                n["dormant_skipped"] += 1
                continue
            e["checked"] = today.isoformat()
        n["tried"] += 1
        done = False
        if scs_ok:
            try:
                n["bars"] += catch_up(s, e)
                n["scs"] += 1
                done = True
            except Exception as ex:
                print(f"  [{s}] SCS Trade catch-up failed ({type(ex).__name__})")
            time.sleep(REQUEST_PAUSE)
        bar = tv_bars.get(s)
        if not done and bar and bar[0] == sess_iso and bar[0] > e["last_date"]:
            apply_bar(e, bar[0], bar[1], bar[2], bar[4], bar[6])
            n["tv"] += 1
            n["bars"] += 1
            done = True
        if not done:
            n["fail"] += 1
    return n


# ── Cache / state / logs ───────────────────────────────────────────
def load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_json(obj, path):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=0, sort_keys=True, separators=(",", ":"))
        f.write("\n")
    os.replace(tmp, path)


def log_matches(matches, session_date, path=MATCHES_LOG):
    with open(path, "a", encoding="utf-8") as f:
        for m in matches:
            rec = {"session": session_date.isoformat(), **{k: v for k, v in m.items() if k != "days_in_zone"}}
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")


# ── Formatting ─────────────────────────────────────────────────────
def fmt_px(p):
    return f"{p:,.2f}"


def fmt_line(m):
    flag = "🆕" if m["new"] else "•"
    mark = "†" if m.get("partial") else ""
    when = core.fmt_ath_date(m["ath_date"])
    tail = "" if m["new"] else f" · {m['days_in_zone']}d"
    return f"{flag} {m['symbol']}{mark} {fmt_px(m['close'])} · ▼{m['dd_pct']:.1f}% · ATH {fmt_px(m['ath'])} ({when}){tail}"


def render(header, matches, left, cov, pending_note):
    parts = [header]
    n_new = sum(1 for m in matches if m["new"])
    title = f"In zone: {len(matches)}" + (f" ({n_new} new)" if n_new else "")
    body = "\n".join(fmt_line(m) for m in matches) if matches else "No stocks in the zone this session."
    parts.append(f"{title}\n{body}")
    if any(m.get("partial") for m in matches):
        parts.append("† = price history only partly available (recent years), so the ATH may be understated.")
    if left:
        parts.append("↩️ Left the zone: " + ", ".join(left))
    if pending_note:
        parts.append(pending_note)
    parts.append(cov)
    return "\n\n".join(parts)


def fail_reason(entry, sess):
    if entry is None or entry.get("nodata"):
        return "no price history"
    if (sess - date.fromisoformat(entry["last_date"])).days > MAX_STALE_DAYS:
        return f"no trade since {core.fmt_ath_date(entry['last_date'])}"
    dd = (entry["ath"] - entry["last_close"]) / entry["ath"] * 100
    return f"▼{dd:.0f}%"


# ── Main ───────────────────────────────────────────────────────────
def main():
    today = now_pkt().date()
    print(f"PSX ATH Zone {today.isoformat()} | {ATH_DD_MIN:g}–{ATH_DD_MAX:g}% below adjusted ATH"
          f" | backfill ≤{BACKFILL_LIMIT}/{BACKFILL_BUDGET:.0f}s | dry_run={DRY_RUN}")

    # Universe: committed list + portal (if alive) + TradingView; TradingView also gives fallback bars
    universe = load_universe()
    before = dict(universe)
    portal = portal_symbols()
    try:
        tv_stocks, tv_bars = tradingview_scan()
    except Exception as e:
        print(f"TradingView screener unavailable ({type(e).__name__}) — universe/bars from cache only")
        tv_stocks, tv_bars = {}, {}
    universe, added = merge_universe(universe, portal, tv_stocks)
    print(f"Universe: {len(universe)} PSX equities (committed {len(before)}"
          f" + portal {'alive' if portal else 'dead'} + TradingView {len(tv_stocks)}; {len(added)} new)")
    if added:
        print("  new symbols: " + ", ".join(added[:30]))
    if universe != before:
        save_universe(universe)

    sess, src = latest_session_date(tv_bars)
    print(f"Latest closed session: {sess} (via {src})")

    cache = load_json(CACHE_FILE, {"symbols": {}, "last_run_session": None})
    syms = cache["symbols"]

    # 1) Backfill symbols we have never seen (resumable; budgeted). Retry "nodata" ones every RECHECK_DAYS.
    todo = [s for s in sorted(universe)
            if s not in syms or (syms[s].get("nodata")
                                 and (today - date.fromisoformat(syms[s]["checked"])).days >= RECHECK_DAYS)]
    t0, done, ok = time.time(), 0, 0
    for s in todo:
        if done >= BACKFILL_LIMIT or time.time() - t0 > BACKFILL_BUDGET:
            break
        try:
            e, status = backfill_symbol(s)
        except Exception as ex:
            print(f"  [{s}] backfill error {type(ex).__name__}")
            e, status = None, "error"
        if e:
            syms[s] = e
        elif status == "nodata":
            syms[s] = {"nodata": True, "checked": today.isoformat()}
        # status "error": leave uncached so the next run retries it
        done += 1
        ok += bool(e)
        if done % 25 == 0:
            save_json(cache, CACHE_FILE)
            print(f"  backfill {done}/{len(todo)} ({ok} with data, {time.time() - t0:.0f}s)")
        time.sleep(REQUEST_PAUSE)
    if done:
        save_json(cache, CACHE_FILE)
        print(f"Backfill this run: {done} symbols, {ok} with data, {time.time() - t0:.0f}s")
    pending = [s for s in universe if s not in syms]

    # 2) Roll cached symbols forward to the latest session
    n = {"tried": 0, "scs": 0, "tv": 0, "bars": 0, "fail": 0, "dormant_skipped": 0}
    if sess:
        t1 = time.time()
        n = roll_forward(syms, universe, sess, tv_bars, scs_ok=(src == "scstrade"))
        print(f"Rolled forward to {sess}: {n['scs']} via SCS Trade, {n['tv']} via TradingView, {n['bars']} bars,"
              f" {n['fail']} failed, {n['dormant_skipped']} dormant skipped ({time.time() - t1:.0f}s)")
        cache["last_run_session"] = sess.isoformat()
    save_json(cache, CACHE_FILE)
    outage = n["tried"] >= 20 and n["fail"] > MAX_FAIL_RATIO * n["tried"]

    # 3) Guards — no session / source outage / holiday: send a one-liner and keep state
    hdr = f"📈 PSX ATH Zone — {today:%d %b %Y}" + (f" (session {sess:%d %b})" if sess else "")
    hdr += f"\nRule: {ATH_DD_MIN:g}–{ATH_DD_MAX:g}% below all-time high (split/bonus-adjusted) · all PSX equities"
    holiday = sess is not None and sess < today and today.weekday() < 5 and session_closed_now()
    if sess is None or outage or holiday:
        if sess is None:
            note = "⚠️ PSX price sources unavailable this run (SCS Trade and TradingView both failed)."
        elif outage:
            note = (f"⚠️ Price refresh incomplete: {n['fail']} of {n['tried']} stocks could not be updated"
                    f" — zone list kept from the previous session.")
        else:
            note = f"PSX market closed today (last session {sess:%d %b %Y}) — no new candle."
        if pending:
            note += f"\nHistory backfill in progress: {len(pending)} of {len(universe)} stocks still pending."
        print(note)
        if not DRY_RUN:
            core.send_telegram(f"{hdr}\n\n{note}")
            core.append_archive(f"{hdr}\n\n{note}", ARCHIVE)
        return

    # 4) Evaluate the rule
    state = load_json(STATE_FILE, {"zone": {}})
    prev = state["zone"]
    matches, active, with_hist, traded = [], 0, 0, 0
    for s in sorted(universe):
        e = syms.get(s)
        if not e or e.get("nodata") or e["ath"] <= 0:
            continue
        with_hist += 1
        if e["last_date"] == sess.isoformat():
            traded += 1
        if (sess - date.fromisoformat(e["last_date"])).days > MAX_STALE_DAYS:
            continue
        active += 1
        dd = max(0.0, (e["ath"] - e["last_close"]) / e["ath"] * 100)
        if not (ATH_DD_MIN <= dd <= ATH_DD_MAX):
            continue
        first_seen = prev.get(s, {}).get("first_seen") or sess.isoformat()
        fs = date.fromisoformat(first_seen)
        matches.append({"symbol": s, "name": universe[s], "close": e["last_close"], "last_date": e["last_date"],
                        "ath": e["ath"], "ath_date": e["ath_date"], "dd_pct": round(dd, 2),
                        "hist_start": e["hist_start"], "partial": e.get("partial", False),
                        "first_seen": first_seen, "days_in_zone": (sess - fs).days, "new": fs == sess})
    matches.sort(key=lambda m: (not m["new"], m["dd_pct"]))
    cur = {m["symbol"] for m in matches}
    left = [f"{s} ({fail_reason(syms.get(s), sess)})" for s in sorted(prev) if s not in cur]

    cov = (f"Coverage: {len(universe)} equities · {with_hist} with history · {active} active"
           f" · {traded} traded this session · {len(matches)} in zone")
    pending_note = (f"History backfill in progress: {len(pending)} of {len(universe)} stocks still pending"
                    f" — they will appear once loaded." if pending else "")
    text = render(hdr, matches, left, cov, pending_note)
    print(f"[PSX] {len(matches)} in zone ({sum(m['new'] for m in matches)} new), {len(left)} left, {len(pending)} pending")

    if DRY_RUN:
        print("\n----- DRY RUN: message that would be sent -----\n")
        print(text)
        print("\n----- (no Telegram; state/archive/log untouched; cache + universe saved) -----")
        return

    core.send_telegram(text)
    print("Telegram message sent.")
    core.append_archive(text, ARCHIVE)
    log_matches(matches, sess)
    state["zone"] = {m["symbol"]: {"first_seen": m["first_seen"]} for m in matches}
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(state, STATE_FILE)
    print(f"Wrote {STATE_FILE}, {ARCHIVE}, {MATCHES_LOG}, {CACHE_FILE}, {UNIVERSE_FILE}.")


if __name__ == "__main__":
    main()
