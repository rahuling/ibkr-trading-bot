# IBKR Options Trading Bot

An automated options trading bot that connects to Interactive Brokers via IB Gateway, runs three distinct premium-selling / momentum strategies, and is controlled entirely through Telegram. Designed for a single account running on a Linux VPS (or locally).

---

## What this bot does

The bot manages three separate capital buckets and runs a different options strategy in each one. All trade proposals flow through Telegram — you review and approve/reject before any order is placed (automation level 2, the default).

### Strategy 1 — The Wheel (Core bucket, 55% of portfolio)

Sells cash-secured puts on blue-chip equities and ETFs (SPY, QQQ, IWM, GLD, AAPL, MSFT, NVDA, AMZN, GOOGL). If assigned, switches to covered calls on the shares until they are called away, then cycles back to CSPs. The full CSP → assignment → CC → call-away sequence is tracked as a single "wheel cycle" in the database.

- **CSP parameters**: 30–45 DTE, target delta -0.27 (±0.03), close at 50% of max credit
- **Covered call parameters**: 7–21 DTE, target delta 0.28, close at 75% of max credit

### Strategy 2 — Bull Put Spreads (Tactical bucket, 20% of portfolio)

Sells bull put spreads on higher-IV, higher-beta names (AMD, META, TSLA, NFLX, CRM, COIN, MSTR). The spread structure caps max loss to $600 per position (configurable), unlike naked CSPs.

- **Spread parameters**: 7–21 DTE, target delta -0.30 (±0.02), $5 spread width, minimum credit-to-width ratio 25%, close at 75% of max credit

### Strategy 3 — EOD LEAP Calls (Momentum bucket, 10% of portfolio)

At 3:30pm ET, scans for stocks near their intraday high with elevated volume. Buys a deep-in-the-money LEAP call (delta 0.73–0.83, DTE 300–420 days) as a leveraged stock substitute. Monitored by **underlying price**, not option mark — LEAP bid-ask spreads are too wide for price-based stops to work on the option itself.

- **Signal conditions** (all must be true): price within 1% of day high, price above 20-day SMA, session volume ≥ 1.2× 20-day average, no earnings within 5 days
- **Exit rules**: stop at −2% on underlying, take profit at +8% on underlying, max extrinsic value 25% of option cost
- **Entry window**: 3:30–3:55pm ET only

### Reserve (15% of portfolio)

Never traded. Absorbs assignment capital from CSPs and acts as a buffer against drawdowns.

---

## Architecture

```
bot/
├── main.py              Entry point. Startup sequence, scheduler, wiring.
├── config.py            Pydantic config models. Loads config.yaml + .env.
├── database.py          SQLite schema and init. WAL mode.
├── ibkr.py              IB Gateway connection wrapper (ib_async).
│
├── scanner/
│   ├── base.py          IV history update (runs 4:15pm daily).
│   ├── premium.py       M1: Premium scanner — 9:45am + 3:00pm ET.
│   └── momentum.py      M1: EOD momentum scanner — 3:30pm ET.
│
├── builder/
│   ├── csp.py           M2: CSP proposal builder + Telegram trade card formatter.
│   ├── spread.py        M2: Bull put spread proposal builder.
│   └── leap.py          M2: LEAP call proposal builder + trade card formatter.
│
├── execution/
│   └── engine.py        M4: Order submission, repricing, orphan recovery on restart.
│
├── positions/
│   └── manager.py       M5: Position state, Greeks polling, assignment detection.
│
├── risk/
│   └── engine.py        M6: Circuit breakers, loss limits, PDT monitoring.
│
├── journal/
│   └── journal.py       M7: Trade logging, P&L reports, rule-tag analytics.
│
└── telegram/
    ├── bot.py           Telegram Application setup, auth filter.
    ├── commands.py      M3: All /command handlers.
    └── notifications.py Outbound alerts (scan results, fills, circuit breakers).
```

### Scheduled jobs (all times US Eastern)

| Time | Job |
|------|-----|
| 9:30am | Morning summary pushed to Telegram |
| 9:45am | Premium-selling scan (Core + Tactical) |
| 3:00pm | Afternoon premium-selling scan |
| 3:30pm | EOD momentum scan |
| 4:15pm | IV history update (after market close) |
| Every 5 min | Position monitor — updates P&L/Greeks in DB, fires profit-target alerts |
| Every 5 min | Heartbeat — alerts on 2 consecutive misses |
| 1st of month, 8am | Monthly P&L report |

---

## Risk rules

All limits are computed from **live portfolio value** fetched from IBKR on every check — never hardcoded dollar amounts.

| Rule | Value | Action |
|------|-------|--------|
| Daily loss limit | 1.5% of portfolio | Pause new trades |
| Weekly loss limit | 3.0% of portfolio | Pause new trades |
| Monthly loss limit | 8.0% of portfolio | Full stop — requires `/resume` |
| Max single underlying | 40% of portfolio | Block new positions |
| Max position size | 33% of bucket | Block new positions |
| Max spread loss | $600 per spread | Hard limit |
| PDT warning | Portfolio < $30k | Telegram alert |
| PDT stop | Portfolio < $25k | Block day trades |
| IVR minimum (Core) | 30 | Scanner filter |
| IVR minimum (Tactical) | 35 | Scanner filter |
| Earnings blackout | 7 days pre / 2 days post | Scanner filter |
| Order blackout (open) | 9:30–9:45am ET | Block order submission |
| Order blackout (close) | 3:55–4:00pm ET | Block order submission |

---

## Execution rules

- **Always limit orders.** Market orders only for emergency closes — a Telegram warning is sent before any market order is submitted.
- **Spreads are submitted as BAG/combo contracts** (never two individual legs).
- **Idempotent crash recovery**: `client_order_id` (UUID) is written to the `orders` table *before* the order is sent to IBKR. On startup, the bot checks for any orders in `pending_submit` state and reconciles against live IBKR positions.
- **Repricing**: if an order is unfilled after 5 minutes, reprice 1 tick toward the ask and resubmit once. If still unfilled after 3 more minutes, cancel and notify.
- **Tick size**: $0.05 for options < $3.00, $0.10 for options ≥ $3.00.
- **Proposal TTL**: 2 hours from generation, hard capped at 4:00pm ET.

---

## Telegram commands

### Scanning & proposals
| Command | Description |
|---------|-------------|
| `/scan` | Trigger a manual scan |
| `/approve [id]` | Approve a trade proposal — submits the order |
| `/reject [id] [reason]` | Reject a trade proposal |

### Positions
| Command | Description |
|---------|-------------|
| `/positions` | All open positions with live Greeks |
| `/close [id]` | Manually close a position |
| `/roll [id]` | Initiate roll logic |
| `/wheel [id]` | Full Wheel cycle P&L for an underlying |

### Risk & P&L
| Command | Description |
|---------|-------------|
| `/risk` | Capital allocation, bucket usage, loss limit status |
| `/journal [days]` | Trade journal (default 30 days) |
| `/analyze [tag]` | Win rate and P&L breakdown by rule tag |

### Configuration
| Command | Description |
|---------|-------------|
| `/config` | Show current configuration |
| `/setconfig [param] [value]` | Adjust a parameter at runtime (all changes are audited in DB) |
| `/watchlist` | Show all three watchlists |
| `/addticker [ticker]` | Add a ticker to a watchlist |
| `/removeticker [ticker]` | Remove a ticker from a watchlist |

### System
| Command | Description |
|---------|-------------|
| `/status` | IB Gateway connection, account, balance, automation level |
| `/pause` | Halt all scanning and order submission |
| `/resume` | Resume after a pause |
| `/reconcile` | Force a DB ↔ IBKR position reconciliation |

---

## Automation levels

Controlled by `automation.level` in `config.yaml`:

| Level | Behaviour |
|-------|-----------|
| L1 | Alerts only — bot scans and notifies, no order submission |
| L2 | Assisted (default) — bot proposes, human approves via `/approve` |
| L3 | Autonomous — bot executes without approval, except positions > 20% of portfolio |

---

## Database schema

SQLite, WAL mode. File: `data/trading.db`. Schema is in `bot/database.py`.

| Table | Purpose |
|-------|---------|
| `wheel_cycles` | Full Wheel cycle from CSP entry to CC exit |
| `trades` | Every trade: entry, exit, outcome, rule tags, Greeks snapshot |
| `positions` | Live position state (updated on each poll) |
| `proposals` | Pending trade proposals awaiting `/approve` or `/reject` |
| `orders` | Full order lifecycle — pending → submitted → filled |
| `risk_events` | Circuit breaker events (daily limit hit, PDT warning, etc.) |
| `iv_history` | Daily IV per ticker for IVR calculation (252-day bootstrap required) |
| `config_changes` | Audit log of every `/setconfig` change |

**Schema rule**: all changes must be additive. Never drop or rename columns once deployed to a live account.

---

## Setup

### Prerequisites

- Python 3.11+
- Docker + Docker Compose (for IB Gateway)
- An Interactive Brokers account (paper or live)
- A Telegram bot token from [@BotFather](https://t.me/botfather)
- Your Telegram user ID (get it from [@userinfobot](https://t.me/userinfobot))

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env and fill in:
#   IBKR_USERNAME, IBKR_PASSWORD
#   TRADING_MODE=paper   (keep as paper until fully validated)
#   TELEGRAM_BOT_TOKEN
#   TELEGRAM_ALLOWED_USER_IDS
```

### 2. Start IB Gateway

```bash
docker compose up -d
```

IB Gateway runs on `127.0.0.1:4002`. It is **never exposed publicly** — the Docker port binding explicitly binds to loopback only. IB Gateway has no authentication, so this is critical.

The gateway restarts automatically at 11:59pm daily (required by IBKR) and on failure.

### 3. Install Python dependencies

```bash
python -m venv venv
source venv/bin/activate   # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 4. Bootstrap IV history

The IVR calculation requires 252 days of historical IV per ticker. Run this once before the first scan:

```bash
python scripts/bootstrap_iv_history.py
```

### 5. Start the bot

```bash
python bot/main.py
```

The bot will:
1. Validate `config.yaml`
2. Initialise the SQLite database (safe to run repeatedly)
3. Connect to IB Gateway
4. Reconcile DB positions against live IBKR positions
5. Start the scheduler
6. Start the Telegram bot

Send `/status` in Telegram to confirm everything is connected.

### Running as a systemd service (VPS)

```bash
sudo cp systemd/trading-bot.service /etc/systemd/system/
# Edit the service file — update WorkingDirectory and User if needed
sudo systemctl daemon-reload
sudo systemctl enable trading-bot
sudo systemctl start trading-bot
sudo journalctl -u trading-bot -f   # tail logs
```

### Backup

```bash
bash scripts/backup.sh
```

Backs up `data/trading.db` to `backups/` with a timestamp. Set up a cron job or run manually.

---

## Configuration reference (`config.yaml`)

All parameters can be viewed at runtime with `/config` and changed with `/setconfig`. Changes made via `/setconfig` are written to the `config_changes` audit table.

| Section | Key | Default | Description |
|---------|-----|---------|-------------|
| `risk` | `core_bucket_pct` | 0.55 | Fraction of portfolio for Wheel strategy |
| `risk` | `tactical_bucket_pct` | 0.20 | Fraction for Bull Put Spreads |
| `risk` | `momentum_bucket_pct` | 0.10 | Fraction for LEAP calls |
| `risk` | `reserve_pct` | 0.15 | Never traded |
| `risk` | `daily_loss_limit_pct` | 0.015 | 1.5% daily loss → pause |
| `risk` | `weekly_loss_limit_pct` | 0.03 | 3% weekly loss → pause |
| `risk` | `monthly_loss_limit_pct` | 0.08 | 8% monthly loss → full stop |
| `risk` | `min_ivr_core` | 30 | Min IVR for Core (Wheel) scanner |
| `risk` | `min_ivr_tactical` | 35 | Min IVR for Tactical (Spread) scanner |
| `trading.csp` | `target_delta` | -0.27 | Target put delta for CSPs |
| `trading.csp` | `dte_min/max` | 30–45 | DTE range for CSP expiry selection |
| `trading.csp` | `profit_close_pct` | 0.50 | Close CSP when credit decays 50% |
| `trading.spread` | `spread_width` | 5 | Bull put spread width in dollars |
| `trading.spread` | `profit_close_pct` | 0.75 | Close spread at 75% of max credit |
| `leap` | `target_delta` | 0.78 | Target LEAP call delta |
| `leap` | `stop_loss_pct` | 0.02 | 2% adverse move on underlying → close |
| `leap` | `profit_target_pct` | 0.08 | 8% favourable move → close |
| `automation` | `level` | 2 | 1=alerts, 2=assisted, 3=autonomous |

---

## Development status

The codebase is structured in build phases. Each module has stubs and `TODO (Phase N)` markers showing what needs to be implemented next.

| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Infrastructure: config, DB schema, IB Gateway connection, Telegram `/status` | Done |
| 2 | Premium scanner (M1) + CSP/Spread TradeBuilder (M2) | Done |
| 3 | Execution Engine (M4): order submission, repricing, crash recovery | Done |
| 4 | Risk Engine (M6) + Position Manager (M5): live checks, PDT, reconciliation | Done |
| 5a | Journal (M7): trade logging, monthly reports, `/analyze` | Not started |
| 5b | EOD Momentum scanner + LEAP builder | Not started |

---

## Dependencies

| Package | Purpose |
|---------|---------|
| `ib-async` | IBKR API client (maintained fork of ib_insync) |
| `python-telegram-bot` | Telegram bot framework (async, v20+) |
| `APScheduler` | Cron-style job scheduler (configured for `America/New_York` timezone) |
| `pydantic` | Config validation |
| `aiosqlite` | Async SQLite |
| `python-dotenv` | `.env` file loading |
| `pytz` | Timezone support for APScheduler |
| `yfinance` | Earnings calendar lookup for blackout checks |

---

## Security notes

- `.env` is in `.gitignore` and must never be committed.
- IB Gateway port 4002 is bound to `127.0.0.1` only in `docker-compose.yml` — do not change this.
- The Telegram bot validates `TELEGRAM_ALLOWED_USER_IDS` on every command — only whitelisted user IDs can interact with the bot.
- On a VPS, run behind a firewall with no inbound access to port 4002.
