# Vibe-Trading integration notes

The upstream HKUDS/Vibe-Trading project was reviewed as an engineering reference. We did **not** replace the RF execution kernel with an LLM agent.

Selected ideas incorporated into RF Liquidity Pro:

- structured market-data/news provider boundaries;
- bounded public-source fallbacks;
- evidence-gated promotion instead of letting a discovery scan execute;
- explicit runtime states and persistent re-evaluation;
- stronger Windows/test discipline;
- defensive failure handling around external data sources.

The RF project keeps its own strategy and execution authority. Vibe-Trading remains a separate upstream project and is not required at runtime.
