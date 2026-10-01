# Phase 9 Context: General-Purpose DRS Research Engine

## Goals
Evolve Evie's DRS (Decision Research Support) foundation into a general-purpose research system capable of conversational multi-adapter web research and longer-running deep research with structured, attributed reports.

## Requirements
- **`DRS-02`**: Extensible source adapter infrastructure (`SourceAdapter`) supporting multiple web/search providers and local memory data sources without redesigning the core engine.
- **`DRS-03`**: Deep research engine (`execute_deep_research`) performing multi-query planning, iterative evidence retrieval, source deduplication, and structured report synthesis with cited sources and explicit evidence limitations.

## Design Decisions & Invariants
1. **Extensible Source Adapter Architecture (`drs.py`):**
   - Abstract `SourceAdapter` base class with `name` attribute and `search(query, max_results)` method.
   - Registered source adapters:
     - `WebSearchAdapter` (external web search evidence)
     - `LocalMemorySourceAdapter` (local journal entries & RAG memory via `memory.py`)
     - `GitHubGmailIntelligenceAdapter` (cached GitHub repos/activity & Gmail email metadata via `services/github_intelligence.py` and `services/gmail_intelligence.py`)
   - Adapters registered dynamically in global engine registry `AdapterRegistry`.
2. **Unified Retrieval & Deep Research Depth (`drs.py`):**
   - Deep Research operates with balanced depth: **3 sub-queries** per research topic and **5 evidence snippets** per query.
   - Both quick conversational research (`ask_drs_question()`) and deep research (`execute_deep_research()`) draw from the same underlying retrieval, extraction, deduplication, and evidence synthesis logic.
   - Evidence snippets strictly sanitized using `_sanitize_framing()` to eliminate absolute claims ("definitely", "100% proven").
3. **Deep Research Report Generation (`POST /drs/deep_research`):**
   - Generates sub-queries for complex research topics.
   - Iterates over sub-queries, aggregates results across registered source adapters, deduplicates URLs and snippets.
   - Produces structured research report containing: Executive Summary, Key Evidence Points with Source URLs, In-Depth Analysis, Evidence Limitations, and Mandatory Non-Definitive Disclaimer.
4. **API & Conversational Router Integration (`main.py`):**
   - New endpoint `POST /drs/deep_research`.
   - `/chat` conversational router handles "deep research" intent or flags.

## Scope Fences
- No hardware, Taz, or Raspberry Pi GPIO/display work in Phase 9 (reserved for Phase 10).
- No paid research API requirements (all adapters work with free/keyless tools or local DB).
- No alteration of existing Phase 1–8 security, briefing, or calendar safety invariants.
