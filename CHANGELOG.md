- Early institutional preparation is now a fast path: one high-value precursor starts Institutional Zone Analysis immediately; two or more precursor signals can mark the candidate PREPARED_FOR_ENTRY and enter the execution queue for live confirmation.
- A-GRADE remains preferred but is no longer a mandatory intermediate stop before queue preparation; final trigger/zone/ATOM/risk gates remain mandatory.
# Changelog — RF Liquidity Pro

## 2026-09-05 — Institutional Pipeline Separation Repair

### Pipeline
- Separated `WATCHLIST / ACTIVE CANDIDATES` from `INSTITUTIONAL ZONE ANALYSIS`.
- `MEDIUM` is now an analysis trigger only; it is never an execution-queue admission.
- Added a dynamic institutional-zone registry driven by MEDIUM/STRONG + institutional precursor evidence.
- Institutional-zone membership now rotates automatically when evidence disappears, a setup expires, or the phase becomes overextended/exhausted.
- Added explicit `A-GRADE_READY` qualification before execution-queue admission.
- Execution Queue is now downstream of Institutional Zone Analysis and accepts only explicit A-grade candidates.

### Deep Analysis
- Preserved the zone-first institutional sequence: liquidity/sweep, displacement, MSS/BOS/CHoCH, OB, FVG/imbalance, retest/mitigation, rejection, volume/flow, HTF, session, news, expansion/exhaustion and entry geometry.
- Added dashboard visibility for the dynamic Institutional Zone Analysis population separately from the Execution Queue.

### Reliability
- Fixed the promotion-path type error caused by attempting `float("WAIT_RETEST")`; timing labels are now kept as labels while numeric timing scores use a defensive numeric conversion.
- Removed the automatic `PRE_ENTRY_READY -> queue` shortcut.

### Validation
- Project compile/verify passes.
- Targeted institutional/promotion regression suite: 49 passed, 1 skipped.


## 2026-08-19

### Architecture
- Reduced `main.py` to startup/orchestration only.
- Added explicit `strategy`, `portfolio`, `execution`, `news`, `config`, and `scanner/deep_scanner` boundaries.
- Preserved the supplied 9,885-line source as `source_original_mBOT_1.py`.
- Kept the existing RF/Institutional core as the compatibility kernel instead of performing a risky all-at-once rewrite.

### Portfolio
- Added per-symbol state isolation around the legacy single-position engine.
- Added configurable `MAX_OPEN_POSITIONS` with default 6.
- Added portfolio ranking and multi-position supervision.
- Dashboard now displays all active positions and available capacity.
- Manual dashboard trades use the same portfolio boundary.

### Deep Scanner
- Added venue-wide crypto discovery.
- Added configurable Gold/Oil/Index/Stock symbols with strict venue validation.
- Added institutional scoring, momentum/flow evidence, narrative evidence, and news risk adjustment.
- Added ranked portfolio candidates.

### News
- Added optional RSS event-risk service.
- News is advisory only; unavailable feeds do not block the strategy.

### Reliability
- Fixed the paper-mode close/finalization ordering bug.
- Added a safe fallback for paper-mode mark price during finalization.
- Fixed runtime orchestration so `keep_alive` and `safe_main_loop` are actually provided by `core.runtime`.
- Expanded compile and structural tests.

### Validation
- Full first-party Python compilation passes.
- Portfolio isolation test passes.
- News scoring test passes.

## Known limitation
Live non-crypto execution requires the connected broker/exchange to expose those instruments. The build deliberately does not fake support for assets that are absent from the venue.

## 2026-08-19 — Windows Test Runner Hotfix
- Fixed `tests/test_news_service.py` leaking a fake `requests` module into `sys.modules`.
- The leak caused the dashboard import test to fail inside CCXT with `cannot import name 'Session' from requests`.
- News tests now patch `requests.get` locally without replacing the real Requests package.

## 2026-08-19 — Modular Portfolio + Deep Radar Hardening

- Fixed dashboard `/` crash caused by an unescaped JavaScript object literal inside a Python f-string.
- Added canonical `core.engine.get_smart_zones()` so strategy/deep paths do not depend on scanner import order.
- Upgraded DeepScanner to staged venue-wide radar -> deep institutional analysis -> news risk -> ranked candidates.
- Added asset-class discovery for crypto, gold, oil, indices and venue-exposed stocks.
- Kept non-supported instruments explicit: unavailable venue instruments are skipped, never fabricated.
- Enabled six-position portfolio capacity with portfolio-safe default sizing: 10% margin per position and 60% aggregate cap.
- Added configurable per-asset-class exposure cap.
- Added Deep Institutional Radar panel to the existing dashboard.
- Added regression tests for the dashboard crash, smart-zone provider and six-position sizing policy.

## 2026-08-20 — Institutional Queue Hardening

- Changed the engine's implicit default to PAPER mode when `PAPER_MODE` is absent; LIVE still requires explicit credentials and `PAPER_MODE=False`.
- Reworked Execution Queue order-block scoring to require a causal displacement leg, volume support, freshness/touch count, and broken-zone detection instead of treating the latest candle wick as an order block.
- Added a hard institutional readiness gate: READY now requires persistent confirmation plus minimum order-block, liquidity, institutional-confidence, and structure scores.
- Added regression coverage for fake-vs-causal order blocks, the institutional READY gate, and safe paper defaults.
- Windows launcher now installs dependencies only when imports are missing, avoiding unnecessary network/package operations on every restart.

## 2026-09-03 — External Intelligence + reliability hardening
- Added alert-only Finviz/OpenInsider/SEC EDGAR intelligence providers.
- Added multi-source evidence fusion with conservative BUY-bias gating.
- Added dashboard publication and Telegram alerting for high-confidence external setups.
- Added bounded HTTP timeout/cache/retry layer for external providers.
- Set CCXT exchange timeout from environment (default 10s).
- Removed packaged `.env` credentials and sanitized `.env.example`.
- Updated stale pipeline test expectation to match the existing ATOM hard-reject invariant for over-mitigated zones.

## 2026-09-05 — Research/Production Hardening

- Added a persistent, hash-chained decision journal for structured gate/veto audit events; persistence is best-effort and never a trading dependency.
- Added a side-effect-free OHLCV integrity audit utility covering duplicate/non-monotonic timestamps, invalid OHLC relationships, non-positive values, and stale data.
- Preserved the existing Institutional Precursor → Institutional Zone → A-GRADE → Execution Queue architecture and all production risk/position limits.

- FIX: runtime scanner promotion now admits PREPARED_FOR_ENTRY precursor clusters (>=2) into Execution Queue for live re-evaluation; A-GRADE remains preferred but is no longer mandatory.
- FIX: queue admission labels distinguish PREPARED_ADMITTED from A_GRADE_ADMITTED.
- SAFETY: no direct entry is created by preparation; fresh zone/trigger/confirmation/ATOM/portfolio/risk/execution gates remain authoritative.
