# Early Entry Queue Fix — 2026-09-05

## Observed runtime behavior
The dashboard showed a healthy Institutional Zone Analysis population but `Promoted = 0` and `In Queue = 0` even when candidates had multiple precursor signals. The runtime calls `scanner.scanner.promote_to_queue()`, whose previous gate still required `a_grade_ready=True`. This contradicted the intended early-entry design where a genuine precursor cluster should prepare the candidate for live queue re-evaluation before full A-GRADE qualification.

## Surgical correction
`scanner/scanner.py::promote_to_queue()` now admits either:
- `A_GRADE_READY`, or
- `PREPARED_FOR_ENTRY` with at least two canonical precursor signals.

A-GRADE candidates retain the stricter structural-evidence gate. PREPARED candidates use their precursor cluster as the early qualification and are then subjected to fresh data, causal-zone, extension/stale-zone, ZoneMetrics, queue re-evaluation, trigger, confirmation, ATOM, portfolio, risk and execution controls.

No production risk guard, order execution, TP/SL, or position cap was weakened.

## Runtime meaning
`PREPARED_ADMITTED` means **prepare/re-evaluate**, not **open trade**.
`A_GRADE_ADMITTED` means the higher-confidence institutional path was admitted.
Only the downstream READY/ENTRY gates can authorize an order.
