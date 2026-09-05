# EL-BARON BOT PRO — Final Engineering Review — 2026-09-05

## Release scope

This release is based on the latest EL-BARON regression build and incorporates the previously agreed institutional, portfolio, news, session-awareness, exchange-safety, and trade-management work.

## Trading decision architecture

- Venue-wide discovery remains separate from entry authority.
- MEDIUM/watch candidates are monitored and can be promoted by institutional precursor evidence; MEDIUM itself does not place an order.
- Institutional sequence remains liquidity sweep -> displacement -> MSS/BOS -> causal OB/zone/FVG -> retest/mitigation -> rejection/confirmation, with ADX/RF/volume and other indicators as supporting evidence.
- Late/chase entries are rejected by entry geometry/expansion protection.
- Trade style, phase, timing, zone behaviour and the Trade Management Board are captured per position.

## Indicator alignment

The supplied TradingView Institutional Sniper reference is preserved as an evidence/alignment source: session windows, pivot/OB quality, institutional confidence, avoid-late-entry/expansion protection, VWAP, DMI/ADX, ATR, EMA50/EMA200, SMC liquidity/MSS and trend/HTF context.

The exact indicator session clocks remain represented in New York time and are rendered in Germany time for the dashboard. FX wrappers such as `NCFXUSD2CAD/USDT:USDT` are recognized as USD/CAD and mapped to the New York liquidity centre.

## News system

- News remains an independent evidence/slot path, not a blind technical signal.
- Strong/high-impact news candidates can use the independent NEWS slot.
- NEWS positions preserve `trade_type=NEWS` and are managed under `NEWS_DRIVEN` advisory regime.
- Opposed news is recorded as risk evidence and does not blindly force a close by itself.
- The production log now exposes both uppercase machine-readable fields and legacy lowercase fields.

## Portfolio and management

- Six-position portfolio architecture remains isolated per symbol.
- Default class distribution remains 2 CRYPTO + 2 INDEX + 1 GOLD + 1 OIL, with NEWS handled as its independent class/slot while total portfolio capacity remains bounded.
- Position management follows the existing execution authority: synthetic SL, ATR-aware TP1/TP2, breakeven, trailing, runner/profit lock, thesis failure and verified close.
- Profit-management authority is not duplicated by weakening the safety path.

## Exchange/reliability protections

- BingX Hedge Mode uses LONG for BUY and SHORT for SELL.
- No `positionSide=BOTH` and no forced one-way mode.
- Close orders remain `reduceOnly`.
- BingX error 109415 / paused symbols are treated as PAUSED/ERROR rather than false external closure.
- Timeout recovery reconciles account positions before any retry.
- Client order IDs are deterministic-unique under burst generation and remain BingX-safe/under 40 characters.

## Windows regression observations addressed

1. `test_client_order_id.py`: the observed 49/50 burst collision is repaired by monotonic sequence generation.
2. `test_profit_engine_phase3.py`: isolated run shown by the user passed 31/31; production profit rules were not weakened to hide the full-suite capacity symptom.
3. NEWS production logging: `TRADE_TYPE=NEWS` / `REGIME=NEWS_DRIVEN` are now explicit in the dedicated NEWS open log.
4. `ython is not recognized`: identified as a Windows command typo (`python` missing the initial `p`), not a project defect.
5. `main.py`: the release root contains the actual `main.py` entrypoint and `run_windows.bat` starts it from the script directory.

## Validation

- `verify_project.py`: PASS.
- `compileall`/Python compilation: PASS.
- Session + trade-intelligence tests in this environment: 6/6 PASS.
- Client-ID burst validation using a minimal offline import harness: 500/500 unique; max ID length 30.
- Static hedge-mode safety scan: no production `positionSide="BOTH"`; no `set_position_mode(False)`.
- Full Windows pytest remains the authoritative runtime validation because the sandbox does not contain the project's CCXT/Flask runtime dependencies.
