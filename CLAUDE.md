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
| `.github/workflows/psx_scan.yml` | Cron `0 13 * * 1-5` (18:00 PKT Mon–Fri) + manual `dry_run`; commits `psx_ath_cache.json`, `psx_state.json`, `psx_alerts_archive.txt`, `psx_matches.jsonl`. `timeout-minutes: 75` for backfill runs. |
| `psx_ath_cache.json` | Committed per-symbol adjusted-ATH cache (~705 entries). Rolled forward each session; rebuilt automatically if deleted. |

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
debt/ETF/rights/pref instruments (`get_equities`), stocks with no trade in
`PSX_MAX_STALE_DAYS`=10 days (held out, not dropped), and dead listings with no
price data anywhere (cached as `nodata`, re-checked monthly).

### Data sources

| Need | Source | Notes |
|------|--------|-------|
| Universe | portal `GET /symbols` | JSON; `isDebt`/`isETF` flags + `_NON_EQUITY` name regex → ~705 equities |
| Full history | SCSTrade `POST /stockscreening/SS_CompanySnapShotHP.aspx/chart` `{par, date1, date2}` | whole history in one call, newest-first, `/Date(ms)/` stamps (PKT). From 2006-01-02 for old companies. ~2 s/symbol; can hang minutes or fail DNS → `timeout=(15,60)` + portal fallback |
| Fallback history | portal `POST /historical {month, year, symbol}` | HTML table, one month/call; `portal_history` walks back ≤24 months, stops after 3 empty months. Marks entry `partial` (shown as `†`) |
| Daily bar | portal `GET /market-watch` | one HTML table for every stock that traded: LDCP, OPEN, HIGH, LOW, CURRENT, VOLUME. **Live intraday during market hours.** On ex-dividend / ex-bonus / ex-rights days and for non-compliant companies the ticker carries a suffix (`ABLXD`, `XB`, `XR`, `AMTEXNC`); `_base_symbol()` maps these back to the equity so the bar (and the XB-day adjustment) is not lost. ETF/pref/debt rows are dropped |
| Session date | `portal_month` of a bellwether (OGDC/MEBL/HUBC/PSO) | newest row = latest session; today's row appears intraday |

### Forming-session guards (mirror sweep-scanner)

`SESSION_CUTOFF` 16:45 PKT. Before it, `drop_forming()` strips today's rows from
every history fetch and `latest_session_date()` returns the previous session;
`market_live_now()` (weekday 09:00–16:45 PKT) disables market-watch entirely
because it is intraday. The scheduled run is 13:00 UTC = 18:00 PKT, so guards only
matter for manual/local runs. Holiday: weekday after cutoff with `sess < today` →
one-line note, state untouched.

### Corporate-action adjustment (`is_corp_action`, `adjusted_ath`, `apply_bar`)

PSX daily limit is ±10 % or Re 1, whichever is larger. An overnight **drop**
`prev_close − ref > 1.5 × limit` (ref = open, else close; in `apply_bar` the
market-watch LDCP is preferred when it differs from the cached close by >1 %,
because PSX publishes the adjusted reference price on ex-dates) is treated as a
bonus/split/rights and the ratio `ref/prev_close` rescales all OLDER bars
(`adjusted_ath` walks newest→oldest with a cumulative factor). Upward gaps are
ignored. Verified 2026-09-07: LUCK raw ATH 1,796 → adjusted 529.50 (2025-12-22,
event 2025-04-28 ×0.2084); SYS 835 → 174.40 (events 2022-03-31 ×0.53,
2025-06-02 ×0.20). Known limits: bonuses under the cap are missed; the Dec-2008
floor removal produces a false ×0.74–0.84 event on some old names (irrelevant
unless the ATH predates 2009).

### Cache & run flow (`main`)

`psx_ath_cache.json` = `{"symbols": {SYM: {last_date, last_close, ath, ath_date,
hist_start, bars, events[≤8], source, partial} | {nodata, checked}},
"last_run_session"}`. Per run: (1) backfill uncached symbols, ≤`PSX_BACKFILL_LIMIT`
within `PSX_BACKFILL_BUDGET` s, saving every 25 (transient errors leave the symbol
uncached for retry; only "both sources empty" becomes `nodata`); (2) roll cached
symbols to `sess`: if the previous run's session is >3 days old (a run was
skipped) → `catch_up()` via SCSTrade from `last_date`, else apply the
market-watch bar; (3) evaluate, sort new-first then dd ascending, diff against
`psx_state.json` for 🆕/left; (4) Telegram → `psx_alerts_archive.txt` →
`psx_matches.jsonl` → state. `DRY_RUN=1` skips Telegram/state/archive/log but
**does save the cache** (so local backfills count). Initial cache was built
locally on 2026-09-07 (~2 s/symbol, 705 symbols) and committed.

### Local testing

```bash
DRY_RUN=1 PSX_BACKFILL_LIMIT=25 PSX_BACKFILL_BUDGET=240 python psx_scanner.py
```
Point `PSX_CACHE_FILE` at a scratch path if you don't want to touch the committed cache.
