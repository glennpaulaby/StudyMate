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
from study_agent.study.griller import SocraticGriller
from study_agent.study.linter import VaultLintReport, VaultLinter
from study_agent.study.templates import CONCEPT_TEMPLATE, FLASHCARD_TEMPLATE, LECTURE_TEMPLATE
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools
from study_agent.tools.mcp import register_mcp_tools
from study_agent.tools.mcp.services import WebResearchService

console = Console()

STUDYMATE_STYLE = Style.from_dict({
    "prompt": "bold ansicyan",
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
        ("/clear", "Reset in-memory conversation history"),
        ("/config", "View active configuration settings"),
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
    table.add_row("/clear", "Reset conversation history and reload system prompt")
    table.add_row("/config", "Display current configuration parameters")
    table.add_row("/exit, /quit", "Exit StudyMate")
    console.print(table)


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
                search_service=WebResearchService(app_config.tools.mcp.search_endpoint),
                max_results=app_config.tools.mcp.max_results,
            )

    engine = AgentEngine(llm_client=client, tool_registry=registry, config=app_config.agent)

    session = None
    try:
        # Check if running in a real interactive terminal
        if sys.stdin and hasattr(sys.stdin, "isatty") and sys.stdin.isatty():
            completer = StudyMateCompleter(registry)
            session = PromptSession(completer=completer, style=STUDYMATE_STYLE)
    except Exception:
        session = None

    print_banner(app_config)

    if not app_config.llm.api_key:
        console.print(
            "\n[bold yellow]⚠️  Warning:[/bold yellow] No API key detected. "
            "Set [cyan]INNKUBE_API_KEY[/cyan] in your [cyan].env[/cyan] file to communicate with the model.\n"
        )

    while True:
        try:
            # If console.input is mocked (e.g. by unit tests) or session not available, use console.input
            if session is not None and getattr(console.input, "_mock_wraps", None) is None and getattr(console.input, "side_effect", None) is None:
                try:
                    raw_input = session.prompt([("class:prompt", "student > ")], complete_while_typing=True)
                except Exception:
                    raw_input = console.input("[bold cyan]student > [/bold cyan]")
            else:
                raw_input = console.input("[bold cyan]student > [/bold cyan]")

            user_input = raw_input.strip()

            if not user_input:
                continue

            # Handle slash commands
            if user_input.lower() in ("/exit", "/quit", "exit", "quit"):
                console.print("[dim]Goodbye and happy studying! 🎓[/dim]")
                break
            elif user_input.lower() == "/help":
                print_help()
                continue
            elif user_input.lower() == "/templates":
                print_templates()
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
            elif user_input.lower() == "/config":
                print_config(app_config)
                continue

            def on_tool_start(tc: ToolCall) -> None:
                console.print(
                    f"  [dim cyan]⚙ Action:[/dim cyan] [yellow]{escape(tc.name)}[/yellow] "
                    f"[dim]{escape(str(tc.arguments))}[/dim]"
                )

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

            # Render response
            console.print()
            if result.final_response:
                console.print(Markdown(result.final_response))
            elif not result.is_success:
                console.print(f"[bold red]Agent stopped:[/bold red] {result.error_message or 'The agent did not return a response.'}")
            else:
                console.print("[bold red]The agent stopped without returning a response. Please try again.[/bold red]")
            console.print()

        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Session ended. Goodbye! 🎓[/dim]")
            break


def main() -> None:
    """CLI entrypoint invoked by `studymate` console script."""
    run_cli()


if __name__ == "__main__":
    main()
