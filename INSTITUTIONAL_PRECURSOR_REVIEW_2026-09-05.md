# EL-BARON-BOT-PRO — Institutional Precursor Review

## Scope
Surgical change only: connect the live DeepScanner/Watchlist analysis to the canonical Dynamic Institutional Zone Analysis layer so an active MEDIUM/STRONG candidate with sufficient early institutional precursor evidence is promoted immediately for deep institutional analysis, without becoming an entry.

## Pipeline contract
`UNIVERSE -> WATCHLIST -> MEDIUM/STRONG + >=2 precursor evidence -> INSTITUTIONAL ZONE ANALYSIS -> A-GRADE -> EXECUTION QUEUE -> READY -> EXECUTION`

MEDIUM alone does not promote. Precursor evidence alone does not execute. The execution queue remains downstream of A-GRADE qualification and live entry/risk gates.

## Precursor evidence wired from the live Watchlist
- Liquidity sweep
- Displacement
- BOS / CHoCH / MSB/MSS
- OB / Zone
- OB/Zone retest / mitigation
- FVG / imbalance
- Rejection
- Orderbook imbalance
- Smart-money alignment
- Momentum/flow acceleration
- Expansion/boost context

The fast bridge consumes the same fresh closed-candle Watchlist analysis that renders the candidate, avoiding the previous dependency on the legacy one-by-one radar refresh before the candidate appears in the Institutional Zone panel.

## Dynamic behavior
The Institutional Zone registry remains dynamic and bounded by valid opportunities rather than a fixed number. Existing expiry, invalidation, exhaustion, stale/late handling and ranking remain intact.

## Execution safety
The bridge never calls `execute_entry`, never bypasses the queue, and never converts MEDIUM directly into READY. A candidate can appear in Institutional Zone Analysis while `a_grade_ready=false` and `queue ready=0`.

## Validation
- 47 test files
- 489 test methods executed in isolated dependency-boundary runs
- All isolated suites passed; one pre-existing environment-dependent skip remains in the suite
- `verify_project.py`: PASS
- Paper runtime smoke: PASS
- Six-position portfolio lifecycle: PASS
- Explicit 5 technical + 1 NEWS = 6 concurrent positions: PASS
- 7th-position refusal at total capacity: PASS
- NEWS slot production suite: PASS
- Profit Engine Phase 3: PASS
- Runtime Repairs: PASS
- No live BingX order was sent during validation

## Environment boundary
The validation container does not provide the production `ccxt`/Flask packages. Dependency-boundary stubs were used only for offline automated validation; production `requirements.txt` remains unchanged and is the runtime source of truth.
