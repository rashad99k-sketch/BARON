# Session-Aware Engineering Notes

## Implemented

- Added `core/market_sessions.py` as a pure/read-only session context engine.
- Preserved the exact session clock windows from the supplied TradingView indicator:
  - Asia 20:00-00:00
  - London 02:00-05:00
  - NY AM 08:30-11:00
  - NY PM 13:30-16:00
  These are interpreted in New York time by default and can be overridden safely with `SESSION_ASIA`, `SESSION_LONDON`, `SESSION_NY_AM`, and `SESSION_NY_PM`.
- Added timezone-aware market-centre context using `zoneinfo`, with Germany display (`Europe/Berlin`) and DST handling.
- Recognizes BingX-style FX wrappers such as `NCFXUSD2CAD/USDT:USDT` as `USD/CAD`.
- USD/CAD is marked `PREFERRED` during its New York liquidity window and carries the exact indicator windows into the entry/management snapshot.
- Session state is advisory by default; it changes timing/ranking and does not confuse underlying cash-market hours with BingX contract availability.
- Optional hard gates: `SESSION_FX_HARD_GATE`, `SESSION_EQUITY_HARD_GATE`, `SESSION_COMMODITY_HARD_GATE`.
- Session context is propagated into Trade Intelligence, entry state, portfolio snapshots, scanner candidates and dashboard data.
- Existing paused-symbol protection for BingX error `109415` remains intact.
- Existing Hedge Mode `LONG/SHORT` positionSide behavior and clientOrderId hardening remain intact.

## Validation

- `verify_project.py`: PASS
- Full-project `compileall`: PASS
- Session regression tests: 3/3 PASS
- Trade Intelligence / Trade Management Board tests: 3/3 PASS
- Static safety check: no `positionSide="BOTH"` and no `set_position_mode(False)` in `core/engine.py`.

The complete pytest suite was not executed in the build sandbox because the sandbox does not contain the project's external `ccxt`/Flask dependencies and has no package-network access. Do not interpret that as a full-suite pass.
