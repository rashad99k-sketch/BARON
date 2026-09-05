# RF Liquidity Pro — Modular Windows Build

This build is a **tested structural refactor plus portfolio orchestration layer** around the supplied RF v28 trading brain.

## What changed

- `main.py` is now only startup/orchestration.
- `scanner/` contains RF/Smart/Radar plus a dynamic whole-venue discovery engine that seeds a 50-60 symbol watchlist and continuously deep-analyzes it.
- `strategy/` exposes a stable strategy facade for institutional analysis and entry planning.
- `portfolio/` isolates the legacy single-position state per symbol and supervises up to `MAX_OPEN_POSITIONS` positions. Six is a capacity, not a requirement to force six trades.
- `execution/` is the order-entry boundary.
- `news/` adds cached RSS symbol + global macro intelligence, event classification, recency filtering and risk scoring. High-risk events can block queue execution; news never creates a trade signal by itself.
- `dashboard/` remains the existing dashboard/API and now exposes a portfolio positions panel.
- `config/` centralizes new runtime settings.
- `source_original_mBOT_1.py` remains untouched as the forensic backup of the supplied source.

## Important venue constraint

The connected execution venue in this codebase is BingX/CCXT. The deep scanner can discover crypto perpetuals from the venue. Gold, oil, indices and stocks are **not falsely assumed to be executable**: they must be configured and must actually exist in the connected venue's market list.

If the broker does not expose a requested asset, the scanner logs a clear `instrument not exposed by current venue` warning and skips it. Adding live trading for another asset class requires a broker adapter; it should not be faked through a crypto symbol.

## Multi-position design

The original core has one `STATE`, one `TRADE_STATE`, and one live trade manager. Instead of rewriting thousands of lines of proven trade-management logic in one shot, `portfolio.manager` isolates those objects per symbol and activates one context at a time.

This is a controlled compatibility strategy:
- existing entry/SL/TP/trailing logic remains the authority;
- each position gets its own state and `LiveTradeManager`;
- portfolio capacity is enforced by `MAX_OPEN_POSITIONS`;
- dashboard shows all active positions.

## News layer

`NEWS_ENABLED=True` enables the RSS intelligence layer. It combines:
- symbol-specific headlines,
- global macro headlines,
- event type,
- recency,
- bullish / bearish / neutral bias,
- symbol and macro risk.

It is intentionally **not** an autonomous entry engine. No API key is required
for the default RSS feed; the feed itself is an external data dependency and can
be replaced through `NEWS_RSS_FEEDS`.

## Dynamic opportunity pipeline

Every `GLOBAL_SCAN_INTERVAL_SEC` (default 1200 seconds / 20 minutes), the bot
discovers the connected venue dynamically and builds the strongest 50-60
watchlist candidates. The watchlist is then analyzed continuously in rotating
batches (default 10 symbols every 20 seconds). Only candidates with meaningful
institutional/narrative evidence are promoted to the execution queue.

The scanner does **not** open trades. Queue candidates must survive repeated
order-block, liquidity, structure, timing, trend, institutional and risk
re-evaluation before they become `READY`. Only then does `PortfolioManager`
call the preserved execution brain.

## Windows

Run:

```bat
run_windows.bat
```

The launcher:
1. creates `.venv` if needed,
2. installs `requirements.txt`,
3. creates `.env` from `.env.example` if missing,
4. compiles the project,
5. starts `main.py`.

Never put real API keys into `.env.example`. Put them in the local `.env`.

## Validation

The test suite covers:
- Python compilation,
- import wiring with a mocked exchange dependency,
- portfolio state isolation,
- news parsing,
- dashboard import.

The original source file is preserved for rollback/reference.

The Windows release gate runs each test module in a fresh process via `tools/run_isolated_tests.py`. This is intentional: several legacy tests reload and stub `core.engine`, and a single in-process run can become order-dependent.

## Risk note for six positions

The original single-position SNIPER sizing used 40% margin per trade. That is incompatible with a six-position portfolio. This build defaults to `POSITION_MARGIN_PCT=0.10` with `PORTFOLIO_MARGIN_CAP_PCT=0.60`, so six full slots consume at most roughly 60% of available margin before exchange-specific constraints. The portfolio never forces a trade just to fill a slot.

## Multi-asset truth

The Deep Radar discovers instruments from the connected venue. Gold, oil, indices and stocks are executable only when the connected broker/exchange exposes those instruments and the adapter supports their order type. Configured symbols that do not exist are skipped with an explicit warning; no fake symbols are generated.


## Multi-Market Deep Radar

The Deep Radar discovers and ranks the connected BingX universe across **CRYPTO, STOCK, INDEX, GOLD, and OIL**. BingX currently exposes TradFi perpetual products for stocks, global indices, gold and crude oil in addition to crypto; the scanner therefore uses the live venue market list instead of fabricating symbols.

The allocator is **MAX_OPEN_POSITIONS=6** by default. Six is a hard capacity, not a requirement: the bot opens only the strongest qualified opportunities. When multiple asset classes have valid candidates, selection is diversification-first and then score-ranked.

News is advisory: it contributes directional bias and event risk, but does not independently create a trade. High-risk event conditions can block an otherwise valid candidate.

### Dynamic opportunity pipeline

Every `GLOBAL_SCAN_INTERVAL_SEC` (default 1200 seconds / 20 minutes), the bot
discovers the connected venue dynamically and builds the strongest 50-60
watchlist candidates. The watchlist is then analyzed continuously in rotating
batches (default 10 symbols every 20 seconds). Only candidates with meaningful
institutional/narrative evidence are promoted to the execution queue.

The scanner does **not** open trades. Queue candidates must survive repeated
order-block, liquidity, structure, timing, trend, institutional and risk
re-evaluation before they become `READY`. Only then does `PortfolioManager`
call the preserved execution brain.

## Windows

Run `run_windows.bat`. It creates `.venv`, installs dependencies, compiles every Python module, runs structural tests, and only then starts the dashboard/bot.

### Important live-trading note

Live execution still goes through the existing BingX execution kernel. The Deep Radar does **not** bypass order verification, TP/SL, portfolio sizing, or the existing trade-management brain.

## Institutional V2 hardening

This delivery also fixes the two runtime conditions that caused the previous Deep Radar to display zero:

- a zero radar limit no longer means an empty slice; it means scan all discovered rows;
- the radar now requests enough OHLCV history for the core validator;
- order-block zone key names are consistent between the zone provider and the radar;
- BingX symbols are resolved against the exchange's actual CCXT market keys;
- news uses Yahoo Finance search first with RSS fallback and five-minute caching;
- portfolio capacity is diversified by asset class by default.

The implementation follows the upstream engineering direction observed in Vibe-Trading: bounded data providers, fallback chains, evidence-gated decisions, and regression tests, while preserving RF Liquidity Pro's own execution brain.

## Deep Scanner reliability contract

The Deep Scanner keeps the existing public pipeline and return types, but now has an explicit dependency boundary for market discovery. `RADAR_SYMBOLS` is optional: when it is blank or unset, the live Dynamic Universe remains the source of truth. When configured, it acts only as a filter over instruments actually exposed by the venue.

Transient provider/data failures are not converted into an unexplained zero. Scanner state is published separately as `HEALTHY`, `DEGRADED`, `NO_OPPORTUNITY`, `DATA_UNAVAILABLE`, `PROVIDER_FAILURE`, or `PRESERVED_DEGRADED`. A successful watchlist is preserved across a transient discovery failure instead of being erased.

The test boundary also uses dependency injection/reload isolation so the Deep Scanner tests do not depend on which earlier test imported `core.engine`.

## External Market Intelligence (added 2026-09)

The build now includes an **alert-only external intelligence layer** for U.S. equities. It consumes three sources shown in the supplied reference material:

- **Finviz**: relative volume, price/change and market activity context.
- **OpenInsider**: insider purchase/sale activity.
- **SEC EDGAR**: recent issuer filings and CIK resolution through the public SEC data APIs.

The sources are fused into a bounded 0-100 advisory score. A BUY bias requires multiple independent evidence families; a SEC filing alone can never create a bullish signal. The layer publishes its output to the dashboard and can send a Telegram alert, but **cannot place an order and is not connected to `execute_entry()`**.

SEC automated requests should use a descriptive `SEC_USER_AGENT` containing a real contact email. The SEC states that its submissions and XBRL APIs are public and updated throughout the day; automated access must follow its fair-access policy. Finviz's documented Relative Volume is current volume divided by average volume with intraday adjustment. citeturn0search1turn0search0turn0search2

### External intelligence environment

Set these in local `.env`:

```text
EXTERNAL_INTELLIGENCE_ENABLED=True
EXTERNAL_INTELLIGENCE_INTERVAL_SEC=600
EXTERNAL_INTELLIGENCE_ALERT_SCORE=70
EXTERNAL_INTELLIGENCE_TIMEOUT_SEC=6
EXTERNAL_INTELLIGENCE_CACHE_TTL_SEC=300
SEC_USER_AGENT=ATOM-BOOT/1.0 contact=YOUR_EMAIL@example.com
EXTERNAL_INTELLIGENCE_SYMBOLS=AAPL/USDT:USDT,AMZN/USDT:USDT,GOOGL/USDT:USDT,MSFT/USDT:USDT,NVDA/USDT:USDT,META/USDT:USDT,TSLA/USDT:USDT,JPM/USDT:USDT,MU/USDT:USDT,PLTR/USDT:USDT
```

Keep `.env` local. The distribution contains only `.env.example` with empty credentials.

## Institutional trade intelligence + Trade Management Board

The current build adds a pure advisory intelligence layer in `core/trade_intelligence.py`.
It is deliberately separated from the exchange kernel and classifies each setup using:

- liquidity sweep / reclaim;
- MSS/BOS structure;
- causal order-block/zone quality and displacement;
- FVG / imbalance;
- volume expansion and ADX/DMI;
- VWAP, EMA50/EMA200, VWMA 8/13/21/34 and HTF alignment;
- accumulation/distribution risk;
- early-expansion vs expansion vs compression;
- zone mitigation, pullback, micro-pullback and retest timing;
- SCALP / SWING / TREND trade style.

`TradeManagementBoard` follows an open position through ENTRY → CONFIRMATION → HEALTH →
PULLBACK → CONTINUATION → PROFIT → DISTRIBUTION → EXIT. It is observational and does not
place orders; the existing execution/management authority remains responsible for actual
SL/TP, partial close, trailing and final close decisions.

### BingX paused-symbol hardening

BingX error `109415` (contract paused) is handled as **PAUSED**, not as an externally closed
position. The guard suppresses repeated requests during a cooldown and preserves the last
known position state so Portfolio Management cannot accidentally erase a real position just
because a symbol-specific REST query failed.

## Session-aware execution

The institutional pipeline is session-aware for multi-market instruments. The supplied TradingView reference windows are preserved as an indicator-alignment layer: **Asia 20:00-00:00, London 02:00-05:00, NY AM 08:30-11:00, NY PM 13:30-16:00 (New York time)**. The runtime also uses timezone-aware market-centre windows so daylight-saving changes are handled correctly.

For FX-style BingX symbols such as `NCFXUSD2CAD/USDT:USDT`, the engine identifies the underlying **USD/CAD** pair and marks the New York liquidity window as preferred. Session state is advisory by default; hard gates are opt-in through `SESSION_FX_HARD_GATE`, `SESSION_EQUITY_HARD_GATE`, and `SESSION_COMMODITY_HARD_GATE`. This avoids confusing an underlying cash-market session with BingX contract availability.

## Research & Audit Hardening (2026-09-05)

This release includes `GITHUB_RESEARCH_INTEGRATION.md`, a persistent hash-chained decision journal (`core/decision_journal.py`), and a side-effect-free OHLCV integrity audit (`tools/market_data_audit.py`). These additions improve auditability and research correctness without changing the live execution authority.
