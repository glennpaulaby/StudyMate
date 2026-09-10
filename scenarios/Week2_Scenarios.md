# Week 2 Demo Scenarios — StudyMate Agent Harness

Live-demo proposal for the Thursday feedback session. Each scenario is runnable
end-to-end on a TA machine with only an `INNKUBE_API_KEY` in `.env`.

```bash
git checkout demo-week2
cp .env.example .env   # then set INNKUBE_API_KEY (needs chat + embedding + reranker grants)

pip install -r requirements.txt
pip install -e .
studymate
```

---

## 1. Episodic memory — chat threads survive restarts (E. Khalyl)

Goal: prove conversations persist across application restarts and are navigable.

1. `student > I struggle with Dijkstra's edge relaxations, help me revise graphs`
   - Observe: `New thread: i-struggle-with-dijkstra...`, answer arrives.
2. `student > /threads`
   - Observe: arrow-key picker lists the thread (`↑/↓ + Enter`, `✕ Cancel` works, `Esc` is not required).
3. Quit (`/exit`), restart `studymate`, then `/threads` → pick the thread.
   - Observe: `✓ Entered thread ... (N messages restored)` **plus the full transcript** (not just a count line).
4. `student > continue where we left off`
   - **Pass:** the agent references edge relaxations without being re-told.
5. `student > /archive-this` → `/threads`
   - **Pass:** thread disappears from the list (tombstoned, nothing deleted — `allow_delete=False` holds).

Also demoable: `/thread <#|id|name>`, `/new [name]`, paged `See more` with 10+/threads.

## 2. Semantic memory — facts, hybrid recall, rerank (E. Khalyl)

Goal: prove durable preferences/facts persist, survive restarts, and are found by
meaning, not just keywords. Uses `octen-embedding-8b` + `qwen3-reranker-4b`
(same key); without those grants the tool transparently degrades to keyword-only.

1. `student > remember that I prefer Anki-style cloze flashcards`
   - Observe agent call `memory_store`; reply confirms `Stored fact #N`.
2. Quit, restart, `student > make me flashcards for Bellman-Ford`
   - **Pass:** cards come out cloze-style with no reminder given (recall via `memory_recall`).
3. Paraphrase check: `student > what do I dislike about studying?` (or any wording
   sharing no keywords with the stored fact)
   - **Pass:** the preference is still surfaced — this exercises the
   `sqlite-vector` cosine path, not substring match.
4. Duplicate check: repeat step 1 verbatim.
   - **Pass:** `Already known (deduplicated)` — no duplicate row.

## 3. Procedural memory — taught routines are followed (E. Khalyl)

Goal: prove a routine taught once is executed on a later, differently-worded task.

1. Show the seed: `workspace/artifacts/procedures/ingest-lecture.md`
   (`approved`, triggers, ordered steps, rationale, pitfalls).
2. `student > ingest the slides for chapter 2` (in `workspace/data/`)
   - Observe: the agent lists files first, reads each fully, writes
   `lecture-<slug>.md` + `concept-<slug>.md`, cross-links with `[[wikilinks]]`,
   ends with a completion summary — the routine's steps, in order.
3. Draft safety: create `workspace/artifacts/procedures/grill-drill.md` with
   `status: draft` and trigger `drill`, then `student > drill me`
   - **Pass:** the agent *asks* before following (`Draft routine ... say the word`),
   never silently executes drafts.

## 4. Working memory — compaction under pressure (E. Khalyl)

Goal: prove long sessions don't die at the context wall.

1. `student > /config` — note `Context Window` (resolved per model) and `Compact Threshold`.
2. Hold a long multi-turn session (ingest + grill + follow-ups), watching the
   sticky bottom status line (`model | thread | ctx x/y (p%)`, gray → amber → red).
3. `student > /compact`
   - Observe: `✓ Compacted: ~Ak → ~Bk tokens. Same thread, history saved.`
   - **Pass:** conversation continues coherently; the `[Auto-compact summary]`
   checkpoint is visible in history and persisted to the thread.
4. Auto path: same, but keep chatting past ~80% instead of typing `/compact`.

## 5. Web research MCP — search, domain filter, deep crawl (B. Putra)

Goal: prove the agent can search the live web, target specific domains, crawl linked pages, and ingest structured Markdown without third-party search API keys.

1. Quick search & source deduplication (`depth=0`):
   - `student > search the web for Bellman-Ford algorithm with depth 0`
   - Observe: agent calls `research_topic(topic="Bellman-Ford algorithm", depth=0)`.
   - **Pass:** returns clean summary with deduplicated source links; DuckDuckGo redirect tokens (`/l/?uddg=...`) are unquoted to real URLs.
2. Domain-targeted search:
   - `student > research dynamic programming on en.wikipedia.org`
   - Observe: agent calls `research_topic(topic="dynamic programming", domains=["en.wikipedia.org"])`.
   - **Pass:** search results are isolated to `site:en.wikipedia.org`.
3. Deep crawl & structured Markdown parsing (`depth=1`):
   - `student > research Dijkstra vs Bellman-Ford negative weight handling with depth 1 and max_pages 3`
   - Observe: agent calls `research_topic(..., depth=1, max_pages=3)`, crawling linked same-site pages.
   - **Pass:** extracted content is formatted as clean Markdown (`# Headings`, `- Lists`, ```` ```code``` ````); boilerplate like `<script>`, `<style>`, and `<nav>` is stripped.

## 6. Obsidian MCP — graph traversal, section editing, stdio server (B. Putra)

Goal: prove safe, non-destructive vault operations, knowledge graph traversal via wikilinks, section-level editing, and external FastMCP stdio hosting.

1. Content search & safe write:
   - `student > search the vault for "Dijkstra" and show me what we have`
   - Observe: agent calls `obsidian_search_notes(query="Dijkstra")` then `obsidian_read_note("concepts/dijkstra.md")`.
   - **Pass:** surfaces exact matching line numbers and content.
2. Overwrite safety:
   - `student > write a new note to concepts/dijkstra.md saying "test"`
   - Observe: agent calls `obsidian_write_note(path="concepts/dijkstra.md", content="test", overwrite=False)`.
   - **Pass:** raises `FileExistsError` refusing accidental overwrite (`overwrite=true` required). No data is lost.
3. Knowledge graph navigation (outgoing & backlinks):
   - `student > what does concepts/dijkstra.md link to, and what notes link back to it?`
   - Observe: agent calls `obsidian_get_outgoing_links` and `obsidian_get_backlinks`.
   - **Pass:** returns outgoing `[[wikilinks]]` (aliases `|display` and `#anchors` stripped) and all notes referencing `dijkstra` across the vault.
4. Targeted section insertion:
   - `student > add a pitfall note under the "Core Intuition" section of concepts/dijkstra.md that it fails on negative weights`
   - Observe: agent calls `obsidian_insert_into_section(path="concepts/dijkstra.md", heading="Core Intuition", content="- Warning: fails on negative edge weights.")`.
   - **Pass:** content appends strictly inside that heading section before the next `#` header; YAML frontmatter and template structure remain intact.
5. External FastMCP stdio server:
   - Run in terminal: `python -m study_agent.tools.mcp.server`
   - **Pass:** boots stdio server exposing all 10 tools for Claude Desktop, Cursor, or MCP inspector.

Also demoable: autocomplete dropdown in REPL for slash commands & tool names (`prompt_toolkit`), launcher window (`studymate-window`).

## 7. Multi-step autonomous scenario — Exam prep & knowledge expansion (G. Aby)

Goal: prove autonomous multi-step reasoning orchestrating Long-Term Memory, Custom MCP tools, and Web Research without human intervention. (Formal benchmark: `tests/e2e/test_mcp_memory_scenario.py`).

1. Pre-condition (seeded state):
   - Memory DB has past struggle: *"Student struggles with negative edge cycles and why Dijkstra fails on negative weights."*
   - Vault contains baseline note: `concepts/dijkstra.md` (links to `[[graphs]]`, `[[shortest-path]]`).
2. `student > I need to review Dijkstra vs Bellman-Ford for my exam. Check what I struggled with earlier, inspect our vault notes, research negative cycle detection, update the notes, and record my progress.`
3. Observe autonomous ReAct execution trace (8 consecutive tool calls):
   - **Step 1 (Memory Recall):** `memory_recall("dijkstra struggle")` $\to$ retrieves past weakness.
   - **Step 2 (Vault Search):** `obsidian_search_notes("Dijkstra")` $\to$ finds `concepts/dijkstra.md`.
   - **Step 3 (Vault Read):** `obsidian_read_note("concepts/dijkstra.md")` $\to$ identifies missing coverage on negative cycles.
   - **Step 4 (Graph Traversal):** `obsidian_get_outgoing_links("concepts/dijkstra.md")` $\to$ traverses graph (`[[graphs]]`, `[[shortest-path]]`).
   - **Step 5 (Web Research):** `research_topic("Bellman-Ford negative cycle detection", depth=0)` $\to$ fetches external cycle detection insights.
   - **Step 6 (Section Update):** `obsidian_insert_into_section("concepts/dijkstra.md", "Core Intuition", ...)` $\to$ updates note without touching YAML frontmatter.
   - **Step 7 (New Concept Note):** `obsidian_write_note("concepts/negative-cycles.md", ...)` $\to$ creates new atomic note with reciprocal `[[wikilinks]]`.
   - **Step 8 (Memory Consolidation):** `memory_store(...)` $\to$ saves newly mastered milestone into `memory.db`.
   - **Step 9 (Synthesized Response):** Agent gives final response summarizing notes and confirmed memory.
4. **Pass:**
   - `concepts/dijkstra.md` has the new note content inside `Core Intuition`.
   - `concepts/negative-cycles.md` is created with valid YAML frontmatter and links.
   - `/lint` confirms 0 broken links and 0 orphaned concepts across the vault.
   - `memory_recall` confirms `"Mastered negative cycle detection..."` persists across restarts.
   - All 8 tool operations run autonomously without user intervention or path escapes.

---

## Cross-cutting checks (all scenarios)

- No deletions anywhere: `allow_delete=False`; archive/tombstone only.
- Full suite green: `pytest` (136 passed on the demo branch).
- Deterministic offline tests back every memory behavior above
  (`tests/unit/test_memory_*.py`, `tests/e2e/test_memory_*_workflow.py`).
- Deterministic offline unit and E2E benchmark tests back MCP and multi-step reasoning
  (`tests/unit/test_mcp.py`, `tests/e2e/test_mcp_memory_scenario.py`);
  live InnKube calls happen only in the REPL, never in tests.
