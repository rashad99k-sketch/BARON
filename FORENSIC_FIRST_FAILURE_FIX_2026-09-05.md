# Forensic first-failure fix — 2026-09-05

The first `-x` failure was `tests/test_portfolio_dynamic_6way.py::DynamicSixPositionRealEngineTest::test_six_simultaneous_dynamic_lifecycle`, where `PortfolioManager.open_top(..., slots=6)` returned 0 instead of 6.

Root cause: the upgraded `execute_entry()` required `timestamp` through `is_valid_dataframe()`. The deterministic portfolio simulator intentionally supplies the canonical OHLCV columns without a timestamp. The previous engine accepted that frame. This caused every simulated entry to be rejected before portfolio capacity/lifecycle logic ran.

Repair: keep strict timestamp validation at data-ingestion/exchange boundaries, but make the execution gate require only the five canonical OHLCV columns (`open/high/low/close/volume`) and a usable row count. No institutional gate, risk cap, session logic, or production strategy rule was removed.
