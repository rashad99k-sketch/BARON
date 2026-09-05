# EL-BARON BOT PRO — Release Blocker Repairs — 2026-09-05

This build addresses the concrete failures observed in the Windows regression screenshots while preserving the production trading authorities.

## 1. Client Order ID collision

Observed failure: `49 != 50` in `test_consecutive_generated_ids_are_unique`.

Repair: `OrderManager` now uses a per-manager monotonic sequence combined with the millisecond timestamp and symbol digest. IDs remain ASCII-safe and below BingX's 40-character limit. Randomness is no longer the uniqueness mechanism.

## 2. NEWS management log contract

Observed failure: the runtime executed a NEWS position, but the production test could not find `TRADE_TYPE=NEWS` in the captured management log.

Repair: the dedicated NEWS-open log now emits both machine-readable fields (`TRADE_TYPE=NEWS`, `REGIME=NEWS_DRIVEN`, `SLOT=NEWS`) and the legacy lowercase NEWS fields. The actual NEWS trade classification remains pinned to `NEWS` for the full lifecycle.

## 3. Profit-engine / full-sweep regression

The isolated `test_profit_engine_phase3.py` and `test_client_order_id.py` runs shown in the Windows screenshots pass individually (31/31 and 4/4). The remaining full-suite symptom was the final capacity assertion after the portfolio sweep, not a failure of the isolated profit-engine scenarios.

Production risk protection is **not** weakened to make that assertion pass. Daily-loss and consecutive-loss cooldowns remain real safety controls. The package therefore treats the full-sweep capacity check as a regression signal to be validated under the user's exact Windows `.env`/test environment rather than masking it by disabling risk protection.

## 4. Session-aware execution

The supplied TradingView institutional session configuration remains the reference alignment layer. The engine keeps exact New York-clock windows and converts them to Germany time for operator visibility, while FX pairs such as BingX `NCFXUSD2CAD/USDT:USDT` are recognized as USD/CAD and mapped to the New York liquidity centre.

Hard session blocking remains opt-in so venue availability is not confused with underlying market hours.

## 5. Exchange safety preserved

- No `positionSide=BOTH`.
- No forced one-way `set_position_mode(False)`.
- Hedge mode remains LONG for BUY and SHORT for SELL.
- Close orders remain `reduceOnly`.
- BingX paused-symbol `109415` is treated as PAUSED/ERROR, never as an external close.
- Timeout recovery reconciles the real account position before retrying.

## Release validation performed in this environment

- Python AST/bytecode compilation: PASS.
- Static hedge-mode safety scan: PASS.
- ZIP integrity: checked after packaging.
- Full pytest cannot be honestly claimed here because this build environment does not contain the project's CCXT/Flask runtime dependencies. Windows `.venv` remains the final runtime authority.
