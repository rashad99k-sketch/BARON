# ATOM-BOOT / SAR_ATOM — Engineering Review & Institutional Upgrade

## Scope

Reviewed the uploaded `SAR_ATOM-main.zip` as the source of truth and upgraded the production tree without creating a second execution engine.

## Architecture retained

`main -> bootstrap -> runtime -> DeepScanner -> watchlist -> InstitutionalRadar -> ExecutionQueue -> PortfolioManager -> core execution kernel -> LiveTradeManager`

The exchange order path remains inside the existing engine. The new intelligence layer is read-only/advisory and therefore cannot place an order by itself.

## Major findings and changes

### 1. BingX 109415 paused-contract failure

Root cause: a paused BingX contract could make symbol-specific position synchronization fail repeatedly. A failed REST query could be confused with a position disappearing.

Fix:
- `SymbolAvailabilityGuard` detects BingX 109415 / `pause currently`.
- Paused symbols receive a cooldown and are suppressed from repeated data calls.
- `fetch_position_status()` distinguishes `OK`, `NOT_FOUND`, `PAUSED`, and `ERROR`.
- `ExchangeSyncService` preserves the last known/local position snapshot for `PAUSED` and `ERROR` instead of marking the position closed.
- Runtime compatibility sync uses the same safe boundary.
- Deep Scanner skips a symbol while it is paused.

### 2. Institutional setup intelligence

Added `core/trade_intelligence.py`.

It evaluates:
- sell-side / buy-side liquidity sweep and reclaim;
- MSS/BOS structure;
- causal order-block quality;
- displacement;
- FVG / imbalance;
- volume expansion;
- ADX/DMI;
- VWAP;
- EMA50/EMA200;
- VWMA 8/13/21/34 stack;
- HTF trend proxy;
- accumulation/distribution risk;
- expansion phase;
- zone proximity and entry geometry;
- pullback, micro-pullback and retest timing;
- SCALP / SWING / TREND style classification.

The uploaded TradingView-derived reference uses VWAP, DMI/ADX, ATR14, volume average, EMA50/EMA200, SMC liquidity/SFP/MSS, VWMA trend structure and HTF trend evidence. The implementation uses these as evidence channels rather than forcing every indicator to agree on every candle.

### 3. MEDIUM -> institutional analysis

The existing watchlist remains the early-warning layer. The new intelligence snapshot is attached to both BUY and SELL hypotheses at deep-watchlist analysis.

MEDIUM remains a promotion/monitoring state, not a direct entry trigger. Mature execution still requires the existing queue and final execution authority.

### 4. Entry timing

The technical entry gate was replaced with a distributed institutional sequence:

`liquidity sweep -> MSS/BOS -> causal zone/FVG -> displacement or rejection -> mitigation/retest/near-zone geometry -> asset-specific ADX -> final execution`

RF, volume and smart-money evidence remain supporting channels rather than universal hard blockers. Late/distribution-prone entries are rejected.

### 5. Trade classification

Every opened position now records a context-aware `trade_style`:

- `SCALP`
- `SWING`
- `TREND`

This is separate from the existing `trade_type` so the legacy execution classification is not destroyed.

### 6. Trade Management Board

Added a stateful advisory board that follows an open trade through:

`ENTRY -> CONFIRMATION -> HEALTH -> PULLBACK -> CONTINUATION -> PROFIT -> DISTRIBUTION -> EXIT`

It records current stage, verdict, market phase, timing, zone behaviour and recent history.

It does **not** submit orders. The existing LiveTradeManager / management brain remains the execution authority for SL, TP, partial close, trailing and final close.

### 7. Dashboard

Portfolio and live-position views now expose:
- trade style;
- entry timing;
- market phase;
- zone behaviour;
- Trade Management Board stage/verdict;
- paused-symbol status where relevant.

## Execution safety preserved

No `positionSide=BOTH` was reintroduced. Hedge mode remains LONG/SHORT based on BUY/SELL. The existing clientOrderId hardening remains intact.

## Validation performed

- `python verify_project.py` — PASS.
- `python -m unittest tests/test_trade_intelligence.py -v` — 3/3 PASS.
- `python -m unittest tests/test_compile.py -v` — PASS.
- All modified Python files compile successfully.
- Source check confirms no production `positionSide=BOTH` or `set_position_mode(False)`.

Full project pytest was attempted. The sandbox does not contain `ccxt`/`Flask`, and outbound package installation is unavailable, so full runtime/import regression certification could not honestly be claimed here. The package keeps the existing Windows dependency installation path and runtime verifier for final machine-level validation.

## Live certification boundary

This package is not declared BingX-live certified from the sandbox. Final live validation must be performed in the user's Windows environment with dependencies installed, first in PAPER mode, then only after observing the real BingX position/order lifecycle.
