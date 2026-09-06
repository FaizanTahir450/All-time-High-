# CLAUDE.md — ATH zone scanner

Guidance for future Claude Code sessions working in this repo. Sibling project:
`D:\projects\sweep-scanner` (same owner, same GitHub-Actions → Telegram pattern,
different strategy). Reuse its conventions; do not import its code.

## What this project is

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
| `README.md` | End-user setup guide (Telegram bot, GitHub secrets/variables, manual run, tuning). |

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

Private GitHub repo; Actions runs the daily cron. Commit-back keeps the repo
active so GitHub's 60-day scheduled-workflow pause doesn't trigger.
