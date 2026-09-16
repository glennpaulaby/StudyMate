import logging
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

from prompt_toolkit import PromptSession
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.formatted_text import HTML
from prompt_toolkit.styles import Style

from study_agent.config.loader import load_config
from study_agent.config.settings import AppConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.base import BaseLLMClient
from study_agent.llm.client import OpenAICompatibleLLMClient
from study_agent.core.compaction import estimate_tokens, maybe_compact, SUMMARY_PREFIX
from study_agent.memory.embeddings import Embedder, InnKubeEmbedder
from study_agent.memory.rerank import InnKubeReranker, Reranker
from study_agent.memory.store import MemoryStore
from study_agent.memory.threads import generate_thread_name, resolve_thread
from study_agent.memory.tools import register_memory_tools
from study_agent.study.griller import SocraticGriller
from study_agent.study.procedures import DRAFT, format_procedures_block, match_procedures
from study_agent.study.linter import VaultLintReport, VaultLinter
from study_agent.study.templates import CONCEPT_TEMPLATE, FLASHCARD_TEMPLATE, LECTURE_TEMPLATE
from study_agent.tools.base import ToolRegistry
from study_agent.security.policy import PermissionEvent, policy_from_config
from study_agent.tools.filesystem import register_filesystem_tools
from study_agent.tools.mcp import register_mcp_tools
from study_agent.tools.mcp.external import ExternalMCPService
from study_agent.tools.delegation import register_delegation_tools

console = Console()
logger = logging.getLogger(__name__)

STUDYMATE_STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
    # Kill prompt_toolkit's default `reverse` on the toolbar window,
    # otherwise the whole row paints as a full-width bar.
    "bottom-toolbar": "noreverse",
    "toolbar.ok": "#6b7280",
    "toolbar.warn": "#e5c07b",
    "toolbar.bad": "#e06c75",
    "completion-menu.completion": "bg:#1e1e2e #cdd6f4",
    "completion-menu.completion.current": "bold bg:#313244 #f9e2af",
    "completion-menu.meta.completion": "bg:#181825 #a6adc8",
    "completion-menu.meta.completion.current": "bold bg:#313244 #89b4fa",
    "scrollbar.background": "bg:#11111b",
    "scrollbar.button": "bg:#585b70",
})


class StudyMateCompleter(Completer):
    """
    Auto-completer providing dropdown completions for slash commands and registered tools.
    """

    SLASH_COMMANDS = [
        ("/help", "Show available CLI commands"),
        ("/ingest", "Ingest study materials into Obsidian notes"),
        ("/lint", "Run vault health check and link linter"),
        ("/grill", "Launch Socratic active-recall sparring session"),
        ("/templates", "Display Obsidian markdown templates"),
        ("/threads", "Browse and pick previous chat threads"),
        ("/thread", "Enter or continue a specific chat thread"),
        ("/new", "Start a new conversation thread"),
        ("/archive-this", "Archive the current active chat thread"),
        ("/archive-all", "Archive all chat threads"),
        ("/compact", "Summarize history into a checkpoint now"),
        ("/clear", "Reset in-memory conversation history"),
        ("/config", "View active configuration settings"),
        ("/permissions", "Show active tool permission policy"),
        ("/exit", "Exit StudyMate"),
        ("/quit", "Exit StudyMate"),
    ]

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def get_completions(self, document, complete_event):
        text_before_cursor = document.text_before_cursor
        word_before_cursor = document.get_word_before_cursor(WORD=True)

        # Match slash commands if line starts with '/'
        if text_before_cursor.startswith("/"):
            query = text_before_cursor.lower()
            for cmd, desc in self.SLASH_COMMANDS:
                if cmd.lower().startswith(query):
                    yield Completion(
                        text=cmd,
                        start_position=-len(text_before_cursor),
                        display=cmd,
                        display_meta=desc,
                    )
            return

        # Match tools based on the current word
        if word_before_cursor:
            query = word_before_cursor.lower()
            for tool_name, tool in self.registry.tools.items():
                if tool_name.lower().startswith(query) or query in tool_name.lower():
                    # Generate a concise description
                    short_desc = tool.description.split(".")[0].strip() if tool.description else "Tool"
                    if len(short_desc) > 65:
                        short_desc = short_desc[:62] + "..."
                    yield Completion(
                        text=tool_name,
                        start_position=-len(word_before_cursor),
                        display=tool_name,
                        display_meta=short_desc,
                    )


def _human_tokens(n: int) -> str:
    """Compact token counts: 12400 -> '12.4k', 900 -> '900'."""
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def context_usage_bar(used: int, window: int, threshold: float, width: int = 20) -> str:
    """Pure usage bar: green below threshold, yellow past it, red past 95%.

    Returns Rich markup like `[dim][green]██████░░░░[/green] 12.4k/262k (5%)[/dim]`.
    """
    window = max(1, window)
    frac = min(1.0, max(0.0, used / window))
    filled = int(round(frac * width))
    if frac >= 0.95:
        color = "red"
    elif used >= threshold * window:
        color = "yellow"
    else:
        color = "green"
    bar = "█" * filled + "░" * (width - filled)
    return (
        f"[dim][{color}]{bar}[/{color}] {_human_tokens(used)}/{_human_tokens(window)} "
        f"({frac * 100:.0f}%)[/dim]"
    )


def print_context_usage(engine: AgentEngine, context: ContextManager) -> None:
    """Prints the live context footprint (fallback when no toolbar; never raises)."""
    try:
        from study_agent.core.compaction import compaction_threshold_tokens

        schemas = engine.tool_registry.list_schemas() if engine.tool_registry else None
        used = estimate_tokens(context.get_messages(), schemas)
        window = engine._window()
        fire_line = compaction_threshold_tokens(window, engine.compaction)
        line = context_usage_bar(used, window, engine.compaction.threshold)
        if used >= fire_line:
            line += " [yellow]— approaching compact threshold[/yellow]"
        console.print(line)
    except Exception:
        pass


def format_toolbar_text(
    model: str, thread_name: Optional[str], used: int, window: int, threshold: float
) -> str:
    """Pure status-line text: model | thread | context footprint (no markup)."""
    window = max(1, window)
    frac = min(1.0, max(0.0, used / window))
    thread = thread_name or "new thread"
    return f"{model} | {thread} | ctx {_human_tokens(used)}/{_human_tokens(window)} ({frac * 100:.0f}%)"


def toolbar_level(used: int, window: int, threshold: float) -> str:
    """Band for toolbar styling: ok < threshold <= warn < 95% <= bad."""
    window = max(1, window)
    frac = min(1.0, max(0.0, used / window))
    if frac >= 0.95:
        return "bad"
    if used >= threshold * window:
        return "warn"
    return "ok"


def create_input_session(toolbar_fn, completer=None) -> Optional[object]:
    """prompt_toolkit session: completion + subtle status toolbar, one style.

    The toolbar uses foreground-only colors (no backgrounds) so it reads as a
    dim status line, never a full-width bar. Returns None outside a TTY or
    when prompt_toolkit is unavailable — callers fall back to console.input
    (plus footer-line usage display).
    """
    import sys as _sys

    if not bool(getattr(_sys.stdin, "isatty", lambda: False)()):
        return None
    try:
        from prompt_toolkit import PromptSession as _Session
        from prompt_toolkit.formatted_text import FormattedText as _FT

        def _toolbar():
            try:
                text, level = toolbar_fn()
            except Exception:
                return ""
            if not text:
                return ""
            return _FT([(f"class:toolbar.{level}", f" {text} ")])

        return _Session(completer=completer, style=STUDYMATE_STYLE, bottom_toolbar=_toolbar)
    except (ImportError, Exception):
        return None


def read_student_input(session) -> str:
    """Reads one REPL line via the session (styled) or Rich fallback."""
    if (
        session is not None
        and getattr(console.input, "_mock_wraps", None) is None
        and getattr(console.input, "side_effect", None) is None
    ):
        try:
            return session.prompt([("class:prompt", "student > ")], complete_while_typing=True).strip()
        except (KeyboardInterrupt, EOFError):
            raise
        except Exception:
            pass
    return console.input("[bold cyan]student > [/bold cyan]").strip()



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
    table.add_row(escape("/lint [path]"), "Lint the vault and automatically repair broken links and formatting issues")
    table.add_row("/grill <topic>", "Launch an interactive Socratic grilling sparring session")
    table.add_row("/templates", "Display standard Obsidian markdown templates")
    table.add_row("/threads", "List previous chat threads")
    table.add_row(escape("/thread <id|name>"), "Enter/continue a previous thread")
    table.add_row(escape("/new [name]"), "Start a new thread (default on first message)")
    table.add_row(escape("/archive-this"), "Archive the current thread (tombstone, no delete)")
    table.add_row(escape("/archive-all"), "Archive ALL threads (asks for confirmation)")
    table.add_row("/compact", "Summarize history into a checkpoint now (same thread)")
    table.add_row("/clear", "Reset in-memory history (thread history stays saved)")
    table.add_row("/config", "Display current configuration parameters")
    table.add_row("/permissions", "Show the active tool permission policy and session allowances")
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
        if (content or "").startswith(SUMMARY_PREFIX):
            console.print("[dim]compacted context > [/dim]" + escape(content or ""))
        else:
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


def run_vault_lint(
    target_dir: str,
    engine: Optional[AgentEngine] = None,
    context: Optional[ContextManager] = None,
) -> None:
    """Lint a vault and ask the agent to repair detected integrity errors."""
    linter = VaultLinter(target_dir)
    with console.status("[bold green]Linting vault knowledge graph...[/bold green]"):
        report = linter.lint()

    if (report.broken_links or report.metadata_issues) and engine and context:
        repair_prompt = _build_lint_repair_prompt(target_dir, report)
        console.print(
            "[bold yellow]⚙ Lint errors found. StudyMate is attempting automatic repairs...[/bold yellow]"
        )
        with console.status("[bold green]Repairing vault issues...[/bold green]"):
            repair_result = engine.run(repair_prompt, context)
        if repair_result.is_success:
            console.print("[green]✓ Automatic repair pass completed.[/green]")
        else:
            console.print(
                f"[bold red]Automatic repair stopped:[/bold red] "
                f"{repair_result.error_message or 'No repair summary was produced.'}"
            )
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

    metadata_style = "[green]0 (Clean)[/green]" if len(report.metadata_issues) == 0 else f"[red]{len(report.metadata_issues)} Issues[/red]"
    summary.add_row("Formatting / Metadata Issues", str(len(report.metadata_issues)), metadata_style)

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

    if report.metadata_issues:
        metadata_table = Table(title="⚠️ Formatting / Metadata Issues", show_header=True, header_style="bold red")
        metadata_table.add_column("Note", style="yellow")
        metadata_table.add_column("Issue", style="red")
        for source, issue in report.metadata_issues[:10]:
            metadata_table.add_row(escape(source), escape(issue))
        console.print(metadata_table)

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


def _build_lint_repair_prompt(target_dir: str, report: VaultLintReport) -> str:
    """Build a constrained repair request from a lint report."""
    issues = []
    for source, target in report.broken_links:
        issues.append(f"- Broken wikilink in '{source}': [[{target}]]")
    for source, description in report.metadata_issues:
        issues.append(f"- Formatting/frontmatter issue in '{source}': {description}")

    return (
        f"Repair the detected integrity errors in the Markdown vault at '{target_dir}'. "
        "Read each affected note before editing. Fix only the listed broken wikilinks "
        "and formatting/frontmatter issues. For broken wikilinks, link to an existing "
        "note when the intended target is unambiguous; otherwise remove only the broken "
        "link syntax and preserve the surrounding meaning. For frontmatter issues, "
        "preserve existing metadata and add only the required missing fields using the "
        "vault's established note format. Use edit_file for precise changes and write_file "
        "only when necessary. Do not delete files, invent notes, rewrite unrelated content, "
        "or modify orphan status. After editing, report the files changed.\n\n"
        "Detected issues:\n"
        + "\n".join(issues)
    )


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
    table.add_row("Context Window", f"{config.compaction.context_windows.get(config.llm.model, config.compaction.default_context_window)} (model: {config.llm.model})")
    table.add_row("Compact Threshold", f"{config.compaction.threshold} (~{int(config.compaction.context_windows.get(config.llm.model, config.compaction.default_context_window) * config.compaction.threshold)} tokens)")
    table.add_row("API Key Set?", "Yes" if config.llm.api_key else "No (Set INNKUBE_API_KEY)")

    console.print(table)


def make_confirmation_handler(registry: ToolRegistry, spinner_ref: Optional[dict] = None):
    """
    Builds the interactive yes/no/always confirmation prompt used by the
    permission gate when a tool call requires user confirmation.
    Fails closed (returns False) on non-interactive stdin or interrupted input.

    ``spinner_ref`` is an optional ``{"live": RichLive}`` holder: the handler
    runs inside the agent loop's ``console.status()`` spinner, whose Live
    refresh thread repaints the bottom of the terminal and ERASES the
    confirmation prompt line, leaving an invisible prompt (agent looks frozen).
    When given, the spinner is paused for the duration of the prompt.
    """

    def handler(name: str, arguments: dict, rule_desc: str) -> bool:
        if not (sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
            return False
        live = spinner_ref.get("live") if spinner_ref else None
        paused = live is not None and live.is_started
        if paused:
            live.stop()
        try:
            args_preview = escape(str(arguments))
            if len(args_preview) > 200:
                args_preview = args_preview[:200] + "…"
            console.print(Panel(
                f"[bold]{escape(name)}[/bold] requires confirmation [dim]({escape(rule_desc)})[/dim]\n"
                f"[dim]{args_preview}[/dim]",
                title="🔐 Permission check",
                border_style="yellow",
            ))
            try:
                answer = console.input(
                    "[bold yellow]Allow this tool call? [green]y[/green]/"
                    "[red]n[/red]/[cyan]always[/cyan] [/bold yellow]"
                ).strip().lower()
            except (KeyboardInterrupt, EOFError):
                console.print()
                return False
        finally:
            if paused:
                live.start()
        if answer in ("a", "always"):
            if registry.policy is not None:
                registry.policy.allow_for_session(name)
                console.print(f"[cyan]✓ '{escape(name)}' allowed for this session.[/cyan]")
            return True
        return answer in ("y", "yes")

    return handler


def print_permissions(registry: ToolRegistry) -> None:
    """Displays the active permission policy and any session-level allowances."""
    if registry.policy is None:
        console.print("[yellow]Permission management is disabled.[/yellow]")
        return
    table = Table(title="Permission Policy (first match wins)", show_header=True, header_style="bold yellow")
    table.add_column("#", style="dim", width=4)
    table.add_column("Tools", style="cyan")
    table.add_column("Decision", style="white")
    table.add_column("Tier", style="magenta")
    for i, rule in enumerate(registry.policy.rules, start=1):
        table.add_row(
            str(i), ", ".join(rule.tools), rule.decision.value, rule.tier,
        )
    console.print(table)
    console.print(
        f"[dim]Default (no rule matched):[/dim] [bold]{registry.policy.default_decision.value}[/bold]"
    )
    if registry.policy.session_allowances:
        console.print(
            "[dim]Session allowances:[/dim] "
            + ", ".join(registry.policy.session_allowances)
        )
    if registry.confirmation_handler is None:
        console.print("[yellow]Non-interactive mode: confirmations are denied (fail-closed).[/yellow]")


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

    registry = tool_registry or ToolRegistry(
        policy=policy_from_config(app_config.permissions),
    )
    # Shared holder so the confirmation handler can pause the thinking spinner
    # (its Live repaint erases the y/n prompt line; see make_confirmation_handler).
    spinner_ref: dict = {}
    if tool_registry is None and registry.policy is not None:
        registry.confirmation_handler = make_confirmation_handler(registry, spinner_ref)

        def _log_permission_event(event: PermissionEvent) -> None:
            logger.info(
                f"permission: tool={event.tool} decision={event.decision.value} "
                f"executed={event.executed} rule={event.source}"
            )

        registry.on_permission_decision.append(_log_permission_event)
    project_root = Path.cwd().resolve()
    if tool_registry is None and app_config.tools.filesystem.enabled:
        workspace_root = (project_root / app_config.agent.workspace_dir).resolve()
        register_filesystem_tools(
            registry,
            base_dir=project_root,
            config=app_config.tools.filesystem,
            workspace_root=workspace_root,
        )
    if tool_registry is None and app_config.tools.mcp.enabled:
        configured_vault = app_config.tools.mcp.vault_path
        vault_path = Path(configured_vault)
        if not vault_path.is_absolute():
            vault_path = (project_root / vault_path).resolve()
        if vault_path.is_dir():
            register_mcp_tools(
                registry,
                vault_path=vault_path,
                external_service=ExternalMCPService(
                    command=app_config.tools.mcp.search_command,
                    args=app_config.tools.mcp.search_args,
                ),
            )

    if "delegate_task" not in registry.tools:
        register_delegation_tools(registry, client)

    engine = AgentEngine(
        llm_client=client,
        tool_registry=registry,
        config=app_config.agent,
        compaction=app_config.compaction,
        llm_base_url=app_config.llm.base_url,
    )

    memory_store: Optional[MemoryStore] = None
    if app_config.memory.enabled:
        try:
            memory_store = MemoryStore(app_config.memory.db_path)
        except Exception as e:
            console.print(f"[yellow]Memory disabled (DB init failed): {e}[/yellow]")
            memory_store = None
    embedder: Optional[Embedder] = None
    reranker: Optional[Reranker] = None
    if memory_store is not None and app_config.llm.api_key:
        try:
            embedder = InnKubeEmbedder(
                api_key=app_config.llm.api_key,
                base_url=app_config.llm.base_url,
                model=app_config.llm.embedding_model,
                timeout=app_config.llm.timeout,
            )
            reranker = InnKubeReranker(
                api_key=app_config.llm.api_key,
                base_url=app_config.llm.base_url,
                model=app_config.llm.reranker_model,
                timeout=app_config.llm.timeout,
            )
        except Exception as e:
            console.print(f"[yellow]Semantic recall disabled (embedder init failed): {e}[/yellow]")
            embedder = None
            reranker = None
    register_memory_tools(registry, memory_store, embedder, reranker)
    facts_note_base = ""
    if memory_store is not None and app_config.memory.max_inject > 0:
        recent_facts = memory_store.recall_facts("", limit=app_config.memory.max_inject)
        if recent_facts:
            lines = ["Relevant long-term memory (persisted facts, most recent first):"]
            lines += [f"- [{f.topic}] {f.fact}" for f in recent_facts]
            lines.append("Use memory_recall to search all facts just-in-time when personalizing.")
            facts_note_base = "\n".join(lines)
            context.inject_system_note(facts_note_base)
    procedures_dir = Path(app_config.agent.artifacts_dir) / "procedures"
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

    def _toolbar_data():
        """Live (text, level) for the sticky status toolbar; empty text hides it."""
        schemas = engine.tool_registry.list_schemas() if engine.tool_registry else None
        used = estimate_tokens(context.get_messages(), schemas)
        window = engine._window()
        thread_name = None
        if memory_store is not None and active_thread_id:
            thread = memory_store.get_thread(active_thread_id, include_archived=True)
            thread_name = thread.name if thread else None
        text = format_toolbar_text(
            app_config.llm.model, thread_name, used, window, engine.compaction.threshold
        )
        return text, toolbar_level(used, window, engine.compaction.threshold)

    completer = None
    try:
        # Check if running in a real interactive terminal
        if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            completer = StudyMateCompleter(registry)
    except Exception:
        completer = None

    input_session = create_input_session(_toolbar_data, completer=completer)
    use_toolbar = input_session is not None

    print_banner(app_config)

    if not app_config.llm.api_key:
        console.print(
            "\n[bold yellow]⚠️  Warning:[/bold yellow] No API key detected. "
            "Set [cyan]INNKUBE_API_KEY[/cyan] in your [cyan].env[/cyan] file to communicate with the model.\n"
        )

    while True:
        try:
            user_input = read_student_input(input_session)

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
                        if not use_toolbar:
                            print_context_usage(engine, context)
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
                if not use_toolbar:
                    print_context_usage(engine, context)
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
                run_vault_lint(target_path, engine=engine, context=context)
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
            elif user_input.lower() == "/compact":
                before = estimate_tokens(context.get_messages())
                if len(context.get_messages()) <= 2:
                    console.print("[yellow]Nothing worth compacting yet.[/yellow]")
                    continue
                with console.status("[bold green]Compacting conversation...[/bold green]", spinner="dots"):
                    compacted, _, after = maybe_compact(
                        context.get_messages(),
                        client,
                        app_config.compaction,
                        force=True,
                        state=engine.compaction_state,
                    )
                if not compacted:
                    console.print("[yellow]Compaction did not run (breaker open or summarizer failed).[/yellow]")
                    continue
                if memory_store is not None and active_thread_id is not None:
                    try:
                        memory_store.add_message(active_thread_id, context.get_messages()[1])
                    except Exception as e:
                        console.print(f"[yellow]Warning: summary not persisted: {e}[/yellow]")
                console.print(f"[green]✓ Compacted: ~{before} → ~{after} tokens (est.). Same thread, history saved.[/green]")
                continue
            elif user_input.lower() == "/permissions":
                print_permissions(registry)
                continue
            elif user_input.lower() == "/config":
                print_config(app_config)
                continue

            def on_tool_start(tc: ToolCall) -> None:
                console.print(
                    f"  [dim cyan]⚙ Action:[/dim cyan] [yellow]{escape(tc.name)}[/yellow] "
                    f"[dim]{escape(str(tc.arguments))}[/dim]"
                )

            def on_tool_end(tc: ToolCall, observation: str) -> None:
                if observation.startswith("Error:"):
                    console.print(
                        f"  [bold red]✗ Failed:[/bold red] [yellow]{escape(tc.name)}[/yellow]"
                    )
                else:
                    console.print(
                        f"  [dim green]✓ Completed:[/dim green] [yellow]{escape(tc.name)}[/yellow]"
                    )

            # Procedures: approved routines merge into the system note (template-safe,
            # idempotent replace); drafts are surfaced, never silently followed.
            approved = match_procedures(procedures_dir, user_input)
            if approved:
                note = facts_note_base
                note += ("\n\n" if note else "") + format_procedures_block(approved)
                context.inject_system_note(note)
            elif facts_note_base:
                context.inject_system_note(facts_note_base)
            else:
                context.reset_injected_note()
            drafts = match_procedures(procedures_dir, user_input, statuses=(DRAFT,))
            if drafts:
                names = ", ".join(f"'{escape(d.name)}'" for d in drafts)
                console.print(
                    f"[dim]💡 Draft routine(s) match ({names}) — say the word and I'll follow "
                    f"it, or approve it by flipping status to approved.[/dim]"
                )

            ensure_thread(user_input)
            pre_len = len(context.get_messages())

            # Run ReAct Loop with animated status
            thinking = console.status("[bold green]StudyMate is thinking...[/bold green]", spinner="dots")
            # The inner rich.live.Live (Status itself lacks is_started); the
            # confirmation handler pauses/resumes it so the y/n line stays visible.
            spinner_ref["live"] = thinking._live
            try:
                with thinking:
                    try:
                        result = engine.run(
                            user_message=user_input,
                            context=context,
                            on_tool_start=on_tool_start,
                            on_tool_end=on_tool_end,
                        )
                    except Exception as e:
                        console.print(f"\n[bold red]Error during agent execution:[/bold red] {e}\n")
                        continue
            finally:
                spinner_ref.pop("live", None)

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
            if not use_toolbar:
                print_context_usage(engine, context)
            console.print()

        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Session ended. Goodbye! 🎓[/dim]")
            break

    if memory_store is not None:
        memory_store.close()


def launch_in_new_window() -> bool:
    """Spawns StudyMate in a dedicated, clean terminal window on supported platforms."""
    import os
    import shutil
    import subprocess

    cwd = str(Path.cwd().resolve())
    py_exe = sys.executable

    # Check if Windows Terminal (wt.exe) is available for a modern UI window
    wt_path = shutil.which("wt.exe") or shutil.which("wt")
    if sys.platform == "win32":
        env = dict(os.environ)
        env["STUDYMATE_SPAWNED"] = "1"

        if wt_path:
            cmd = [
                wt_path,
                "-w", "new",
                "--title", "StudyMate 🎓",
                "-d", cwd,
                py_exe, "-m", "study_agent.cli.terminal", "--inline"
            ]
            try:
                subprocess.Popen(cmd, env=env)
                return True
            except Exception:
                pass

        # Fallback to standard Windows cmd.exe start
        try:
            cmd = f'start "StudyMate 🎓" /D "{cwd}" "{py_exe}" -m study_agent.cli.terminal --inline'
            subprocess.Popen(cmd, shell=True, env=env)
            return True
        except Exception:
            return False

    return False


def main() -> None:
    """CLI entrypoint invoked by `studymate` console script."""
    import os

    # If --inline or already spawned in a dedicated window or non-interactive/testing, run directly
    if "--inline" in sys.argv:
        sys.argv.remove("--inline")
        run_cli()
        return

    if os.getenv("STUDYMATE_SPAWNED") == "1" or not (sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty()):
        run_cli()
        return

    # Attempt to pop up a dedicated window
    if launch_in_new_window():
        console.print("[bold green]🎓 Launched StudyMate in a new window![/bold green]")
        console.print("[dim]You can close this terminal or keep working here.[/dim]")
    else:
        run_cli()


if __name__ == "__main__":
    main()
