# RF Liquidity Pro — Production Architecture

## Runtime flow

`main.py` -> `app/bootstrap.py` -> `core/runtime.py`

The canonical pipeline is:

`WHOLE VENUE DISCOVERY -> TOP 50-60 WATCHLIST -> CONTINUOUS DEEP WATCHLIST ANALYSIS -> INSTITUTIONAL EXECUTION QUEUE -> READY -> PORTFOLIO EXECUTION`

The dashboard is isolated in `dashboard/`, discovery/deep-watchlist logic is
isolated in `scanner/deep_scanner.py` + `scanner/universe.py`, news is isolated
in `news/service.py`, and order execution remains in the preserved BingX kernel.

## Decision sequence

1. Dynamic venue discovery every 20 minutes.
2. Lightweight radar over the connected multi-market universe.
3. Select the strongest 50-60 candidates, prioritizing proximity to smart zones,
   liquidity context, momentum/flow and RF evidence.
4. Rotate through the watchlist continuously; the watchlist is an analysis
   engine, not a static list.
5. For each watched symbol, evaluate both BUY and SELL and refresh:
   liquidity sweep, BOS/CHoCH, order-block/zone retest, rejection,
   displacement, volume, RF, smart money, momentum and news risk.
6. Promote only mature watchlist setups into the execution queue.
7. Re-evaluate queue candidates frequently; require persistent institutional
   confirmation before READY.
8. PortfolioManager executes only READY queue candidates.
9. Existing strict execution / TP / SL / live management remains the authority.

## Six-position policy

`MAX_OPEN_POSITIONS=6` is a capacity limit. The allocator does not force six
trades. It first attempts to take the best candidate from each asset class,
then fills remaining slots by score, subject to the per-class cap and the
existing margin cap.

Per-class caps come from a single live rule shared by the open authority
(`PortfolioManager._class_cap`) and the allocator report
(`GlobalAssetAllocator.allocate`), in this precedence:

1. `MAX_<CLASS>_POSITIONS` (e.g. `MAX_CRYPTO_POSITIONS`) — per-class override;
2. `MAX_POSITIONS_PER_ASSET_CLASS` — the EVERY-class master override;
3. the built-in 6-market default profile: CRYPTO 2 / INDEX 2 / GOLD 1 / OIL 1 /
   NEWS 1 (unknown classes cap at 0, so e.g. STOCK never opens by default).

Example: running with `MAX_CRYPTO_POSITIONS=6` and a crypto-heavy live board
expands the CRYPTO seat count to 6 while INDEX/GOLD/OIL keep their defaults.
The default profile explains why a crypto-heavy board historically opened only
2 of 6 slots — that was the CRYPTO cap being enforced correctly, not a broken
open path.

## Market support

The project is designed around BingX's current USDT-margined derivatives
universe. BingX exposes crypto plus TradFi products including stocks, global
indices, gold and crude oil. The scanner discovers the live market list and
only trades instruments actually exposed by the connected venue.

## News

News is advisory, cached, bounded and fail-open. It supports both symbol-specific
and global macro context, classifies events, deduplicates headlines and applies
recency limits. A feed outage never fabricates a signal. High event-risk can
block queue execution; news alone cannot open a trade.

## Preservation rule

The large `core/engine.py` remains intentionally preserved as the execution
kernel. The refactor adds stable module boundaries around it rather than
rewriting its high-risk TP/SL/exchange synchronization logic blindly.

## Engineering hardening added in this review

- **Execution boundary:** scanner/queue code never calls exchange orders directly; READY candidates flow into `PortfolioManager`, which is the portfolio allocation boundary and delegates entry to the preserved execution kernel.
- **Portfolio risk guard:** aggregate margin cap, daily drawdown limit, and consecutive-loss cooldown are enforced before a new slot can be opened. Risk state is published to the dashboard.
- **Manual control security:** `/trade` and `/close` are local-only when no `DASHBOARD_CONTROL_TOKEN` is configured. Remote/public control requires `X-Dashboard-Token` (or the request `control_token`).
- **Paper runtime smoke:** `tools/paper_runtime_smoke.py` validates deterministic venue discovery -> watchlist -> queue promotion -> queue re-evaluation without exchange credentials.

These boundaries follow the modular executor/controller approach used by Hummingbot and the explicit protection model used by Freqtrade, while preserving the project's existing RF/institutional decision logic. The exchange boundary remains CCXT/BingX-specific.

## Session-aware market context

Session context is an evidence/timing layer between market discovery and entry. It detects FX pairs embedded in venue symbols (including BingX forms such as `NCFXUSD2CAD/USDT:USDT`), maps the relevant liquidity centre, preserves the exact supplied TradingView session windows, and exposes the current session in UTC/New York/Germany time.

Session state is `PREFERRED`, `ACTIVE`, or `QUIET`. It affects timing/ranking but does not universally block BingX derivatives. Optional hard gates are controlled by `SESSION_FX_HARD_GATE`, `SESSION_EQUITY_HARD_GATE`, and `SESSION_COMMODITY_HARD_GATE`.
