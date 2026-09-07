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

Data (all free, no keys):
  * Universe   — PSX Data Portal /symbols (equities only: no debt, ETFs, rights, prefs)
  * History    — SCSTrade chart API, whole history in one call (mostly from 2006);
                 portal /historical (one month per call) is the fallback
  * Daily bar  — portal /market-watch: OHLC + previous close for every traded stock, one call
  * Session    — portal /historical for a bellwether symbol tells the latest session date

Files (committed back by the Action):
  psx_ath_cache.json     per-symbol running adjusted ATH, last close/date, corp-action events
  psx_state.json         stocks currently in the zone + first-seen date (drives 🆕)
  psx_alerts_archive.txt copy of every Telegram message
  psx_matches.jsonl      one JSON line per (session, stock) in zone

The first run(s) BACKFILL history: PSX_BACKFILL_LIMIT symbols per run within
PSX_BACKFILL_BUDGET seconds, resumable, cache saved every 25 symbols. Until the
backfill is complete the message shows how many stocks are still pending.

Env: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (required unless DRY_RUN=1);
PSX_ATH_DD_MIN, PSX_ATH_DD_MAX, PSX_MAX_STALE_DAYS, PSX_BACKFILL_LIMIT,
PSX_BACKFILL_BUDGET, PSX_PORTAL_MONTHS, PSX_REQUEST_PAUSE, PSX_CACHE_FILE,
PSX_STATE_FILE, PSX_ALERTS_ARCHIVE, PSX_MATCHES_LOG, DRY_RUN (optional).
DRY_RUN=1 sends nothing and leaves state/archive/log untouched, but the history
cache IS saved so a backfill run is never wasted.
"""

import os
import re
import sys
import json
import time
from datetime import datetime, date, timedelta, timezone

import requests
from datetime import time as dtime

import scanner as core          # shared Telegram / formatting helpers (same repo)

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _env(name, default):
    return os.environ.get(name, default)


# ── Rule & runtime config ──────────────────────────────────────────
ATH_DD_MIN      = float(_env("PSX_ATH_DD_MIN", "0"))       # % below ATH, lower bound
ATH_DD_MAX      = float(_env("PSX_ATH_DD_MAX", "40"))      # % below ATH, upper bound
MAX_STALE_DAYS  = int(_env("PSX_MAX_STALE_DAYS", "10"))    # stock must have traded within N calendar days
BACKFILL_LIMIT  = int(_env("PSX_BACKFILL_LIMIT", "400"))   # symbols to backfill per run
BACKFILL_BUDGET = float(_env("PSX_BACKFILL_BUDGET", "2400"))  # seconds of backfill per run (40 min)
PORTAL_MONTHS   = int(_env("PSX_PORTAL_MONTHS", "24"))     # portal fallback depth when SCSTrade has nothing
REQUEST_PAUSE   = float(_env("PSX_REQUEST_PAUSE", "0.3"))  # s between history requests
GAP_LIMIT_MULT  = 1.5                                      # gap > 1.5× the daily limit ⇒ corporate action
DRY_RUN         = _env("DRY_RUN", "0") == "1"

CACHE_FILE  = _env("PSX_CACHE_FILE", "psx_ath_cache.json")
STATE_FILE  = _env("PSX_STATE_FILE", "psx_state.json")
ARCHIVE     = _env("PSX_ALERTS_ARCHIVE", "psx_alerts_archive.txt")
MATCHES_LOG = _env("PSX_MATCHES_LOG", "psx_matches.jsonl")

PSX_BASE = "https://dps.psx.com.pk"
SCS_BASE = "https://www.scstrade.com"
PKT = timezone(timedelta(hours=5))
BELLWETHERS = ("OGDC", "MEBL", "HUBC", "PSO")             # any of these trades every session
SESSION_CUTOFF = dtime(16, 45)      # PKT: before this, today's bar on the portal is still forming
MARKET_OPEN    = dtime(9, 0)        # PKT: market-watch shows LIVE intraday prices between open and cutoff

_NON_EQUITY = re.compile(r"\((?:r\d*|right|prs|ptc|[^)]*pref[^)]*)\)", re.I)   # rights / preference share tags
_TR = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_TAG = re.compile(r"<[^>]+>")

session = requests.Session()
session.headers.update({"User-Agent": "Mozilla/5.0 (psx-ath-scanner)"})


def now_pkt():
    return datetime.now(PKT)


def session_closed_now():
    """True once today's session is final on the portal (after SESSION_CUTOFF PKT)."""
    return now_pkt().time() >= SESSION_CUTOFF


def market_live_now():
    """True on a weekday between the open and the cutoff — market-watch is intraday, not a closed bar."""
    now = now_pkt()
    return now.weekday() < 5 and MARKET_OPEN <= now.time() < SESSION_CUTOFF


def drop_forming(rows):
    """Remove today's bar from a history list while today's session is still forming."""
    if session_closed_now():
        return rows
    today = now_pkt().date().isoformat()
    return [r for r in rows if r[0] < today]


def _num(s):
    s = (s or "").replace(",", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0


def _cells(tr_html):
    return [_TAG.sub("", c).strip() for c in _TD.findall(tr_html)]


# ── PSX Data Portal ────────────────────────────────────────────────
def get_equities():
    """symbol -> name for every listed equity (no debt, ETFs, rights or preference shares)."""
    r = session.get(f"{PSX_BASE}/symbols", timeout=30)
    r.raise_for_status()
    out = {}
    for x in r.json():
        if x.get("isDebt") or x.get("isETF"):
            continue
        name = x.get("name") or ""
        if _NON_EQUITY.search(name):
            continue
        out[x["symbol"]] = name
    return out


# Market-watch tags a ticker on special days: ABLXD = ABL ex-dividend, XB = ex-bonus,
# XR = ex-rights, AMTEXNC = AMTEX flagged non-compliant. Map those back to the base
# symbol so the bar (and, on XB days, the bonus adjustment) is not missed.
_MW_SUFFIXES = ("XDXB", "XBXD", "XDXR", "XRXD", "XD", "XB", "XR", "NC")


def _base_symbol(sym, equities):
    if sym in equities:
        return sym
    for suf in _MW_SUFFIXES:
        if sym.endswith(suf) and sym[:-len(suf)] in equities:
            return sym[:-len(suf)]
    return None


def market_watch(equities):
    """base symbol -> dict(ldcp, open, high, low, close, volume) for every equity that traded in the latest session."""
    r = session.get(f"{PSX_BASE}/market-watch", timeout=60)
    r.raise_for_status()
    out = {}
    for tr in _TR.findall(r.text):
        c = _cells(tr)
        # SYMBOL, SECTOR, LISTED IN, LDCP, OPEN, HIGH, LOW, CURRENT, CHANGE, CHANGE (%), VOLUME
        if len(c) < 11 or not c[0] or c[0].upper() == "SYMBOL":
            continue
        base = _base_symbol(c[0], equities)
        if base is None:                       # ETFs, preference shares, debt, unknown tags
            continue
        ldcp, o, h, l, cl, v = _num(c[3]), _num(c[4]), _num(c[5]), _num(c[6]), _num(c[7]), _num(c[10])
        if cl > 0 and base not in out:
            out[base] = {"ldcp": ldcp, "open": o, "high": h if h > 0 else cl, "low": l, "close": cl,
                         "volume": int(v), "tag": c[0][len(base):]}
    return out


def portal_month(sym, year, month):
    """One month of EOD OHLCV from the portal -> ascending rows (date, o, h, l, c, v)."""
    r = session.post(f"{PSX_BASE}/historical", data={"month": month, "year": year, "symbol": sym}, timeout=30)
    r.raise_for_status()
    rows = []
    for tr in _TR.findall(r.text):
        c = _cells(tr)
        if len(c) < 6 or c[0].upper() == "DATE":
            continue
        try:
            d = datetime.strptime(c[0], "%b %d, %Y").date().isoformat()
            o, h, l, cl = (_num(x) for x in c[1:5])
            v = int(_num(c[5]))
        except ValueError:
            continue
        if cl > 0:
            rows.append((d, o, h if h > 0 else cl, l, cl, v))
    return sorted(rows)


def latest_session_date():
    """Newest CLOSED session date on the portal, from a bellwether's current-month table.

    Today's row only counts after SESSION_CUTOFF; before that the last closed session wins.
    """
    now = now_pkt()
    months = [(now.year, now.month)]
    prev = (now.replace(day=1) - timedelta(days=1))
    months.append((prev.year, prev.month))
    for sym in BELLWETHERS:
        for (y, m) in months:
            try:
                rows = drop_forming(portal_month(sym, y, m))
            except Exception:
                continue
            if rows:
                return date.fromisoformat(rows[-1][0])
    return None


# ── SCSTrade (full history) ────────────────────────────────────────
def scs_history(sym, start="01/01/1990"):
    """Whole daily history in one call -> ascending rows (date, o, h, l, c, v). Empty list if none."""
    end = (now_pkt() + timedelta(days=1)).strftime("%m/%d/%Y")
    r = session.post(f"{SCS_BASE}/stockscreening/SS_CompanySnapShotHP.aspx/chart",
                     json={"par": sym, "date1": start, "date2": end},
                     headers={"Content-Type": "application/json"}, timeout=(15, 60))
    r.raise_for_status()
    rows = {}
    for x in r.json().get("d") or []:
        ms = int(re.search(r"-?\d+", x["trading_Date"]).group())
        d = datetime.fromtimestamp(ms / 1000, tz=timezone.utc).astimezone(PKT).date().isoformat()
        c, h = x.get("trading_close"), x.get("trading_high")
        if not c or c <= 0:
            continue
        rows[d] = (d, float(x.get("trading_open") or 0), float(h or c), float(x.get("trading_low") or 0),
                   float(c), int(x.get("trading_vol") or 0))
    return [rows[k] for k in sorted(rows)]


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


def portal_history(sym, months=PORTAL_MONTHS):
    """Fallback: walk back month by month; stop after 3 empty months once some data exists.

    Returns (rows, had_error) — had_error means at least one month request failed, so an
    empty result is NOT proof that the stock has no data.
    """
    now = now_pkt()
    y, m = now.year, now.month
    rows, empty_run, got_any, had_error = [], 0, False, False
    for _ in range(months):
        try:
            part = portal_month(sym, y, m)
        except Exception:
            part, had_error = [], True
        if part:
            rows = part + rows
            got_any, empty_run = True, 0
        else:
            empty_run += 1
            if (got_any and empty_run >= 3) or (not got_any and empty_run >= 6):
                break
        m -= 1
        if m == 0:
            y, m = y - 1, 12
        time.sleep(REQUEST_PAUSE)
    return sorted(set(rows)), had_error


def backfill_symbol(sym):
    """(entry, status) from SCSTrade, falling back to the portal.

    status: "ok" (entry built), "nodata" (both sources answered and have nothing — the
    stock is inactive/delisted), or "error" (a source failed; retry next run, cache nothing).
    """
    scs_failed = False
    try:
        rows = drop_forming(scs_history(sym))
    except Exception as e:
        print(f"  [{sym}] SCSTrade failed ({type(e).__name__}) — trying portal")
        rows, scs_failed = [], True
    if rows:
        return entry_from_rows(rows, "scstrade", False), "ok"
    rows, portal_failed = portal_history(sym)
    rows = drop_forming(rows)
    if rows:
        return entry_from_rows(rows, "portal", True), "ok"
    # The portal is the authority on "this stock has no prices": only trust an empty
    # answer when every portal request actually succeeded.
    return None, ("error" if portal_failed else "nodata")


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
    """Missed sessions (a run was skipped): pull bars after last_date from SCSTrade."""
    start = (date.fromisoformat(entry["last_date"]) - timedelta(days=3)).strftime("%m/%d/%Y")
    rows = drop_forming(scs_history(sym, start=start))
    n = 0
    for d, o, h, l, c, v in rows:
        if d > entry["last_date"]:
            apply_bar(entry, d, o, h, c)
            n += 1
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

    equities = get_equities()
    print(f"Universe: {len(equities)} PSX equities")
    sess = latest_session_date()
    print(f"Latest closed session on portal: {sess}")
    mw = {}
    if market_live_now():
        print("Market is open right now — market-watch is intraday, so today's bar is skipped (cached closes used)")
    else:
        try:
            mw = market_watch(equities)
            tagged = sum(1 for b in mw.values() if b.get("tag"))
            print(f"Market-watch: {len(mw)} equities traded in the latest session ({tagged} under XD/XB/XR/NC tags)")
        except Exception as e:
            print(f"Market-watch unavailable ({type(e).__name__}) — using cached closes")

    cache = load_json(CACHE_FILE, {"symbols": {}, "last_run_session": None})
    syms = cache["symbols"]

    # 1) Backfill symbols we have never seen (resumable; budgeted). Retry "nodata" ones monthly.
    todo = [s for s in sorted(equities)
            if s not in syms or (syms[s].get("nodata") and (today - date.fromisoformat(syms[s]["checked"])).days >= 30)]
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
    pending = [s for s in equities if s not in syms]

    # 2) Roll cached symbols forward to the latest session
    if sess:
        last_run = cache.get("last_run_session")
        missed = bool(last_run) and (sess - date.fromisoformat(last_run)).days > 3   # more than a weekend passed
        n_bar = n_catch = 0
        for s, e in syms.items():
            if e.get("nodata") or s not in equities:
                continue
            ld = date.fromisoformat(e["last_date"])
            if ld >= sess:
                continue
            if missed and (sess - ld).days > 3:
                try:
                    n_catch += catch_up(s, e)
                    time.sleep(REQUEST_PAUSE)
                    continue
                except Exception as ex:
                    print(f"  [{s}] catch-up failed ({type(ex).__name__}) — using today's bar only")
            bar = mw.get(s)
            if bar:
                apply_bar(e, sess.isoformat(), bar["open"], bar["high"], bar["close"], bar["ldcp"])
                n_bar += 1
        print(f"Rolled forward: {n_bar} from market-watch, {n_catch} catch-up bars")
        cache["last_run_session"] = sess.isoformat()
    save_json(cache, CACHE_FILE)

    # 3) Holiday guard — on a weekday after the close with no new session, send a one-liner and keep state
    hdr = f"📈 PSX ATH Zone — {today:%d %b %Y}" + (f" (session {sess:%d %b})" if sess else "")
    hdr += f"\nRule: {ATH_DD_MIN:g}–{ATH_DD_MAX:g}% below all-time high (split/bonus-adjusted) · all PSX equities"
    holiday = sess is not None and sess < today and today.weekday() < 5 and session_closed_now()
    if sess is None or holiday:
        note = ("⚠️ PSX portal unavailable this run." if sess is None
                else f"PSX market closed today (last session {sess:%d %b %Y}) — no new candle.")
        if pending:
            note += f"\nHistory backfill in progress: {len(pending)} of {len(equities)} stocks still pending."
        print(note)
        if not DRY_RUN:
            core.send_telegram(f"{hdr}\n\n{note}")
            core.append_archive(f"{hdr}\n\n{note}", ARCHIVE)
        return

    # 4) Evaluate the rule
    state = load_json(STATE_FILE, {"zone": {}})
    prev = state["zone"]
    matches, active, with_hist = [], 0, 0
    for s in sorted(equities):
        e = syms.get(s)
        if not e or e.get("nodata") or e["ath"] <= 0:
            continue
        with_hist += 1
        if (sess - date.fromisoformat(e["last_date"])).days > MAX_STALE_DAYS:
            continue
        active += 1
        dd = max(0.0, (e["ath"] - e["last_close"]) / e["ath"] * 100)
        if not (ATH_DD_MIN <= dd <= ATH_DD_MAX):
            continue
        first_seen = prev.get(s, {}).get("first_seen") or sess.isoformat()
        fs = date.fromisoformat(first_seen)
        matches.append({"symbol": s, "name": equities[s], "close": e["last_close"], "last_date": e["last_date"],
                        "ath": e["ath"], "ath_date": e["ath_date"], "dd_pct": round(dd, 2),
                        "hist_start": e["hist_start"], "partial": e.get("partial", False),
                        "first_seen": first_seen, "days_in_zone": (sess - fs).days, "new": fs == sess})
    matches.sort(key=lambda m: (not m["new"], m["dd_pct"]))
    cur = {m["symbol"] for m in matches}
    left = [f"{s} ({fail_reason(syms.get(s), sess)})" for s in sorted(prev) if s not in cur]

    cov = (f"Coverage: {len(equities)} equities · {with_hist} with history · {active} active"
           + (f" · {len(mw)} traded this session" if mw else "") + f" · {len(matches)} in zone")
    pending_note = (f"History backfill in progress: {len(pending)} of {len(equities)} stocks still pending"
                    f" — they will appear once loaded." if pending else "")
    text = render(hdr, matches, left, cov, pending_note)
    print(f"[PSX] {len(matches)} in zone ({sum(m['new'] for m in matches)} new), {len(left)} left, {len(pending)} pending")

    if DRY_RUN:
        print("\n----- DRY RUN: message that would be sent -----\n")
        print(text)
        print("\n----- (no Telegram; state/archive/log untouched; cache saved) -----")
        return

    core.send_telegram(text)
    print("Telegram message sent.")
    core.append_archive(text, ARCHIVE)
    log_matches(matches, sess)
    state["zone"] = {m["symbol"]: {"first_seen": m["first_seen"]} for m in matches}
    state["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_json(state, STATE_FILE)
    print(f"Wrote {STATE_FILE}, {ARCHIVE}, {MATCHES_LOG}, {CACHE_FILE}.")


if __name__ == "__main__":
    main()
