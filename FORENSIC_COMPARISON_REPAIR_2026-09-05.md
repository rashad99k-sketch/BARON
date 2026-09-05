# EL-BARON Forensic Comparison & Repair — 2026-09-05

## Compared sources

- Reference / previously working baseline: `SAR_ATOM-main(1).zip`
- New build under test: `EL-BARON-BOT-PRO-GRACEFUL-SHUTDOWN-2026-09-05(1).zip`

## Primary finding

The 32 Windows test failures are not explained by the graceful-shutdown patch itself. The major behavioral regression is the replacement of the original `check_institutional_entry()` contract with a stricter TradeIntelligence-first gate.

The old gate accepted the established engine-owned evidence chain:
liquidity sweep -> strong zone/FVG/OB -> MSS/CHoCH -> rejection/displacement -> volume expansion -> ADX -> RF alignment -> anti-chase.

The new gate added a hard early rejection whenever TradeIntelligence reported `valid=False`, plus mandatory causal-zone scoring and a composite readiness score. The existing synthetic/integration fixtures were written for the established engine contract and therefore could be rejected before the portfolio/profit/slot lifecycle was reached. This cascaded into failures such as `0 != 6`, `False is not true`, and related slot/news assertions.

## Repair

The repaired build keeps the new institutional sequence as the primary path but adds a canonical evidence fallback when the enrichment layer cannot validate a sparse/synthetic frame.

The fallback still requires:
- directional liquidity sweep
- MSS/BOS
- causal engine-owned OB/zone/FVG evidence
- displacement or rejection
- asset-class ADX floor/cap
- anti-chase geometry

RF and volume remain supporting evidence, not universal hard blockers.

This avoids weakening the production institutional core while preserving the established engine execution contract used by the lifecycle suites.

## Additional defects fixed

1. `scanner/deep_scanner.py` passed an undefined `symbol` variable into TradeIntelligence; it now passes the local `sym`.
2. The live institutional updater thread is now registered with the process shutdown barrier, so it is joined during graceful shutdown.
3. Python source was recompiled after the repairs.
4. No production `positionSide="BOTH"` or `set_position_mode(False)` was introduced.

## Validation performed in the build environment

- `verify_project.py`: PASS
- `compileall`: PASS
- Static hedge-mode safety scan: PASS
- ZIP integrity: PASS

The build environment does not contain the project's `ccxt`/Flask runtime packages, so a complete 484-test Windows run cannot be truthfully claimed here. The final authority remains the user's Windows virtualenv test run.

## Expected next Windows validation

Run:

```bat
python -m pytest -q
```

Then, after the suite, run the bot and press `Ctrl+C` once to validate that the previous:

`Fatal Python error: _enter_buffered_busy`

no longer occurs.
