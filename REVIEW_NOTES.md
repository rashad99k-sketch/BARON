# Forensic Integration Review — roro.py + V2 Package

## Source set

- `archive/source_supplied_roro.py`: supplied `roro.py`, preserved unchanged as the reference source.
- `core/engine.py`: production execution/strategy kernel in the package.
- `scanner/`, `news/`, `portfolio/`, `dashboard/`, `core/runtime.py`: modular orchestration layers.

## Reused / connected

### Execution Queue
The supplied bot's institutional queue concepts were verified against the package queue. The package keeps the queue in the core kernel and connects it to the dynamic watchlist and portfolio executor.

### Institutional sequence
The review preserves the intended sequence:

`Liquidity sweep -> displacement -> BOS/CHoCH/MSS -> mitigation/retest -> OB/zone quality -> volume/flow -> institutional participation -> RF -> news/event risk -> queue confirmation -> execution`

### Dynamic watchlist
The package uses venue discovery followed by a rotating deep watchlist. The supplied bot's watchlist/queue concepts were used as compatibility evidence rather than duplicated as a second strategy engine.

### News
The package now has a bounded NewsService with provider fallback, caching, freshness filtering, symbol context and macro context. News cannot open a trade on its own.

### Portfolio
The package isolates position state per symbol and supports up to six contexts. A new portfolio-level risk guard enforces:

- 10% target margin per position
- 60% aggregate margin cap
- 5% daily drawdown stop
- consecutive-loss cooldown
- maximum six positions
- maximum two positions per asset class

### Dashboard controls
Remote manual `/trade` and `/close` require `DASHBOARD_CONTROL_TOKEN`. Localhost remains usable without a token.

## Deliberately not duplicated

- A second RF engine was not added.
- A second execution engine was not added.
- A second trade-management brain was not added.
- The supplied standalone bot was not blindly pasted into the package. That would recreate the monolithic coupling the modular V2 architecture is specifically designed to remove.

## Final engineering decision

The V2 package is the canonical production tree. `roro.py` is retained as an audited source reference. The high-risk execution and trade-management kernel remains the single authority; scanners and intelligence modules provide evidence, while the queue and portfolio layer govern promotion and capacity.
