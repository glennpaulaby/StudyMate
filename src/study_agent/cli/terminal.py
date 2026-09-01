import sys
from typing import Optional
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table

from study_agent.config.loader import load_config
from study_agent.config.settings import AppConfig
from study_agent.core.context import ContextManager
from study_agent.llm.base import BaseLLMClient
from study_agent.llm.client import OpenAICompatibleLLMClient

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
    table.add_column("Command", style="cyan", width=15)
    table.add_column("Description", style="white")
    table.add_row("/help", "Show this help table")
    table.add_row("/clear", "Reset conversation history and reload system prompt")
    table.add_row("/config", "Display current configuration parameters")
    table.add_row("/exit, /quit", "Exit StudyMate")
    console.print(table)


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
    llm_client: Optional[BaseLLMClient] = None
) -> None:
    """
    Main interactive REPL loop for StudyMate.
    """
    app_config = config or load_config()
    client = llm_client or OpenAICompatibleLLMClient(app_config.llm)
    context = ContextManager(system_prompt_path=app_config.agent.system_prompt_path)
    
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
            elif user_input.lower() == "/clear":
                context.clear()
                console.print("[green]✓ Conversation history reset.[/green]")
                continue
            elif user_input.lower() == "/config":
                print_config(app_config)
                continue
                
            # Add user message to history
            context.add_user_message(user_input)
            
            # Send to LLM with animated status
            with console.status("[bold green]StudyMate is thinking...[/bold green]", spinner="dots"):
                try:
                    response = client.chat(messages=context.get_messages())
                except Exception as e:
                    console.print(f"\n[bold red]Error communicating with LLM:[/bold red] {e}\n")
                    continue
            
            # Add assistant message to history
            context.add_assistant_message(content=response.content, tool_calls=response.tool_calls)
            
            # Render response
            console.print()
            if response.content:
                console.print(Markdown(response.content))
            else:
                console.print("[dim](No text response)[/dim]")
            console.print()
            
        except (KeyboardInterrupt, EOFError):
            console.print("\n[dim]Session ended. Goodbye! 🎓[/dim]")
            break


def main() -> None:
    """CLI entrypoint invoked by `studymate` console script."""
    run_cli(
        config=(
            
        )
    )


if __name__ == "__main__":
    main()
