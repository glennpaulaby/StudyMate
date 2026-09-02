# AGENTS.md — StudyMate

> Read this before every task. Concise, actionable rules for AI agents working on this repo.

## 1. Project Overview

StudyMate is an autonomous AI agent harness (Python 3.12) for the AI Engineering Lab (Uni Passau, SoSe 2026). It helps students turn lecture materials into structured Obsidian knowledge bases via a terminal REPL (`studymate`).

Built on **ReAct (Reason + Act)**: the agent ingests files from `workspace/data/` (read-only), generates linked concept notes, lecture summaries, flashcards, and mind maps in `workspace/artifacts/`, and provides vault linting (`/lint`) and Socratic grilling (`/grill <topic>`). All file I/O is sandboxed to `./workspace/` with strict jail containment and no-delete invariant.

## 2. Tech Stack

- **Language/Runtime**: Python 3.12 (requires `>=3.12`, `python:3.12-slim-bookworm` in Docker)
- **Package manager**: `pip` + `requirements.txt`; packaging via `setuptools` (`pyproject.toml`), editable install `pip install -e .`
- **Core deps** (`pyproject.toml:11-18`): `pydantic>=2.7`, `pyyaml>=6.0`, `httpx>=0.27`, `openai>=1.30` (InnKube OpenAI-compatible endpoint), `rich>=13.7`, `pypdf>=4`
- **Dev deps** (`pyproject.toml:21-28`): `pytest>=8`, `pytest-cov`, `pytest-mock`, `ruff>=0.4`, `mypy>=1.10` (installed but **no ruff/mypy config files exist** — inferred convention)
- **LLM endpoint**: InnKube `https://llms.innkube.fim.uni-passau.de`, models `qwen-agentworld-35b-a3b` (default), `gemma4-31b-it`
- **Entry point**: `studymate = study_agent.cli.terminal:main` (`pyproject.toml:30-31`)
- **Container**: `Dockerfile` (ENTRYPOINT `studymate`); `compose.yaml` referenced in README but **not present on disk** — check before using

## 3. Repo Structure

```
group-11/
├── config/config.yaml      # LLM, agent, tools, observability settings (source of truth for defaults)
├── config/soul.md          # Agent persona / system prompt injected via ContextManager
├── src/study_agent/
│   ├── cli/terminal.py     # REPL loop, slash-command dispatcher, Rich rendering
│   ├── config/settings.py  # Pydantic AppConfig hierarchy (LLMConfig, AgentConfig, etc.)
│   ├── config/loader.py    # YAML + .env loader (priority: env > .env > YAML > defaults)
│   ├── core/engine.py      # ReAct loop (AgentEngine.run)
│   ├── core/context.py     # ContextManager — system prompt + message history
│   ├── core/types.py       # Shared Pydantic models: Role, ToolCall, ChatMessage, LLMResponse, AgentRunResult
│   ├── llm/base.py         # BaseLLMClient ABC
│   ├── llm/client.py       # OpenAICompatibleLLMClient (InnKube)
│   ├── llm/mock.py         # MockLLMClient for offline testing
│   ├── tools/base.py       # BaseTool ABC + ToolRegistry
│   ├── tools/filesystem.py # PathSecurityManager + 7 filesystem tools (see Architecture)
│   ├── study/templates.py  # Obsidian CONCEPT_TEMPLATE, LECTURE_TEMPLATE, format_concept_note()
│   ├── study/linter.py     # VaultLinter + VaultLintReport
│   ├── study/griller.py    # SocraticGriller (Level 1→3 prompts)
│   ├── memory/             # Stub — Week 2 (persistent memory, do not pre-populate)
│   ├── observability/      # Stub — Week 3 (Prometheus)
│   └── subagents/          # Stub — Week 3 (delegation)
├── tests/unit/             # Unit tests per module (test_config, test_context, test_tools, etc.)
├── tests/e2e/              # Benchmark E2E tests (test_file_operations, test_study_workflows)
├── tests/conftest.py       # Fixtures: sample_config, mock_llm, temp_workspace, sandbox_env
├── workspace/data/         # Read-Only source materials (empty in repo, agent must handle missing)
├── workspace/artifacts/    # Read/Write generated notes (empty in repo)
├── Dockerfile              # python:3.12-slim-bookworm
├── requirements.txt        # Locked runtime deps (mirrors pyproject.toml)
└── pyproject.toml          # Build, deps, scripts, pytest config
```

## 4. Setup & Commands

All commands assume `cd project/group-11` and `python -m venv venv` activated.

```bash
# Install
pip install -r requirements.txt
pip install -e .                          # exposes `studymate` CLI

# Env (required for live LLM)
cp .env.example .env                      # then set INNKUBE_API_KEY
# .env.example: INNKUBE_API_KEY, STUDY_AGENT_ENV, LOG_LEVEL

# Run REPL
studymate                                 # or: python -m study_agent.cli.terminal

# Tests — from pyproject.toml:33-36 testpaths=["tests"] pythonpath=["src"] addopts="-v"
pytest                                    # all 31 tests (unit + e2e)
pytest tests/unit/test_tools.py -v        # single file
pytest --cov=study_agent --cov-report=term-missing  # coverage (if pytest-cov installed)
.\venv\Scripts\pytest.exe --collect-only  # Windows: use venv binary directly if PATH missing

# Lint / Typecheck — tools installed but NO config file; use defaults
ruff check src/ tests/                    # inferred — no ruff.toml/.eslintrc exists
mypy src/                                # inferred — no mypy.ini

# Docker (Dockerfile ENTRYPOINT is `studymate`; compose.yaml missing — verify first)
docker build -t studymate .
docker run -it --rm -e INNKUBE_API_KEY="..." -v ./workspace:/app/workspace studymate
# docker compose run --rm studymate  — only if compose.yaml restored
```

## 5. Code Style & Conventions

Verified by sampling `src/study_agent/**/*.py` and `tests/**/*.py` — not just linter config:

- **Naming**: `snake_case` for files, functions, variables; `PascalCase` for classes (`AgentEngine`, `VaultLinter`, `PathSecurityManager`); `SCREAMING_SNAKE` only for constants like `DEFAULT_SYSTEM_PROMPT`. Tool names are `snake_case` strings (`create_folder`, `read_pdf`).
- **File organization**: Feature-layered modules (`cli/`, `config/`, `core/`, `llm/`, `tools/`, `study/`). One class per file for tools; re-export via `study_agent/tools/__init__.py`. No barrel re-exports elsewhere.
- **Typing**: Type hints everywhere; `Optional[X]`, `List[X]`, `Dict[str, Any]` from `typing`. Pydantic `BaseModel` + `Field(default=..., ge=..., description=...)` for all config/types (`src/study_agent/config/settings.py:5-50`, `src/study_agent/core/types.py:1-83`). Return types explicit on public methods.
- **Imports**: stdlib first (`pathlib`, `re`, `typing`), then third-party (`pydantic`, `yaml`, `openai`, `rich`), then local (`study_agent.*`). No wildcard imports.
- **Error handling**: Tools **never raise to the LLM** — `ToolRegistry.execute()` catches and returns `"Error: ..."` strings (`src/study_agent/tools/base.py:73-86`). Security violations raise `PermissionError` inside `PathSecurityManager` then surfaced as error strings by registry. LLM client wraps `openai` errors into `RuntimeError`/`TimeoutError`/`ConnectionError` (`src/study_agent/llm/client.py:86-103`).
- **Logging**: `logging.getLogger(__name__)` per module, `logger.debug`/`warning`/`error` — no `print()`.
- **Docstrings**: Class-level and method-level triple-quoted docstrings on public APIs; short one-liners on properties.
- **Pydantic patterns**: `Field(default_factory=...)` for mutable defaults; `AppConfig(**yaml_data)` construction in `loader.py`. `ChatMessage.to_dict()` serializes via `json.dumps` for tool args (`src/study_agent/core/types.py:37-59`).
- **Template usage**: Always use `format_concept_note()` / constants from `study/templates.py` — never hand-roll frontmatter. Frontmatter requires `title` + `status` (`src/study_agent/study/linter.py:86-88`).
- **Windows compat**: `src/study_agent/cli/terminal.py:5-12` reconfigures stdout/stderr to UTF-8 on win32 — preserve this.

## 6. Testing Requirements

- **Framework**: `pytest` with `pytest-mock` and `MockLLMClient` (`src/study_agent/llm/mock.py`). Config in `pyproject.toml:33-36` — `testpaths=["tests"]`, `pythonpath=["src"]`, `-v`.
- **Location/naming**: `tests/unit/test_*.py` and `tests/e2e/test_*.py`, each test `test_<feature>` with docstring describing benchmark. `tests/conftest.py` provides `sample_config`, `mock_llm`, `temp_workspace` fixtures — reuse them.
- **Patterns** (inferred from existing tests):
  - Use `tmp_path` + `PathSecurityManager`/`ToolRegistry` with explicit `FilesystemToolConfig` rather than real workspace.
  - Script LLM behavior with `mock_llm.add_response(content=..., tool_calls=[ToolCall(...)])` sequentially — count iterations/tool_calls.
  - Assert both side-effects (file exists, content contains `"one"`/`"twenty"`) **and** `result.is_success`/`result.final_response` (see `tests/e2e/test_file_operations.py:70-82`).
  - Vault tests: call `VaultLinter(tmp_path).lint()` and assert `report.broken_links`/`orphan_pages`/`is_healthy` (`tests/unit/test_vault_linter.py`).
- **What needs tests**: Any new tool, `PathSecurityManager` rule, `AgentEngine` branch, `VaultLinter` check, or template formatter. E2E for ingestion/linking workflows.
- **Running**: `pytest` must pass before PR. Coverage via `pytest --cov` if needed — no threshold enforced yet (inferred).
- **Flakes**: No flaky tests observed; `MockLLMClient` is deterministic. Real `OpenAICompatibleLLMClient` tests are skipped unless `INNKUBE_API_KEY` is set.

## 7. Architecture Notes

Respect these invariants — violating them breaks benchmarks:

- **ReAct loop** (`src/study_agent/core/engine.py:29-114`): `run()` appends user message → loop: `llm.chat(messages, tools)` → append assistant msg → if no `tool_calls` return `AgentRunResult(is_success=True)` → else `registry.execute()` each call → append `TOOL` observation. Honors `AgentConfig.max_iterations` (default 15); on exceed returns `is_success=False` with `"maximum number of reasoning steps"` message. Supports optional `on_tool_start`/`on_tool_end` callbacks for UI.
- **Context** (`src/study_agent/core/context.py`): `ContextManager` loads `config/soul.md` (or `custom_system_prompt` or `DEFAULT_SYSTEM_PROMPT`) as first `SYSTEM` message. `clear(keep_system_prompt=True)` resets to just system prompt. No token truncation — messages accumulate.
- **Tool registry** (`src/study_agent/tools/base.py`): `BaseTool` requires `name`/`description`/`parameters` (JSON Schema) + `execute(**kwargs)->str`. `ToolRegistry.list_schemas()` → OpenAI `type:function` format. `execute()` returns error string if tool missing or throws.
- **Security / Sandbox** (`src/study_agent/tools/filesystem.py:9-68`): `PathSecurityManager` resolves all paths against `base_dir` (CWD by default). `allowed_read_paths=["./workspace"]`, `allowed_write_paths=["./workspace/artifacts"]` (`config/config.yaml:29-32`). `validate_read_path`/`validate_write_path` use `Path.resolve()` + `relative_to` check — blocks `../` traversal and absolute escapes. `allow_delete=False` always — no delete tool exists and must not be added without design review (Week 3 permission tiers pending).
- **Filesystem tools** (7 total, `src/study_agent/tools/filesystem.py:70-455`): `create_folder`, `write_file` (creates parents), `read_file` (offset/limit pagination, header `Total lines: X | Showing lines Y-Z`), `read_pdf` (pypdf, `page_start`/`page_end`, default 10 pages), `list_files` (dirs first, sorted), `search_text` (escaped regex, case-insensitive, max 50 hits), `edit_file` (exact `target` must appear exactly once or errors).
- **Config loading** (`src/study_agent/config/loader.py:8-59`): Priority env > `.env` > YAML > Pydantic defaults. `.env` parsed manually (no `python-dotenv`). API key injected from `INNKUBE_API_KEY` or `OPENAI_API_KEY` if `config.llm.api_key` empty.
- **Vault system** (`src/study_agent/study/linter.py`): `VaultLinter.lint()` walks `vault_dir` for `.md`, maps slugs (`stem.lower()` + filename), parses `[[wikilinks]]` (strips `|display` and `#anchor`), frontmatter YAML (`title`/`status` required), and `## Open Questions` (`- [ ]` items). Reports `broken_links`, `orphan_pages` (excludes `index`/`readme`/`topics`/`connections`), `open_questions`, `metadata_issues`.
- **Templates** (`src/study_agent/study/templates.py`): `CONCEPT_TEMPLATE` YAML frontmatter + 7 sections; `LECTURE_TEMPLATE` + `FLASHCARD_TEMPLATE`. Use `format_concept_note()` helper — it lowercases course for tags, ISO date for `last_updated`.
- **CLI** (`src/study_agent/cli/terminal.py`): REPL with Rich. Slash commands: `/help`, `/lint [path]`, `/grill <topic>` (rewrites to `SocraticGriller.create_initial_prompt`), `/ingest [path]` (rewrites to ingestion prompt targeting `workspace/artifacts/lecture-<slug>.md` + `concept-<slug>.md`), `/templates`, `/clear`, `/config`, `/exit`. `/lint` defaults to `artifacts_dir`; `/grill` and `/ingest` are prompt rewrites fed into `engine.run()`.
- **Stubs**: `memory/`, `observability/`, `subagents/`, `tools/mcp/` are empty `__init__.py` placeholders for Weeks 2-3 — do not implement unless the roadmap task explicitly requires it.

## 8. Do's and Don'ts

**Do:**
- Always route file I/O through `ToolRegistry` + `PathSecurityManager` — never use raw `Path.write_text`/`open()` outside tools in agent-facing code.
- Keep `workspace/data` read-only and `workspace/artifacts` as only write target — enforce via `FilesystemToolConfig`.
- Use `MockLLMClient` in tests; never make live InnKube calls in unit/e2e tests.
- Use `format_concept_note()` and `CONCEPT_TEMPLATE`/`LECTURE_TEMPLATE` for generated notes — include `[[wikilinks]]` for connections.
- Preserve `allow_delete=False` and the no-delete guarantee in `config/config.yaml:28` and `settings.py:28`.
- Run `pytest` locally before pushing; keep `ChatMessage.to_dict()` `json.dumps` behavior for tool args.
- Respect `max_iterations` budget and return `is_success=False` on exhaustion — don't loop infinitely.

**Don't:**
- Don't add a delete/remove tool or set `allow_delete=True` — Week 1 spec strictly prohibits deletion (`README:24`, `config/soul.md:20-21`).
- Don't write outside `workspace/` — path jail will block it and break benchmarks (`test_security_path_jail_escape_attempt`).
- Don't write to `workspace/data/` — it's the immutable source of truth (`test_security_write_to_read_only_data_dir`).
- Don't hand-roll YAML frontmatter or Obsidian templates — use `study/templates.py`.
- Don't bypass `ToolRegistry.execute()` error handling — don't let tools throw uncaught exceptions to the LLM.
- Don't add new runtime deps without updating both `pyproject.toml` and `requirements.txt` (they are kept in sync, `Dockerfile:14` installs both).
- Don't commit `.env` or API keys — `.env` is gitignored (` .gitignore:33-36`), use `.env.example` as template.
- Don't implement `memory/`, `observability/`, `subagents/` stubs ahead of Week 2/3 milestones.
- Don't add `compose.yaml`/`ruff.toml`/`mypy.ini` with conflicting settings without team agreement — they are currently absent by design.

## 9. PR / Commit Conventions

Inferred from `git log --oneline` and branches — no `CONTRIBUTING.md` exists:

- **Commit format**: Conventional Commits — `feat(scope):`, `fix(scope):`, `chore:` prefix. Examples: `feat(cli): add /ingest command`, `fix(core): use json.dumps for tool call serialization`, `chore: initialize scaffolding`. Keep subject imperative, lowercase after colon.
- **Scopes used**: `cli`, `tools`, `core`, `study` — follow existing.
- **Branch naming**: `feat/<name>` (e.g. `feat/tools-and-react-loop`, `feat/studymate-core`), `chore/<name>`. Base branch is `main` (also `origin/HEAD -> origin/main`).
- **PR checklist** (inferred — no template exists):
  - `pytest` passes locally
  - No secrets committed (check `.env` not staged)
  - If adding tools, include unit test + e2e benchmark update
  - Update `README.md` Project Structure / Roadmap if adding modules
  - Keep `Dockerfile` COPY lines in sync with new top-level dirs

## 10. Known Gotchas

- **No compose.yaml on disk** despite `README` documenting `docker compose run --rm studymate` — `compose.yaml` was in a prior commit (`a85b666 compose`) but is currently missing; verify or recreate before using compose. `Dockerfile` alone works via `docker build`/`run`.
- **No linter configs** — `ruff`/`mypy` are in `dev` deps but no `ruff.toml`/`pyproject.tool.ruff`/`mypy.ini`/` .editorconfig` exists; running them uses defaults which may not match future team config. Pin explicitly if adding.
- **Windows UTF-8**: CLI forces `sys.stdout.reconfigure(encoding="utf-8")` (`cli/terminal.py:5-12`) — without it, Rich + emoji crash on Windows PowerShell. Don't remove.
- **`workspace/` empty**: Both `workspace/data/` and `workspace/artifacts/` are empty in the repo. Tests use `tmp_path` workspaces, not the real one. Agent must handle missing/empty dirs gracefully (`VaultLinter.lint()` returns empty report if dir missing).
- **`INNKUBE_API_KEY` required live**: REPL warns if missing (`cli/terminal.py:160-164`) but doesn't crash; `OpenAICompatibleLLMClient` falls back to `placeholder_key` for mock init. E2E with real LLM will fail auth without `.env`.
- **`search_text` is substring, not full regex**: Code does `re.escape(query)` (`tools/filesystem.py:298`) — query is treated as literal case-insensitive text despite docstring saying "regex". Don't rely on regex features.
- **`edit_file` strictness**: Requires exact `target` present exactly once (`count == 1`); multiple occurrences returns `"appears N times"` error ( `tools/filesystem.py:367-369`). Agents should read file first and pick a unique snippet.
- **`PathSecurityManager` base_dir**: Defaults to `Path.cwd()` — in tests it's overridden via `base_dir=tmp_path/workspace`. If running tools outside repo root (e.g. Docker `WORKDIR /app`), resolve paths relative to that base.
- **Mock consumption order**: `MockLLMClient._current_index` increments per `chat()` call; if you enqueue fewer responses than engine iterations, it falls back to `"[Mock Response to: ...]"` echo — can mask iteration bugs. Always enqueue exactly `iterations` responses in tests.
- **Import path**: `pythonpath = ["src"]` in `pyproject.toml:35` — imports are `from study_agent.cli.terminal import main`, not `from src.study_agent...`. CI/tests will fail if this is removed.
