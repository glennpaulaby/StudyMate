# My contributions:
- implemented context-window compaction on feat/memory-episodic-procedural: CompactionConfig with dynamic per-model windows (explicit table, gateway discovery, suffix hint, conservative default), chars/4 estimator, tiered micro-prune plus single tool-less LLM summary in the same thread
- wired auto-compact at the top of the ReAct loop plus overflow compact-and-retry once, manual /compact command with thread persistence, and a sticky bottom toolbar showing model, thread, and live context footprint (Claude Code/OpenCode-inspired, prompt_toolkit bottom_toolbar with the default reverse style killed)
- fixed a live 400 from the model gateway by merging fact injection into messages[0] (strict templates reject any SYSTEM past index zero)
- implemented procedural memory as versioned markdown bundles (study/procedures.py plus seed ingest-lecture routine, draft/approved/archived lifecycle with human approval, per-turn injection, draft surfacing never silently followed)
- resolved the merge of feat/memory-episodic-procedural into main: requirements/pyproject unions (kept colleagues' mcp pin and stricter prompt-toolkit, dropped our duplicate), settings Dict import, terminal.py union keeping both tab-completion and the toolbar in one PromptSession
- test: suite 109 -> 135 passed (compaction matrix/breaker/engine integration, procedures roundtrip plus mem0 teach-reuse e2e, colleagues' CLI tests green against the merged input path)
- Decided: same-thread compaction over forks, LIKE-first hybrid ordering kept under rerank, tombstones over deletes throughout

# Team:
- Coordinated the merge with teammates (their MCP/completer work vs my memory/compaction work) and kept both sides' features intact
- Aligned that MCP/skills stay with colleagues while memory (episodic, semantic, procedural) plus compaction stay with me
