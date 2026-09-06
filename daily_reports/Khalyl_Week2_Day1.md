# My contributions:
- thoroughly went through the specification document (AI_Engineering_Lab_2026.pdf): Week 1 requirements vs our implementation (found diagram gap, max_iterations default violating le=50) and Week 2 §8.3 memory/MCP requirements plus COALA background
- impl: SQLite persistent chat-thread memory on feat/chat-threads-sqlite (MemoryStore with threads/messages tables, archived tombstones, explicit BEGIN IMMEDIATE for DDL atomicity, write guard inside transaction)
- impl: thread naming helper (heuristic slug from first message) plus resolve by id/prefix/name and paged finder
- impl: MemoryConfig in settings.py plus config.yaml section; fixed AgentConfig max_iterations default 60 -> 50
- impl: ContextManager.load_history for thread resume across restarts
- impl: CLI /threads (arrow-key picker with See-more pagination), /thread <#|id|name>, /new, /archive-this, /archive-all with confirmation, persist after each run, transcript restore, local-time display, studymate > prefix
- impl: crash-safe atomic file writes (tmp file plus os.replace) in write_file/edit_file
- test: 17 new tests (memory unit incl. fault-injection atomicity and legacy-DB migration, thread-resume e2e, crash-safety tests); full suite 82 passed
- fixed live-REPL bugs found while testing: Esc-cancel crash (questionary Choice value=None trap) plus regression tests, picker double-highlight (dropped default=, single reverse-video cursor row)
- Decided on tombstone archiving instead of a /drop-db command to preserve the no-delete invariant

# Team:
- Discussed the Week 2 task split with my teammates (chat-threads persistence first, semantic facts/procedural/MCP as follow-ups) and aligned on branch scope
