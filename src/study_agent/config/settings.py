from typing import Dict, List
from pydantic import BaseModel, Field


class LLMConfig(BaseModel):
    """Configuration for LLM inference endpoint (e.g. InnKube cluster)."""
    model: str = Field(default="qwen-agentworld-35b-a3b", description="Model identifier")
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, description="Sampling temperature")
    max_tokens: int = Field(default=2048, ge=1, description="Max tokens per response")
    base_url: str = Field(default="https://llms.innkube.fim.uni-passau.de", description="OpenAI-compatible endpoint")
    timeout: float = Field(default=60.0, ge=1.0, description="Request timeout in seconds")
    api_key: str = Field(default="", description="API key (injected from environment)")
    embedding_model: str = Field(default="octen-embedding-8b", description="Embedding model for semantic fact recall")
    reranker_model: str = Field(default="qwen3-reranker-4b", description="Reranker model for hybrid fact ordering")


class AgentConfig(BaseModel):
    """Configuration for the StudyMate agent core behavior and paths."""
    name: str = Field(default="StudyMate", description="Agent persona name")
    max_iterations: int = Field(default=50, ge=1, le=50, description="Max ReAct loop iterations")
    system_prompt_path: str = Field(default="config/soul.md", description="Path to agent persona directives")
    workspace_dir: str = Field(default="./workspace", description="Root sandbox directory")
    data_dir: str = Field(default="./workspace/data", description="Read-only source documents directory")
    artifacts_dir: str = Field(default="./workspace/artifacts", description="Read/write study artifacts directory")


class FilesystemToolConfig(BaseModel):
    """Security boundaries and rules for filesystem access."""
    enabled: bool = Field(default=True, description="Whether filesystem tools are registered")
    allow_delete: bool = Field(default=False, description="Strict deletion prohibition invariant")
    allowed_read_paths: List[str] = Field(default_factory=lambda: ["./workspace"], description="Paths permitted for reading")
    allowed_write_paths: List[str] = Field(default_factory=lambda: ["./workspace/artifacts"], description="Paths permitted for writing")


class ToolsConfig(BaseModel):
    """Container for all tool category configurations."""
    filesystem: FilesystemToolConfig = Field(default_factory=FilesystemToolConfig)


class MemoryConfig(BaseModel):
    """Persistent chat-thread memory (SQLite episodic store)."""
    enabled: bool = Field(default=True, description="Whether thread persistence is active")
    db_path: str = Field(
        default="./workspace/artifacts/memory/memory.db",
        description="SQLite file (must stay inside allowed_write jail)",
    )
    max_load: int = Field(default=50, ge=1, le=200, description="Max past messages reloaded on thread enter")
    max_inject: int = Field(default=5, ge=0, le=20, description="Top-N recent facts auto-injected at session start")


class CompactionConfig(BaseModel):
    """Context-window compaction (Claude/OpenCode-style, same-thread checkpoint)."""
    enabled: bool = Field(default=True, description="Auto-compact when the window fills")
    threshold: float = Field(default=0.8, ge=0.1, le=0.95, description="Fraction of window triggering compaction")
    buffer: int = Field(default=20000, ge=0, description="Reserved headroom so the summary call itself fits")
    keep_recent_messages: int = Field(default=10, ge=1, description="Recent tail kept verbatim on compact")
    summary_max_tokens: int = Field(default=1000, ge=100, description="Cap for the generated summary")
    max_consecutive_failures: int = Field(default=2, ge=1, description="Circuit breaker: stop after N failed compactions")
    default_context_window: int = Field(default=128000, ge=8000, description="Conservative window for unknown models")
    context_windows: Dict[str, int] = Field(
        default_factory=dict, description="Explicit per-model context windows (override)"
    )


class ObservabilityConfig(BaseModel):
    """Logging and Prometheus telemetry settings."""
    log_level: str = Field(default="INFO", description="Logging verbosity")
    metrics_enabled: bool = Field(default=False, description="Prometheus metrics exporter switch")
    metrics_port: int = Field(default=8000, ge=1024, le=65535, description="Prometheus scrape port")


class AppConfig(BaseModel):
    """Root application configuration."""
    llm: LLMConfig = Field(default_factory=LLMConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
