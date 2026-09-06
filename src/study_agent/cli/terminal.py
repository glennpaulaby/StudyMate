import sys
from pathlib import Path
from typing import Optional

# Ensure UTF-8 output encoding on Windows terminals to support emojis and Unicode symbols
if sys.platform == "win32":
    try:
        if sys.stdout and hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        if sys.stderr and hasattr(sys.stderr, "reconfigure"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from study_agent.config.loader import load_config
from study_agent.config.settings import AppConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.base import BaseLLMClient
from study_agent.llm.client import OpenAICompatibleLLMClient
from study_agent.memory.store import MemoryStore
from study_agent.memory.threads import generate_thread_name, resolve_thread
from study_agent.study.griller import SocraticGriller
from study_agent.study.linter import VaultLinter
from study_agent.study.templates import CONCEPT_TEMPLATE, FLASHCARD_TEMPLATE, LECTURE_TEMPLATE
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools

console = Console()



def print_banner(config: AppConfig) -> None:
    """Displays the welcome banner in the terminal."""
    banner_text = (
        f"[bold cyan]StudyMate[/bold cyan] [dim]— Autonomous Academic Study Assistant[/dim]\n"
        f"[dim]Model:[/dim] [green]{config.llm.model}[/green]  "
        f"[dim]Endpoint:[/dim] [yellow]{config.llm.base_url}[/yellow]\n"
        f"[dim]Type your question or study goal. Type [bold]/help[/bold] for commands or [bold]/exit[/bold] to quit.[/dim]"
    )
    console.print(Panel(banner_text, title="🎓 Welcome to StudyMate", border_style="cyan"))


def print_help() -> None:
    """Displays available interactive CLI commands."""
    table = Table(title="StudyMate Commands", show_header=True, header_style="bold magenta")
    table.add_column("Command", style="cyan", width=20)
    table.add_column("Description", style="white")
    table.add_row("/help", "Show this help table")
    table.add_row(escape("/ingest [path]"), "Ingest source files and generate Obsidian concept notes + lecture summary")
    table.add_row(escape("/lint [path]"), "Run vault health check (broken links, orphans, open questions)")
    table.add_row("/grill <topic>", "Launch an interactive Socratic grilling sparring session")
    table.add_row("/templates", "Display standard Obsidian markdown templates")
    table.add_row("/threads", "List previous chat threads")
    table.add_row(escape("/thread <id|name>"), "Enter/continue a previous thread")
    table.add_row(escape("/new [name]"), "Start a new thread (default on first message)")
    table.add_row(escape("/archive-this"), "Archive the current thread (tombstone, no delete)")
    table.add_row(escape("/archive-all"), "Archive ALL threads (asks for confirmation)")
    table.add_row("/clear", "Reset in-memory history (thread history stays saved)")
    table.add_row("/config", "Display current configuration parameters")
    table.add_row("/exit, /quit", "Exit StudyMate")
    console.print(table)


def format_local_time(iso_ts: str) -> str:
    """Formats a UTC ISO timestamp in the user's local timezone."""
    from datetime import datetime as _dt

    try:
        return _dt.fromisoformat(iso_ts).astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return iso_ts[:16].replace("T", " ")


def render_message(role: str, content: Optional[str]) -> None:
    """Renders one chat message with a speaker prefix (student > / studymate >)."""
    if role == "user":
        console.print("[bold cyan]student > [/bold cyan]" + escape(content or ""))
    elif role == "assistant":
        if (content or "").strip():
            console.print("[bold magenta]studymate > [/bold magenta]", end="")
            console.print(Markdown(content or ""))
        else:
            console.print("[bold magenta]studymate > [/bold magenta][dim](tool action, no text)[/dim]")
    elif role == "tool":
        console.print(f"  [dim]↳ tool observation: {escape((content or '')[:200])}[/dim]")
    else:
        console.print(escape(content or ""))


def _visible_history(messages) -> list:
    """Filters out system prompts and empty tool-only assistant turns."""
    visible = []
    for msg in messages or []:
        role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        if role == "system":
            continue
        if role == "assistant" and not (msg.content or "").strip() and not msg.tool_calls:
            continue
        visible.append(msg)
    return visible


def _ask_show_older(count: int, remaining: int) -> bool:
    """Asks whether to reveal an older page (TTY only; defaults False)."""
    import sys as _sys

    try:
        import questionary as _q

        if bool(getattr(_sys.stdin, "isatty", lambda: False)()):
            try:
                answer = _q.confirm(
                    f"Show {count} older message(s)? ({remaining} remaining)"
                ).ask()
            except KeyboardInterrupt:
                console.print()
                return False
            return bool(answer)
    except (ImportError, Exception):
        pass
    try:
        from rich.prompt import Confirm as _Confirm

        return bool(_Confirm.ask(f"Show {count} older message(s)? ({remaining} remaining)", default=False))
    except (KeyboardInterrupt, EOFError):
        console.print()
        return False


def render_history(messages, page_size: int = 10) -> None:
    """Prints a restored transcript, most recent page first with show-older paging."""
    import sys as _sys

    visible = _visible_history(messages)
    if not visible:
        console.print("[dim](empty thread — no messages yet)[/dim]")
        return
    total = len(visible)
    is_tty = bool(getattr(_sys.stdin, "isatty", lambda: False)())
    if not is_tty:
        for msg in visible:
            role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
            render_message(role, msg.content)
        console.print()
        return
    start = max(0, total - page_size)
    for msg in visible[start:]:
        role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
        render_message(role, msg.content)
    remaining = start
    while remaining > 0:
        chunk = min(page_size, remaining)
        if not _ask_show_older(chunk, remaining):
            break
        start = remaining - chunk
        console.print(f"[dim]--- {chunk} older message(s) ---[/dim]")
        for msg in visible[start:start + chunk]:
            role = msg.role.value if hasattr(msg.role, "value") else str(msg.role)
            render_message(role, msg.content)
        remaining = start
    console.print(f"[dim](showing {total - remaining} of {total} messages)[/dim]\n")


THREADS_PAGE_SIZE = 10

_SEE_MORE = "__see_more__"
_CANCEL = "__cancel__"  # explicit sentinel: Choice(value=None) defaults to title, so None is unusable


def _thread_choice_title(store: MemoryStore, t, active_id: Optional[str]) -> str:
    """One-line label for a thread choice; active thread is prefixed and tagged."""
    if t.id == active_id:
        return f"● {t.name}  ({store.count_messages(t.id)} msgs, {format_local_time(t.updated_at)}) — current"
    return f"  {t.name}  ({store.count_messages(t.id)} msgs, {format_local_time(t.updated_at)})"


def _picker_style():
    """StudyMate picker theme: exactly one highlight = the cursor row (whole row, reverse video).

    Deliberately no `selected` styling: questionary pins `class:selected` onto
    the `default=` row persistently, which reads as a second stuck highlight.
    The active thread is marked by text (● + "— current") instead.
    """
    try:
        from questionary import Style as _Style

        return _Style(
            [
                ("pointer", "bold cyan"),
                ("highlighted", "reverse bold"),
                ("answer", "bold green"),
                ("selected", ""),
            ]
        )
    except (ImportError, Exception):
        return None


def pick_thread_interactive(store: MemoryStore, active_id: Optional[str] = None, page_size: int = 10):
    """Arrow-key thread picker (↑/↓ + Enter) with See-more pagination.

    Each request fetches one page (`limit=page_size`); choosing
    "See more" issues the next paged request. Numbered fallback
    when not a TTY. Returns the chosen ThreadRecord or None on cancel.
    """
    import sys as _sys

    total = store.count_threads()
    if total == 0:
        console.print("[yellow]No previous threads yet. Just ask something to start one.[/yellow]")
        return None

    shown: list = []
    offset = 0
    while True:
        page = store.list_threads(limit=page_size, offset=offset)
        if not page:
            return None
        shown.extend(page)
        has_more = len(shown) < total

        try:
            import questionary as _q

            if bool(getattr(_sys.stdin, "isatty", lambda: False)()):
                # Note: lone Esc is swallowed by prompt_toolkit (Meta-key prefix),
                # so an explicit Cancel choice + Ctrl-C are the reliable exits.
                choices = [
                    _q.Choice(title=_thread_choice_title(store, t, active_id), value=t)
                    for t in shown
                ]
                if has_more:
                    choices.append(
                        _q.Choice(title=f"› See more ({len(shown)} of {total})…", value=_SEE_MORE)
                    )
                choices.append(_q.Choice(title="✕ Cancel", value=_CANCEL))
                # No `default=`: it pins a persistent `selected` highlight on that
                # row, independent of the cursor. Single highlight = cursor row.
                try:
                    chosen = _q.select(
                        f"Select a thread — showing {len(shown)} of {total} (↑/↓ + Enter):",
                        choices=choices,
                        style=_picker_style(),
                    ).ask()
                except KeyboardInterrupt:
                    console.print()
                    return None
                if chosen is None or chosen == _CANCEL:
                    return None
                if chosen == _SEE_MORE:
                    offset += page_size
                    continue
                if not hasattr(chosen, "id"):
                    return None  # defensive: never let a raw string reach the caller
                return chosen
        except (ImportError, Exception):
            pass

        # Fallback: numbered prompt (tests, non-TTY, questionary missing)
        from rich.prompt import Prompt

        table = Table(title=f"Chat Threads ({len(shown)} of {total})", show_header=True, header_style="bold magenta")
        table.add_column("#", style="cyan", width=4)
        table.add_column("Name", style="white")
        table.add_column("Messages", style="green")
        table.add_column("Updated (local)", style="dim")
        for i, t in enumerate(shown, start=1):
            marker = " [bold cyan]● current[/bold cyan]" if t.id == active_id else ""
            table.add_row(
                str(i),
                escape(t.name) + marker,
                str(store.count_messages(t.id)),
                escape(format_local_time(t.updated_at)),
            )
        if has_more:
            table.add_row("m", "[dim]See more…[/dim]", "", "")
        console.print(table)
        try:
            hint = "[cyan]Enter # / 'm' for more (Enter to cancel)[/cyan]" if has_more else "[cyan]Enter # (Enter to cancel)[/cyan]"
            choice = Prompt.ask(hint, default="", show_default=False).strip()
        except (KeyboardInterrupt, EOFError):
            console.print()
            return None
        if not choice:
            return None
        if has_more and choice.lower() == "m":
            offset += page_size
            continue
        if not choice.isdigit() or not (1 <= int(choice) <= len(shown)):
            console.print(f"[red]Invalid choice: {escape(choice)}[/red]")
            return None
        return shown[int(choice) - 1]


def print_threads(store: MemoryStore, active_id: Optional[str] = None, page_size: int = 10) -> None:
    """Displays the first page of threads (non-interactive list)."""
    total = store.count_threads()
    if total == 0:
        console.print("[yellow]No previous threads yet. Just ask something to start one.[/yellow]")
        return
    threads = store.list_threads(limit=page_size, offset=0)
    table = Table(title=f"Chat Threads ({len(threads)} of {total})", show_header=True, header_style="bold magenta")
    table.add_column("#", style="cyan", width=4)
    table.add_column("Name", style="white")
    table.add_column("Messages", style="green")
    table.add_column("Updated (local)", style="dim")
    for i, t in enumerate(threads, start=1):
        marker = " ●" if t.id == active_id else ""
        table.add_row(
            str(i),
            escape(t.name) + marker,
            str(store.count_messages(t.id)),
            escape(format_local_time(t.updated_at)),
        )
    console.print(table)
    console.print("[dim]Use /threads to browse all pages, /thread <id|name> to jump directly.[/dim]")


def run_vault_lint(target_dir: str) -> None:
    """Executes vault health check and prints a structured Rich summary."""
    linter = VaultLinter(target_dir)
    with console.status("[bold green]Linting vault knowledge graph...[/bold green]"):
        report = linter.lint()

    console.print()
    if report.total_files == 0:
        console.print(f"[yellow]No Markdown files found to lint in '{target_dir}'.[/yellow]\n")
        return

    # Overall Summary Table
    summary = Table(title=f"Vault Health Report: {target_dir}", show_header=True, header_style="bold cyan")
    summary.add_column("Metric", style="white")
    summary.add_column("Count", style="bold")
    summary.add_column("Status", style="bold")

    summary.add_row("Total Markdown Notes", str(report.total_files), "[green]OK[/green]")
    summary.add_row("Total [[wikilinks]]", str(report.total_links), "[green]OK[/green]")
    
    broken_style = "[green]0 (Clean)[/green]" if len(report.broken_links) == 0 else f"[red]{len(report.broken_links)} Broken[/red]"
    summary.add_row("Broken Wikilinks", str(len(report.broken_links)), broken_style)

    orphan_style = "[green]0[/green]" if len(report.orphan_pages) == 0 else f"[yellow]{len(report.orphan_pages)} Orphans[/yellow]"
    summary.add_row("Orphan Pages", str(len(report.orphan_pages)), orphan_style)

    summary.add_row("Open Questions Tracked", str(len(report.open_questions)), f"[cyan]{len(report.open_questions)} Questions[/cyan]")
    console.print(summary)

    # Broken links details
    if report.broken_links:
        broken_table = Table(title="⚠️ Broken Wikilinks Found", show_header=True, header_style="bold red")
        broken_table.add_column("Source Note", style="yellow")
        broken_table.add_column("Target Link", style="red")
        for src, tgt in report.broken_links[:10]:
            broken_table.add_row(escape(src), escape(f"[[{tgt}]]"))
        console.print(broken_table)

    # Orphan pages details
    if report.orphan_pages:
        console.print("\n[bold yellow]📌 Orphan Pages (no incoming links):[/bold yellow]")
        for orphan in report.orphan_pages[:10]:
            console.print(f"  - {escape(orphan)}")

    # Open questions
    if report.open_questions:
        console.print("\n[bold cyan]❓ Unresolved Open Questions:[/bold cyan]")
        for src, q in report.open_questions[:10]:
            console.print(f"  - [dim]{escape(src)}:[/dim] {escape(q)}")
    console.print()


def print_templates() -> None:
    """Displays standard Obsidian study note templates."""
    console.print(Panel(CONCEPT_TEMPLATE, title="📝 Obsidian Concept Note Template", border_style="green"))
    console.print(Panel(LECTURE_TEMPLATE, title="📖 Obsidian Lecture Summary Template", border_style="blue"))
    console.print(Panel(FLASHCARD_TEMPLATE, title="🗂️ Flashcard Deck Template", border_style="magenta"))


def print_config(config: AppConfig) -> None:
    """Displays current active configuration."""
    table = Table(title="Active Configuration", show_header=True, header_style="bold blue")
    table.add_column("Key", style="cyan", width=25)
    table.add_column("Value", style="green")
    
    table.add_row("LLM Model", config.llm.model)
    table.add_row("Temperature", str(config.llm.temperature))
    table.add_row("Max Tokens", str(config.llm.max_tokens))
    table.add_row("Base URL", config.llm.base_url)
    table.add_row("Max Iterations", str(config.agent.max_iterations))
    table.add_row("Data Dir (Read-Only)", config.agent.data_dir)
    table.add_row("Artifacts Dir (Write)", config.agent.artifacts_dir)
    table.add_row("Memory Enabled", str(config.memory.enabled))
    table.add_row("Memory DB", config.memory.db_path)
    table.add_row("API Key Set?", "Yes" if config.llm.api_key else "No (Set INNKUBE_API_KEY)")

    console.print(table)


def run_cli(
    config: Optional[AppConfig] = None,
    llm_client: Optional[BaseLLMClient] = None,
    tool_registry: Optional[ToolRegistry] = None
) -> None:
    """
    Main interactive REPL loop for StudyMate.
    """
    app_config = config or load_config()
    client = llm_client or OpenAICompatibleLLMClient(app_config.llm)
    context = ContextManager(system_prompt_path=app_config.agent.system_prompt_path)

    registry = tool_registry or ToolRegistry()
    if tool_registry is None and app_config.tools.filesystem.enabled:
        project_root = Path.cwd().resolve()
        workspace_root = (project_root / app_config.agent.workspace_dir).resolve()
        register_filesystem_tools(
            registry,
            base_dir=project_root,
            config=app_config.tools.filesystem,
            workspace_root=workspace_root,
        )

    engine = AgentEngine(llm_client=client, tool_registry=registry, config=app_config.agent)

    memory_store: Optional[MemoryStore] = None
    if app_config.memory.enabled:
        try:
            memory_store = MemoryStore(app_config.memory.db_path)
        except Exception as e:
            console.print(f"[yellow]Memory disabled (DB init failed): {e}[/yellow]")
            memory_store = None
    active_thread_id: Optional[str] = None

    def ensure_thread(first_text: str) -> Optional[str]:
        """Lazily creates the default new thread on first task message."""
        nonlocal active_thread_id
        if memory_store is None or active_thread_id is not None:
            return active_thread_id
        name = generate_thread_name(first_text)
        thread = memory_store.create_thread(name)
        active_thread_id = thread.id
        console.print(f"[dim]New thread: {escape(name)} ({escape(thread.id[:8])})[/dim]")
        return active_thread_id

    print_banner(app_config)

    if not app_config.llm.api_key:
        console.print(
            "\n[bold yellow]⚠️  Warning:[/bold yellow] No API key detected. "
            "Set [cyan]INNKUBE_API_KEY[/cyan] in your [cyan].env[/cyan] file to communicate with the model.\n"
        )

    while True:
        try:
            user_input = console.input("[bold cyan]student > [/bold cyan]").strip()

            if not user_input:
                continue

            # Handle slash commands
            if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
                console.print("[dim]Goodbye and happy studying! 🎓[/dim]")
                if memory_store is not None:
                    memory_store.close()
                break
            elif user_input.lower() == "/help":
                print_help()
                continue
            elif user_input.lower() == "/templates":
                print_templates()
                continue
            elif user_input.lower() == "/threads":
                if memory_store is None:
                    console.print("[yellow]Memory is disabled.[/yellow]")
                else:
                    chosen = pick_thread_interactive(memory_store, active_thread_id)
                    if chosen is not None:
                        active_thread_id = chosen.id
                        history = memory_store.get_messages(
                            chosen.id, limit=app_config.memory.max_load
                        )
                        context.load_history(history)
                        console.print(
                            f"[green]✓ Entered thread '{escape(chosen.name)}' "
                            f"({len(history)} messages restored).[/green]\n"
                        )
                        render_history(history)
                continue
            elif user_input.lower().startswith("/thread"):
                if memory_store is None:
                    console.print("[yellow]Memory is disabled.[/yellow]")
                    continue
                parts = user_input.split(maxsplit=1)
                if len(parts) < 2 or not parts[1].strip():
                    console.print("[yellow]Usage: /thread <#|id|name>[/yellow]")
                    continue
                query = parts[1].strip()
                found = None
                if query.isdigit() and int(query) >= 1:
                    # Positional pick = single paged row, no full-table scan.
                    page = memory_store.list_threads(limit=1, offset=int(query) - 1)
                    found = page[0] if page else None
                else:
                    found = memory_store.get_thread(query)
                    if found is None:
                        matches = memory_store.find_threads(query, limit=5)
                        if len(matches) == 1:
                            found = matches[0]
                        elif len(matches) > 1:
                            console.print("[yellow]Multiple matches:[/yellow]")
                            for i, m in enumerate(matches, start=1):
                                console.print(f"  {i}. {escape(m.name)} ({escape(m.id[:8])})")
                            console.print("[dim]Refine your query or pick from /threads.[/dim]")
                            continue
                if found is None:
                    console.print(f"[red]Thread not found: {escape(query)}[/red]")
                    continue
                active_thread_id = found.id
                history = memory_store.get_messages(found.id, limit=app_config.memory.max_load)
                context.load_history(history)
                console.print(
                    f"[green]✓ Entered thread '{escape(found.name)}' "
                    f"({len(history)} messages restored).[/green]\n"
                )
                render_history(history)
                continue
            elif user_input.lower().startswith("/new"):
                parts = user_input.split(maxsplit=1)
                context.clear()
                if memory_store is None:
                    console.print("[green]✓ Started fresh (memory disabled).[/green]")
                    continue
                if len(parts) > 1 and parts[1].strip():
                    thread = memory_store.create_thread(parts[1].strip())
                    active_thread_id = thread.id
                    console.print(f"[green]✓ New thread: {escape(thread.name)}[/green]")
                else:
                    active_thread_id = None
                    console.print("[green]✓ Started fresh (next message creates a thread).[/green]")
                continue
            elif user_input.lower() == "/archive-this":
                if memory_store is None:
                    console.print("[yellow]Memory is disabled.[/yellow]")
                    continue
                if active_thread_id is None:
                    console.print("[yellow]No active thread to archive.[/yellow]")
                    continue
                thread = memory_store.get_thread(active_thread_id, include_archived=True)
                name = thread.name if thread else active_thread_id[:8]
                if memory_store.archive_thread(active_thread_id):
                    console.print(f"[green]✓ Archived thread '{escape(name)}' (history kept, hidden).[/green]")
                else:
                    console.print("[yellow]Thread was already archived.[/yellow]")
                active_thread_id = None
                context.clear()
                continue
            elif user_input.lower() == "/archive-all":
                if memory_store is None:
                    console.print("[yellow]Memory is disabled.[/yellow]")
                    continue
                from rich.prompt import Confirm

                try:
                    confirmed = Confirm.ask(
                        "[red]Archive ALL threads? (history kept, hidden)[/red]",
                        default=False,
                    )
                except (KeyboardInterrupt, EOFError):
                    console.print()
                    continue
                if not confirmed:
                    console.print("[dim]Cancelled.[/dim]")
                    continue
                n = memory_store.archive_all()
                active_thread_id = None
                context.clear()
                console.print(f"[green]✓ Archived {n} thread(s) (history kept, hidden).[/green]")
                continue
            elif user_input.lower().startswith("/lint"):
                parts = user_input.split(maxsplit=1)
                target_path = parts[1] if len(parts) > 1 else app_config.agent.artifacts_dir
                run_vault_lint(target_path)
                continue
            elif user_input.lower().startswith("/grill"):
                parts = user_input.split(maxsplit=1)
                topic = parts[1] if len(parts) > 1 else "Core Concepts"
                user_input = SocraticGriller.create_initial_prompt(topic)
                console.print(f"[bold magenta]🥊 Launching Socratic Grilling Session on '{topic}'...[/bold magenta]\n")
            elif user_input.lower().startswith("/ingest"):
                parts = user_input.split(maxsplit=1)
                source_path = parts[1].strip() if len(parts) > 1 else app_config.agent.data_dir
                artifacts_path = app_config.agent.artifacts_dir
                user_input = (
                    f"Ingest the source material located at '{source_path}'. "
                    f"List all files in that directory first, then read each file, "
                    "For PDF files, use read_pdf repeatedly in page ranges until every page "
                    "has been read; one read_pdf call only reads a limited page range. "
                    "For each source file, write a lecture summary note and create individual "
                    f"concept notes for every key concept mentioned. "
                    f"Save lecture summaries as '{artifacts_path}/lecture-<slug>.md' "
                    f"and concept notes as '{artifacts_path}/concept-<slug>.md'. "
                    "Do not create the artifact root folder; write_file creates parent folders automatically. "
                    f"Cross-link concept notes to their lecture using [[wikilinks]]. "
                    "After all files are processed, provide a concise completion summary "
                    "listing the artifacts that were created. Do not finish with an empty response."
                )
                console.print(f"[bold green]📥 Ingesting source material from '{source_path}'...[/bold green]\n")

            elif user_input.lower() == "/clear":
                context.clear()
                console.print("[green]✓ Conversation history reset.[/green]")
                continue
            elif user_input.lower() == "/config":
                print_config(app_config)
                continue

            def on_tool_start(tc: ToolCall) -> None:
                console.print(
                    f"  [dim cyan]⚙ Action:[/dim cyan] [yellow]{escape(tc.name)}[/yellow] "
                    f"[dim]{escape(str(tc.arguments))}[/dim]"
                )

            ensure_thread(user_input)
            pre_len = len(context.get_messages())

            # Run ReAct Loop with animated status
            with console.status("[bold green]StudyMate is thinking...[/bold green]", spinner="dots"):
                try:
                    result = engine.run(
                        user_message=user_input,
                        context=context,
                        on_tool_start=on_tool_start
                    )
                except Exception as e:
                    console.print(f"\n[bold red]Error during agent execution:[/bold red] {e}\n")
                    continue

            if memory_store is not None and active_thread_id is not None:
                try:
                    for msg in context.get_messages()[pre_len:]:
                        memory_store.add_message(active_thread_id, msg)
                except Exception as e:
                    console.print(f"[yellow]Warning: failed to persist thread: {e}[/yellow]")

            # Render response
            console.print()
            if result.final_response:
                console.print("[bold magenta]studymate > [/bold magenta]", end="")
                console.print(Markdown(result.final_response))
            elif not result.is_success:
                console.print(f"[bold red]Agent stopped:[/bold red] {result.error_message or 'The agent did not return a response.'}")
            else:
                console.print("[bold red]The agent stopped without returning a response. Please try again.[/bold red]")
            console.print()

        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Session ended. Goodbye! 🎓[/dim]")
            break

    if memory_store is not None:
        memory_store.close()


def main() -> None:
    """CLI entrypoint invoked by `studymate` console script."""
    run_cli()


if __name__ == "__main__":
    main()
