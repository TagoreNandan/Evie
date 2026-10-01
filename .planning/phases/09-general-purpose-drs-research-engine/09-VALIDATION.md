# Phase 9 Validation: General-Purpose DRS Research Engine

## Success Criteria Checklist

1. [ ] **Source Adapter Infrastructure (`DRS-02`)**: Abstract `SourceAdapter` base class implemented in `drs.py`. New adapters can be added and registered dynamically without modifying core engine logic.
2. [ ] **Multi-Source Retrieval & Deduplication**: Retrieval aggregates evidence across registered web and local journal adapters, de-duplicating URLs and snippets.
3. [ ] **Deep Research Report Generation (`DRS-03`)**: `execute_deep_research()` generates sub-queries, aggregates evidence across adapters, and formats structured reports with Executive Summary, Key Evidence Points, Source URLs, Evidence Limitations, and Mandatory Non-Definitive Disclaimer.
4. [ ] **Endpoint Integration**: `POST /drs/deep_research` endpoint in `main.py` serves deep research queries; `POST /chat` handles deep research intents.
5. [ ] **Non-Definitive Framing Safety**: Output text is strictly passed through `_sanitize_framing()` to eliminate absolute claims.
6. [ ] **100% Test Pass Rate**: `tests/test_drs_deep_research.py` passes cleanly alongside existing test suite.
