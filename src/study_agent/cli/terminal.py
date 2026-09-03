import sys
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
    table.add_row("/clear", "Reset conversation history and reload system prompt")
    table.add_row("/config", "Display current configuration parameters")
    table.add_row("/exit, /quit", "Exit StudyMate")
    console.print(table)


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
        register_filesystem_tools(registry, config=app_config.tools.filesystem)

    engine = AgentEngine(llm_client=client, tool_registry=registry, config=app_config.agent)

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
                user_input = (
                    f"Ingest the source material located at '{source_path}'. "
                    f"List all files in that directory first, then read each file, "
                    f"and for each one: write a lecture summary note and create individual "
                    f"concept notes for every key concept mentioned. "
                    f"Save lecture summaries as 'workspace/artifacts/lecture-<slug>.md' "
                    f"and concept notes as 'workspace/artifacts/concept-<slug>.md'. "
                    f"Cross-link concept notes to their lecture using [[wikilinks]]."
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
            else:
                console.print("[dim](No text response)[/dim]")
            console.print()

        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Session ended. Goodbye! 🎓[/dim]")
            break


def main() -> None:
    """CLI entrypoint invoked by `studymate` console script."""
    run_cli()


if __name__ == "__main__":
    main()


