---
name: "skill-creator"
triggers: ["skill-creator", "create skill", "new routine", "procedure draft", "improve skill", "eval skill"]
scope: "Applies when the student asks to turn a repeated multi-step success into a reusable routine/skill, or to create, improve, or eval such a routine in the StudyMate harness. Does not apply to single-file edits, /ingest runs, or /grill sessions."
version: 1
status: "draft"
supersedes: ""
---

## Steps
1. Capture intent from history first (tools used, step order, user corrections, input/output formats), then ask: (a) what should this routine enable, (b) when should it trigger, (c) expected output format/artifact paths, (d) need test cases? Verifiable file transforms get 2-3 test prompts; subjective style tasks skip assertions by default, user decides.
2. Write the draft with `format_procedure()` shape only (name/triggers/scope/steps/rationale/pitfalls/example), then `write_file` to `workspace/artifacts/procedures/<slug>.md` with `status: draft`. Never write `status: approved` directly; never write outside `workspace/artifacts/`.
3. Create 2-3 realistic student test prompts (e.g. "/ingest workspace/data", "turn this workflow into a routine"). Run each in a fresh `/new` thread following the draft steps with `read_file`/`write_file`/`search_text`/`edit_file`; for automated checks use the `MockLLMClient` eval pattern (`add_response()` enqueued exactly per iteration, assert side-effects + `result.is_success`).
4. Evaluate with the user qualitatively (show created artifacts + completion summary) plus lightweight quantitative checks (`pytest`, `/lint` for `[[wikilinks]]`/orphans, `search_text` hit counts). Explain what was checked; do not invent scores.
5. Iterate on feedback: generalize from the 2-3 examples (no overfit MUSTs, explain why per step), remove unproductive steps found in transcripts, bundle repeated helper work into the routine body. Re-run tests per iteration.
6. Surface for approval, never auto-follow the draft: "I have a draft routine for X — use it?" Human flips `status: draft` to `approved` via `edit_file` with an exact unique target; prefer highest-version routine on conflict and flag conflicts instead of guessing.
7. Optimize triggers for `match_procedures` (substring, case-insensitive, approved-only default): keep 4-8 specific pushy triggers covering paraphrases, add near-miss negatives to scope (when NOT to use), keep SKILL under ~500 lines with pointers to references.

## Rationale
Draft-first plus human approval exists because `soul.md` forbids silently following drafts and auto-injection only fires on `approved` routines. Fresh-thread runs isolate the routine under test from stale `reset_injected_note` leaks. `MockLLMClient` with exact enqueue counts is required because under-enqueue falls back to echo responses that mask iteration bugs. Explaining why beats rigid MUSTs because the ReAct LLM generalizes better from rationale than from caps-lock constraints.
## Pitfalls
- Flipping `status` to `approved` without human confirmation (drafts must be surfaced, never followed silently).
- Second SYSTEM message (`System message must be at the beginning.` 400) — merge facts/procedures into `messages[0]` via `inject_system_note`, never append a new SYSTEM.
- `questionary.Choice(value=None)` returns the title string — use the `_CANCEL` sentinel for cancel paths.
- `edit_file` requires the exact `target` exactly once; multiple hits error — `read_file` first and pick a unique snippet.
- `search_text` is literal substring (`re.escape`), not full regex — do not rely on regex features.
- Writing outside `workspace/artifacts/` or into `workspace/data/` (read-only source of truth); `allow_delete=False` — no delete tool exists.
- Overfitting the routine to the 2-3 demo prompts with fiddly MUSTs instead of general patterns.
- Skipping baselines: always compare with-routine vs without-routine (or old snapshot) before claiming improvement.

## Example
Student: "turn my ingest workflow into a skill". Draft `skill-creator.md` (status draft) via `format_procedure`, test prompts: "ingest workspace/data lecture01.pdf", "improve ingest routine for PDFs". Fresh-thread runs produce `lecture-*.md` + `concept-*.md` cross-linked with `[[wikilinks]]`, `/lint` clean, `pytest` passes. User approves via `edit_file` flipping status; triggers later match "create skill" via `match_procedures`.
