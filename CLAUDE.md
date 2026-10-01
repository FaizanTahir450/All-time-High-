# CLAUDE.md — ATH zone scanners (crypto + PSX)

Guidance for future Claude Code sessions working in this repo. Sibling project:
`D:\projects\sweep-scanner` (same owner, same GitHub-Actions → Telegram pattern,
different strategy). Reuse its conventions; do not import its code.

Two scanners share this repo and the Telegram secrets: `scanner.py` (crypto,
CoinGecko) and `psx_scanner.py` (Pakistan Stock Exchange; imports the Telegram
and formatting helpers from `scanner`). See "PSX scanner" at the bottom.

## What the crypto scanner is

A single-file Python scanner that runs **daily on GitHub Actions**, pulls the
CoinGecko market list, keeps the coins that satisfy the owner's **"ATH zone"**
rules, checks they are **listed on Binance** (or other exchanges, optional), and
sends one consolidated **Telegram** message. It remembers which coins were in
the zone last run (`state.json`) so the message can flag 🆕 entries, show days
in zone, and list coins that left. No server, no manual step.

## Strategy — DO NOT change unless the owner explicitly asks

Confirmed with the owner on 2026-09-06 (the original brief was ambiguous; these
are the agreed readings):

| Rule | Value | Implementation |
|------|-------|----------------|
| Distance from ATH | **0–40 % below ATH** (owner chose "up to 40 % below", not a 30–40 % band) | `ath_drawdown_pct()` = `(ath − current_price) / ath × 100`, clamped at 0; `ATH_DD_MIN ≤ dd ≤ ATH_DD_MAX` |
| Market cap | **$10M – $500M** | CoinGecko `market_cap` |
| Supply | **≥ 60 % of supply locked** = at most 40 % circulating (owner confirmed the literal reading; low-float coins) | `locked_pct()` = `(1 − circulating / (max_supply or total_supply)) × 100`; skip if neither supply figure exists |
| Exchange | **Binance** default, others optional | `EXCHANGES` env (comma list); any live non-stale spot pair counts as listed |
| Alerts | **Full list every run, 🆕 for new entries**, plus "left the zone" | `state.json` per exchange: `coin_id → {symbol, first_seen}` |
| Schedule | **Daily 00:15 UTC** | `.github/workflows/scan.yml` cron `15 0 * * *` |

Exclusions: CoinGecko categories `stablecoins`, `tokenized-products`,
`tokenized-stock`, `tokenized-private-credit`, `bittensor-subnets` (fetched each
run, one call each, override via `EXCLUDE_CATEGORIES`), `STABLE_SYMBOLS`, and
names matching wrapped/bridged/staked/restaked/tokenized/xStock. Without these,
the 2026-09-06 dry run's 47 supply-locked candidates were mostly xStocks,
Tradable private-credit tokens and Bittensor `SN##` subnet tokens — none of
which Binance lists, but other exchanges (Bybit, Gate) do list xStocks.

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Everything: CoinGecko client with back-off, universe fetch, rules, exchange-listing check, state, Telegram, archive, matches log. |
| `.github/workflows/scan.yml` | Daily cron + `workflow_dispatch` (inputs: `exchanges`, `dry_run`). Commits `state.json`, `alerts_archive.txt`, `matches.jsonl` back (skipped on dry run). `EXCHANGES` resolves as manual input → repo variable `vars.EXCHANGES` → `binance`. |
| `state.json` | Created on first real run; committed. `{"exchanges": {<ex_id>: {<coin_id>: {"symbol", "first_seen"}}}, "updated"}`. Exchanges not scanned in a run keep their old entry. |
| `alerts_archive.txt` | Append-only copy of each Telegram message (`=====` divider). |
| `matches.jsonl` | One line per (run, exchange, coin): price, ath, ath_date, dd_pct, mcap, mcap_rank, locked_pct, circulating, supply_base, volume_24h, pair, first_seen, new. |
| `README.md` | End-user setup guide (Telegram bot, GitHub secrets/variables, manual run, tuning) for both scanners. |
| `psx_scanner.py` | PSX scanner (see the PSX section below). Imports `send_telegram`, `append_archive`, `fmt_ath_date` from `scanner`. |
| `.github/workflows/psx_scan.yml` | Cron `0 13 * * 1-5` (18:00 PKT Mon–Fri) + manual `dry_run`; commits `psx_ath_cache.json`, `psx_universe.json`, `psx_state.json`, `psx_alerts_archive.txt`, `psx_matches.jsonl`. `timeout-minutes: 75` (normal run ~5 min + ≤30 min session wait + ≤40 min backfill). |
| `psx_ath_cache.json` | Committed per-symbol adjusted-ATH cache (~705 entries). Rolled forward each session; rebuilt automatically if deleted. |
| `psx_universe.json` | Committed `{updated, count, symbols: {SYM: name}}` — every PSX equity ever seen. Seeded 2026-10-01 from the portal's last symbol list (May-2026 snapshot + the Sep-2026 cache) ∪ TradingView; grows each run, never shrinks. |

## Data flow (`main()`)

1. `fetch_universe()` — `/coins/markets` ordered by mcap desc, 250/page, from page 1 until the page's lowest mcap drops below `MCAP_MIN` (≈6 pages today; hard cap `MAX_MARKET_PAGES`=12). Rows deduped by id.
2. `fetch_excluded_ids()` — ids in each `EXCLUDE_CATEGORIES` category, paged by mcap until the floor is under `MCAP_MIN` (≈1 call per category; a failing category is skipped).
3. Funnel filters in order: mcap+exclusions → ATH drawdown → locked supply → `cand_ids`.
4. Per exchange: `exchange_listings(ex_id, cand_ids)` — `/exchanges/{id}/tickers?coin_ids=a,b,…` in batches of 25, paging while a page has 100 tickers. **CoinGecko applies `coin_ids` to base OR target**, so unrelated pairs come back (e.g. every X/BTC pair if `bitcoin` were requested); only tickers whose `coin_id` is in the candidate set count. `is_stale`/`is_anomaly` tickers are ignored. One exchange failing emits a ⚠️ note and leaves its state untouched.
5. Matches sorted new-first then by drawdown ascending; "left" = previous state ids not matched now, annotated by `check_rules()` with the first failing rule (or "no live pair").
6. `render_message()` → Telegram (plain text, no parse_mode, split on line boundaries under 4000 chars) → `append_archive()` → `log_matches()` → `save_state()`. `DRY_RUN=1` prints the message and writes nothing.

## Data source notes

- **CoinGecko public API** `https://api.coingecko.com/api/v3`, no key needed.
  Free tier ≈ 30 req/min with a Demo key, ≈ 10/min without (keyless runs hit
  429 with `Retry-After: 60` at a 2.5 s pause — observed 2026-09-06); the
  client sleeps `REQUEST_PAUSE` after each call (default 6 s keyless, 2 s with
  a key) and honours `Retry-After` on 429/5xx (up to 6 attempts). `COINGECKO_API_KEY` is sent as `x-cg-demo-api-key`
  (Demo keys use the same public host; Pro keys would need `pro-api.coingecko.com` — not implemented).
- Exchange ids differ from display names: MEXC=`mxc`, OKX=`okex`, Bybit=`bybit_spot`,
  Coinbase=`gdax`, HTX=`huobi`. `EXCHANGE_ALIASES` maps friendly names; unknown
  names pass through as raw ids.
- `ath` / `ath_change_percentage` are per vs_currency (USD) across all venues.
  Drawdown is recomputed from `ath` and `current_price` rather than trusting
  `ath_change_percentage`, and clamped at 0 for coins printing a new ATH.
- Full run ≈ 10–15 calls, about a minute.

## Config knobs (env, top of `scanner.py`)

`ATH_DD_MIN` 0 · `ATH_DD_MAX` 40 · `MCAP_MIN` 1e7 · `MCAP_MAX` 5e8 ·
`MIN_LOCKED_PCT` 60 · `EXCHANGES` binance · `COINGECKO_API_KEY` "" ·
`REQUEST_PAUSE` 6 keyless / 2 with key · `MAX_MARKET_PAGES` 12 · `DRY_RUN` 0 ·
`STATE_FILE` state.json · `ALERTS_ARCHIVE` alerts_archive.txt · `MATCHES_LOG` matches.jsonl.

## Secrets — SECURITY

`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (and optional `COINGECKO_API_KEY`) come
**only** from environment variables / GitHub Actions secrets. Never hardcode
them in code, commits, logs or docs.

## Running locally

```bash
pip install -r requirements.txt
DRY_RUN=1 python scanner.py          # PowerShell: $env:DRY_RUN="1"; python scanner.py
```

Use `DRY_RUN=1` for smoke tests — it never sends Telegram or writes files, so
nothing needs to be reverted. For a real local run set the two Telegram env vars;
point `STATE_FILE`/`ALERTS_ARCHIVE`/`MATCHES_LOG` at scratch paths if you don't
want to touch the committed files.

## Deployment

GitHub repo `FaizanTahir450/All-time-High-` (public as of 2026-09-06); Actions
runs the crons. Commit-back keeps the repo active so GitHub's 60-day
scheduled-workflow pause doesn't trigger. Both workflows share the
`ath-zone-scan` concurrency group so their commit-backs never race.

## PSX scanner (`psx_scanner.py`, workflow `psx_scan.yml`)

### Rule — DO NOT change unless the owner explicitly asks

Owner's brief (2026-09-07): "do it simple for PSX stock, just the 0 to 40 % rule
of all-time high, but make sure maximum coverage." So: **one rule**, last close
`0 ≤ dd ≤ 40` % below the **split/bonus-adjusted all-time high**, over **every
listed PSX equity**. No market-cap, float or liquidity filter. Only exclusions:
debt/ETF/rights/pref instruments (portal flags + `_NON_EQUITY` name regex when the
portal answers; `is_share_class()` drops `…PS/…CPS/…PRS/…R` tickers whose base
company is listed, e.g. ASLPS, GCWLPRS, GCWLR), stocks with no trade in
`PSX_MAX_STALE_DAYS`=10 days (held out, not dropped), and dead listings with no
price data anywhere (cached as `nodata`, re-checked every `PSX_RECHECK_DAYS`).

### Data sources — portal outage (2026-09-15 →)

The official portal `dps.psx.com.pk` answers `/symbols`, `/market-watch`,
`/timeseries/eod/*` and `POST /historical` with a branded **HTML 404** for
everyone (HTML pages such as `/company/OGDC` still work). The PSX workflow failed
every weekday from 2026-09-24 until the 2026-10-01 rewrite. The owner rejects
Yahoo for PSX (dividend-adjusted prices drift from exchange prints; ~1 in 8
symbols missing). `portal_symbols()` still probes `/symbols` once per run (15 s
timeout, must be JSON) so the portal rejoins the universe merge if it returns;
nothing else depends on it.

| Need | Source | Notes |
|------|--------|-------|
| Prices (history + daily) | SCS Trade `POST /stockscreening/SS_CompanySnapShotHP.aspx/chart` `{par, date1 "MM/DD/YYYY", date2}` | raw exchange OHLCV, any span in one call, `/Date(ms)/` stamps (PKT midnight). From 2006-01-02 for old companies. ~0.2–1 s/call; `scs_history()` retries 3× on transport/5xx/bad JSON, `timeout=(15,60)`. HTTP 200 with empty `d` = symbol has no prices (→ `nodata`). Verified 2026-10-01 identical to TradingView for 12 symbols |
| Fallback daily bar + universe | TradingView `POST https://scanner.tradingview.com/pakistan/scan` (columns `name, description, type, subtype, time, open, high, low, close, volume, change`; filter `type == stock`) | one call, 481 `common` stocks, 15-min delayed. `time` = the latest bar's open (UTC; PKT date = session). `prev_close = close/(1+change%)` is passed to `apply_bar` as `ldcp` (on ex-dates TradingView's reference is the adjusted previous close). Also the only source of new listings and names now |
| Session date | `scs_bellwether_date()` (OGDC/MEBL/HUBC/PSO, last 14 days) → `tv_session_date()` (most common TradingView bar date, ≥50 rows) | see `latest_session_date()` below |

### Forming-session guards and the session wait

`SESSION_CUTOFF` 16:45 PKT. Before it, `drop_forming()` strips today's rows from
every SCS Trade fetch and `tradingview_scan()` drops a bar dated today. The
scheduled run is 13:00 UTC = 18:00 PKT, so guards only matter for manual/local
runs. `latest_session_date(tv_bars)` returns `(date, source)`: SCS Trade is
authoritative; on a weekday after the cutoff, if SCS Trade lacks today's bar but
TradingView has it, the run polls SCS Trade every `SESSION_POLL`=300 s up to
`PSX_SESSION_WAIT`=1800 s and then falls back to TradingView (`source ==
"tradingview"` → `roll_forward(scs_ok=False)` uses TradingView bars for every
symbol). If TradingView has no newer session either, it is a holiday — no wait.
`(None, None)` = both down → ⚠️ note. Holiday: weekday after cutoff with
`sess < today` → one-line note, state untouched. Outage guard: if more than
`PSX_MAX_FAIL_RATIO` (25 %) of the refreshed symbols failed (≥20 tried) the run
sends a ⚠️ note and keeps the previous zone state.

### Corporate-action adjustment (`is_corp_action`, `adjusted_ath`, `apply_bar`)

PSX daily limit is ±10 % or Re 1, whichever is larger. An overnight **drop**
`prev_close − ref > 1.5 × limit` (ref = open, else close; in `apply_bar` the
`ldcp` argument — TradingView's implied previous close on the fallback path —
is preferred when it differs from the cached close by >1 %, because on ex-dates
it is the adjusted reference price; SCS Trade bars pass no `ldcp`) is treated as a
bonus/split/rights and the ratio `ref/prev_close` rescales all OLDER bars
(`adjusted_ath` walks newest→oldest with a cumulative factor). Upward gaps are
ignored. Verified 2026-09-07: LUCK raw ATH 1,796 → adjusted 529.50 (2025-12-22,
event 2025-04-28 ×0.2084); SYS 835 → 174.40 (events 2022-03-31 ×0.53,
2025-06-02 ×0.20). Known limits: bonuses under the cap are missed; the Dec-2008
floor removal produces a false ×0.74–0.84 event on some old names (irrelevant
unless the ATH predates 2009).

### Cache & run flow (`main`)

`psx_ath_cache.json` = `{"symbols": {SYM: {last_date, last_close, ath, ath_date,
hist_start, bars, events[≤8], source, partial, checked?} | {nodata, checked}},
"last_run_session"}`. Per run: (0) universe = `psx_universe.json` ∪ portal (if
alive) ∪ TradingView, saved when it changed; `tradingview_scan()` also returns
the fallback bars; (1) backfill uncached symbols, ≤`PSX_BACKFILL_LIMIT` within
`PSX_BACKFILL_BUDGET` s, saving every 25 (transport errors leave the symbol
uncached for retry; an empty SCS Trade answer becomes `nodata`); (2)
`roll_forward()`: every cached symbol with `last_date < sess` gets a `catch_up()`
(SCS Trade from `last_date − 3 d`, applies only newer bars, so missed sessions
and double runs are both safe); on a per-symbol SCS failure the TradingView bar
for `sess` is applied instead; symbols idle for more than `PSX_ROLL_WINDOW_DAYS`
(45) are re-probed only every `PSX_RECHECK_DAYS` (30) via the entry's `checked`
date; (3) evaluate, sort new-first then dd ascending, diff against
`psx_state.json` for 🆕/left; (4) Telegram → `psx_alerts_archive.txt` →
`psx_matches.jsonl` → state. `DRY_RUN=1` skips Telegram/state/archive/log but
**does save the cache and universe** (so local backfills count). Initial cache
was built locally on 2026-09-07 (705 symbols) and committed; the 2026-10-01
local dry run caught the cache up from 23 Sep to 1 Oct via SCS Trade.

### Local testing

```bash
DRY_RUN=1 PSX_BACKFILL_LIMIT=25 PSX_BACKFILL_BUDGET=240 python psx_scanner.py
```
Point `PSX_CACHE_FILE` at a scratch path if you don't want to touch the committed cache.
