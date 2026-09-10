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

## 5. MCP servers — PLACEHOLDER

---

## Cross-cutting checks (all scenarios)

- No deletions anywhere: `allow_delete=False`; archive/tombstone only.
- Full suite green: `pytest` (currently 136 passed on the demo branch).
- Deterministic offline tests back every memory behavior above
  (`tests/unit/test_memory_*.py`, `tests/e2e/test_memory_*_workflow.py`);
  live InnKube calls happen only in the REPL, never in tests.
