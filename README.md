# ATH Zone Scanners (Crypto + PSX → Telegram)

Two scanners in one repo, both free on GitHub Actions, both posting to the same
Telegram chat: a **crypto** screener (this section) and a **Pakistan Stock
Exchange** screener (see *PSX stocks* below).

## Crypto

Every day this finds the coins that are **close to their all-time high** while
still being **small/mid caps** with **most of their supply locked**, and that are
**listed on Binance** (other exchanges optional). It sends you one Telegram
message and marks which coins are new to the zone since yesterday. Runs free on
GitHub Actions — no PC, no server, nothing manual.

**The rules (defaults — every one is tunable, see below):**

| Rule | Default | How it is measured |
|------|---------|--------------------|
| Distance from ATH | **0 % – 40 % below ATH** | `(ath − price) / ath`, CoinGecko all-time high in USD |
| Market cap | **$10M – $500M** | CoinGecko circulating market cap |
| Locked supply | **≥ 60 % not circulating** | `1 − circulating / (max_supply, else total_supply)` — coins with no supply data are skipped |
| Exchange listing | **Binance** | At least one live spot pair on the exchange (any quote: USDT, BTC, …) |

Stablecoins, tokenized stocks and other tokenized real-world assets, Bittensor
subnet tokens, and wrapped / bridged / staked tokens are excluded automatically.

**Schedule:** daily at **00:15 UTC** (05:15 PKT). Manual runs any time from the
Actions tab, where you can also pick other exchanges or do a dry run.

**What the message looks like:**

```
🏔 ATH Zone Scan — 06 Sep 2026
Rules: 0–40% below ATH · MC $10.0M–$500M · ≥60% supply locked

— BINANCE — 4 in zone (1 new)
🆕 ABC (Abc Protocol) — $1.2345  [ABC/USDT]
     ▼12.3% from ATH $1.41 (2 Sep 26) · MC $45.2M · 71% locked · new today
• XYZ (Xyz Network) — $0.0456  [XYZ/USDT]
     ▼38.9% from ATH $0.0746 (14 Mar 25) · MC $210M · 64% locked · in zone 14d
...

↩️ Left the zone on Binance: DEF (▼44% ATH), GHI (MC $520M)

Funnel: 1,240 coins $10.0M–$500M → 96 within 40% of ATH → 23 with ≥60% locked → Binance 4
```

- 🆕 = was not in the zone on the previous run. New coins are listed first,
  then the rest sorted closest-to-ATH first.
- The date next to the ATH tells you whether the high is old (a real recovery)
  or days old (a brand-new listing sits at its ATH by definition).
- "Left the zone" tells you which coins dropped out and which rule they now fail.
- The funnel line shows how many coins survived each rule, so you can see at a
  glance whether a filter is too tight or too loose.

**Files the Action commits back each run** (expect small automated commits):

| File | Purpose |
|------|---------|
| `state.json` | Coins currently in the zone per exchange, with the date each one first appeared. This is what drives the 🆕 flag and "days in zone". |
| `alerts_archive.txt` | Plain-text copy of every Telegram message ever sent. |
| `matches.jsonl` | One JSON line per (run, exchange, coin) with price, ATH, drawdown, market cap, locked %, supply numbers and 24h volume — raw material for later analysis. |

---

## PSX stocks (Pakistan Stock Exchange)

`psx_scanner.py` applies **one rule** to every listed PSX equity: the last close
is **0 % – 40 % below the all-time high**. No market-cap or float filter, so the
coverage is the whole exchange.

- **Universe:** all listed equities from the official PSX Data Portal, about 705
  tickers, excluding debt instruments, ETFs, rights and preference shares. A
  stock that has not traded for 10 calendar days is held out until it trades
  again, and a handful of dead listings with no price history are skipped.
- **All-time high, adjusted for splits and bonuses.** PSX price history is *not*
  adjusted for bonus shares, splits or rights issues. Lucky Cement, for example,
  shows a raw high of 1,796 from before its 2025 split against a price near 430,
  which would read as 76 % below its high. The scanner fixes this: PSX caps daily
  moves at 10 % (or one rupee), so an overnight drop bigger than 1.5× that cap can
  only be a corporate action. Those gaps are detected and the older history is
  rescaled, giving Lucky a true high near 530. Small bonuses under the cap are not
  caught and make a stock look slightly further from its high than it really is.
- **History depth:** SCSTrade supplies the full daily history in one call, from
  2006 for older companies and from listing for newer ones. The portal is the
  fallback, one month per call; stocks whose history came only from the portal are
  marked `†` in the message because their high may be understated.
- **Schedule:** **18:00 PKT Monday to Friday**, after the close and the portal's
  end-of-day publish. On a market holiday you get a one-line "market closed" note.
  Manual run: Actions → **PSX ATH Zone Scan** → Run workflow.

What the message looks like:

```
📈 PSX ATH Zone — 07 Sep 2026 (session 07 Sep)
Rule: 0–40% below all-time high (split/bonus-adjusted) · all PSX equities

In zone: 163 (4 new)
🆕 OGDC 328.80 · ▼6.6% · ATH 352.00 (6 Jul 26)
• MEBL 565.46 · ▼6.5% · ATH 605.00 (11 Aug 26) · 21d
• LUCK 433.12 · ▼18.2% · ATH 529.50 (22 Dec 25) · 21d
...

↩️ Left the zone: AIRLINK (▼42%), XYZ (no trade since 25 Aug 26)

Coverage: 705 equities · 690 with history · 560 active · 497 traded this session · 163 in zone
```

New entrants come first, then closest-to-high first. The number of days at the
end is how long the stock has been in the zone. Long lists are split across
several Telegram messages automatically.

**First run and history cache.** The per-stock adjusted high lives in
`psx_ath_cache.json`, committed to the repo, and is rolled forward each session
from the portal's market-watch page in a single call. The initial history load
was done once and shipped with the repo. If a new stock lists, or the cache is
ever deleted, the scanner backfills up to 400 stocks per run within a 40-minute
budget and says in the message how many are still pending.

Files the PSX Action commits back: `psx_ath_cache.json`, `psx_state.json`
(stocks in the zone and first-seen dates), `psx_alerts_archive.txt`,
`psx_matches.jsonl`.

PSX tuning knobs (environment variables, same mechanism as the crypto table
below): `PSX_ATH_DD_MIN` (0), `PSX_ATH_DD_MAX` (40), `PSX_MAX_STALE_DAYS` (10),
`PSX_BACKFILL_LIMIT` (400), `PSX_BACKFILL_BUDGET` seconds (2400),
`PSX_PORTAL_MONTHS` (24). `DRY_RUN=1` prints the message and sends nothing; the
history cache is still saved so a backfill run is never wasted.

---

## Setup (one time, ~15 minutes)

### Step 1 — Create the Telegram bot
1. In Telegram, open **@BotFather** → send `/newbot` → follow the prompts.
2. Copy the **bot token** it gives you (looks like `123456789:AAH...`).
3. Open a chat with your new bot and send it any message (e.g. "hi").
4. Get your **chat ID**: open this URL in a browser (replace TOKEN):
   `https://api.telegram.org/botTOKEN/getUpdates`
   Find `"chat":{"id": 123456789 ...}` — that number is your chat ID.

(If you already run the sweep-scanner bot you can reuse the same token and
chat ID — both scanners will post into the same chat.)

### Step 2 — Create the GitHub repo
1. Create a new **private** repository on github.com (e.g. `ath-zone-scanner`).
2. Push this folder to it, keeping the structure:
   - `scanner.py` and `psx_scanner.py`
   - `psx_ath_cache.json` (the PSX history cache)
   - `requirements.txt`
   - `.github/workflows/scan.yml` and `.github/workflows/psx_scan.yml`

   ```bash
   git init
   git add .
   git commit -m "ATH zone scanner"
   git branch -M main
   git remote add origin https://github.com/<you>/ath-zone-scanner.git
   git push -u origin main
   ```

### Step 3 — Add secrets
Repo → **Settings → Secrets and variables → Actions → New repository secret**:
- `TELEGRAM_BOT_TOKEN` = your bot token
- `TELEGRAM_CHAT_ID`   = your chat ID
- `COINGECKO_API_KEY`  = *(optional but recommended)* a free CoinGecko **Demo**
  API key. Without it the scanner still works on the public rate limit and backs
  off automatically when CoinGecko says "slow down", but GitHub runners share IP
  addresses so a key makes runs faster and more reliable. Get one free at
  coingecko.com → API → Demo plan.

### Step 4 — (Optional) choose exchanges
Binance is the default. To require a listing on more exchanges on the daily
schedule, add a repository **variable** (Settings → Secrets and variables →
Actions → **Variables** tab): `EXCHANGES` = e.g. `binance,mexc,kucoin`.
Each exchange gets its own section in the message and its own 🆕 tracking.

Supported names: `binance`, `mexc`, `kucoin`, `bybit`, `okx`, `gate`,
`coinbase`, `kraken`, `bitget`, `htx`, `crypto.com`, `upbit`, `bingx`, `lbank`
— or any raw CoinGecko exchange id.

### Step 5 — Test it
Repo → **Actions** tab → **ATH Zone Scan** → **Run workflow**. Leave *dry run*
unticked to get a real Telegram message, or tick it to only see the message in
the job log without sending or committing anything.

For PSX, run **PSX ATH Zone Scan** the same way. It uses the same two Telegram
secrets and needs nothing else.

Done. Crypto runs every day at 00:15 UTC, PSX every weekday at 18:00 PKT, and
each messages you its results.

---

## Tuning knobs

All are environment variables read at the top of `scanner.py`. On GitHub, set
them as repository **variables** and add them to the `env:` block of the
`Run scanner` step in `.github/workflows/scan.yml`.

| Variable | Default | Meaning |
|----------|---------|---------|
| `ATH_DD_MIN` | `0` | Lower bound, % below ATH (0 = coins sitting at their ATH count). |
| `ATH_DD_MAX` | `40` | Upper bound, % below ATH. |
| `MCAP_MIN` | `10000000` | Minimum market cap in USD ($10M). |
| `MCAP_MAX` | `500000000` | Maximum market cap in USD ($500M). |
| `MIN_LOCKED_PCT` | `60` | Minimum % of supply that is **not** circulating. |
| `EXCHANGES` | `binance` | Comma-separated exchanges to require a listing on. |
| `EXCLUDE_CATEGORIES` | `stablecoins,tokenized-products,tokenized-stock,tokenized-private-credit,bittensor-subnets` | CoinGecko category ids whose coins are always skipped. |
| `COINGECKO_API_KEY` | *(empty)* | Free CoinGecko Demo key — higher rate limit. |
| `REQUEST_PAUSE` | `6` (`2` with a key) | Seconds between CoinGecko calls. The default adapts to whether `COINGECKO_API_KEY` is set. |
| `DRY_RUN` | `0` | `1` = print the message, send nothing, write no files. |

## Running locally

```bash
pip install -r requirements.txt

# Dry run: prints the message, touches nothing
DRY_RUN=1 python scanner.py                       # PowerShell: $env:DRY_RUN="1"; python scanner.py
DRY_RUN=1 python psx_scanner.py                   # PSX (the history cache is still saved)

# Real run (sends Telegram, writes state.json / alerts_archive.txt / matches.jsonl)
export TELEGRAM_BOT_TOKEN=...                     # PowerShell: $env:TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID=...
python scanner.py
```

A full run makes roughly 10–15 CoinGecko calls: about 1–2 minutes without a
key, under a minute with one. If CoinGecko answers "too many requests" the
scanner waits and retries by itself, so a slow run is normal, not a failure.

## Notes
- **Data source is CoinGecko** for everything: price, ATH, market cap, supply and
  exchange listings. ATH is CoinGecko's all-time high in USD across all venues,
  not the high since a particular exchange listing.
- "Locked" is derived from supply numbers: anything not yet circulating counts
  as locked, whether it is team vesting, treasury, unminted emissions or
  unreleased tokens. Coins where CoinGecko has neither a max nor a total supply
  cannot be evaluated and are skipped.
- A coin counts as listed if CoinGecko shows at least one live (non-stale) spot
  pair on that exchange. The pair shown prefers USDT, then USDC/FDUSD/BTC/ETH.
- If `state.json` is ever deleted, every coin shows as 🆕 on the next run and
  the day counters restart — nothing else is affected.
- GitHub schedules can drift 5–15 minutes at busy times — normal. If the repo
  has no commits for 60 days GitHub pauses scheduled workflows and emails you;
  one click re-enables it (the daily commit-back normally keeps it alive).
