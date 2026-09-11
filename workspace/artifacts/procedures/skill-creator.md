---
name: "skill-creator"
triggers: ["create a skill", "new skill", "improve skill", "skill from scratch", "turn this into a skill", "make a procedure", "optimize triggers"]
scope: "Applies when the user wants to create a reusable procedure bundle from scratch, improve an existing one, or tune its trigger phrases. Does not apply to one-off tasks (just do them), fact storage (use memory_store), or plain Q&A."
version: 1
status: "draft"
supersedes: ""
---

## Steps
1. Capture intent first: what should the routine enable, when should it trigger (exact trigger phrases), what output shape is expected, and whether evals are wanted. Verifiable outputs (file transforms, fixed workflow steps) get test cases; taste-based outputs (writing style) usually skip them. Extract answers from conversation history before asking.
2. Interview for edge cases: input/output formats, example files, success criteria, dependencies. Check existing bundles in `workspace/artifacts/procedures/` and `study/templates.py` first so the user isn't re-explaining known context.
3. Draft the bundle with `format_procedure()`: frontmatter (name, triggers, scope, version, status=draft) plus Steps, Rationale, Pitfalls, Example. Keep it lean; attach the *why* to every non-obvious step instead of writing MUSTs.
4. Write 2-3 realistic test prompts (things a real student would actually type, with detail and casual phrasing — never abstract stubs like "test ingest").
5. Run with-procedure vs baseline, serially (no subagents on this harness): inject via `match_procedures()` + `engine.run()` with a scripted `MockLLMClient`, then the same prompt with no injection. Save runs under `workspace/artifacts/` scratch, not the repo.
6. Grade both ways: quantitative assertions as `pytest` (exact, deterministic — assert on payloads and artifacts, e.g. procedure text present in `mock.call_history`); qualitative outputs shown to the user in the REPL for judgment calls. Compare with vs without, like-for-like.
7. Iterate from feedback: generalize (a routine that only passes its 2-3 evals is useless), trim dead weight (read transcripts, cut steps that waste turns), explain reasoning over rigid rules. Rerun everything per iteration. Stop when the user is happy or feedback goes empty.
8. Tune triggers last: 8-10 should-trigger queries (varied phrasing, casual, edge uses) plus 8-10 near-miss should-not-trigger queries (shared keywords, wrong domain — never obviously-irrelevant ones). Measure precision/recall of `match_procedures()` in pytest; make descriptions a little pushy (our matcher undertriggers on paraphrase) but precise. Near-misses are the whole test.
9. Ship: flip `status` draft→approved (human approval required — never self-approve); when replacing a routine, set `supersedes` instead of deleting.

## Rationale
Subagents, browser reviewers, `eval-viewer`, and `claude -p` from the original skill-creator don't exist here, so each maps to a local equivalent: serial inline runs replace parallel subagents, pytest replaces the benchmark harness and viewer, the REPL replaces the browser review loop. Trigger-phrase evals replace description optimization because our triggering is substring matching over frontmatter, not LLM description reading — which is also why near-miss negatives matter more than positives. Baselines are non-negotiable: without the without-procedure run you can't tell skill from model.

## Pitfalls
- Overfitting to the 2-3 eval prompts (generalize; the routine will meet stranger requests).
- Vague triggers that fire everywhere, or narrow ones that never fire — the trigger eval set catches both directions.
- Duplicating `soul.md` inside the bundle instead of referencing it (two sources rot).
- Storing facts as procedures (know-that vs know-how: preferences go to `memory_store`).
- Skipping baselines or grading only vibes when assertions were possible.
- Letting a distilled draft execute unapproved (policy: drafts surface, never silently followed).

## Example
Creating `ingest-lecture`: triggers ["ingest", "lecture slides"], test prompt "please ingest the slides for chapter 2" (different surface form, same job) vs baseline run without injection. With-procedure payload contains the list-first/page-range/link steps; baseline skips listing. Assertions: routine text in `mock.call_history`, artifacts exist with `[[wikilinks]]`, completion summary non-empty.
