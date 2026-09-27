"""
Configuration Loader Module for StudyMate.

Handles multi-tier configuration loading and merging:
1. Environment variables (e.g., INNKUBE_API_KEY, STUDYMATE_METRICS_ENABLED).
2. .env file in project root.
3. YAML configuration file (config/config.yaml).
4. Pydantic default schema values.
"""

import os
import yaml
from pathlib import Path
from typing import Optional
from study_agent.config.settings import AppConfig



def _load_env_file(env_path: Path) -> None:
    """Simple parser to load key-value pairs from .env without extra dependencies."""
    if not env_path.exists():
        return
    
    with open(env_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key = key.strip()
            val = val.strip().strip("\"'")
            if key and key not in os.environ:
                os.environ[key] = val


def load_config(
    config_path: Optional[str | Path] = None,
    env_path: Optional[str | Path] = None
) -> AppConfig:
    """
    Loads application configuration from YAML and environment variables.
    
    Priority:
    1. System Environment Variables (e.g. INNKUBE_API_KEY)
    2. .env file in project root
    3. YAML config values (config/config.yaml)
    4. Pydantic default fallback values
    """
    # 1. Resolve paths - use __file__ to find project root relative to this module
    base_dir = Path(__file__).parent.parent.parent.parent  # src/study_agent/config/loader.py -> project root
    resolved_env = Path(env_path) if env_path else base_dir / ".env"
    resolved_config = Path(config_path) if config_path else base_dir / "config" / "config.yaml"
    
    # 2. Load .env into os.environ
    _load_env_file(resolved_env)
    
    # 3. Read YAML if present
    yaml_data = {}
    if resolved_config.exists():
        with open(resolved_config, "r", encoding="utf-8") as f:
            yaml_data = yaml.safe_load(f) or {}
    
    # 4. Construct AppConfig
    config = AppConfig(**yaml_data)
    
    # 5. Inject API Key securely from environment if not explicitly set
    # One key covers chat, embeddings, and rerank (granted per model server-side).
    if not config.llm.api_key:
        config.llm.api_key = os.getenv("INNKUBE_API_KEY", "") or os.getenv("OPENAI_API_KEY", "")

    # 6. Observability overrides (env > YAML > defaults). Never commit secrets.
    _obs = config.observability
    if os.getenv("STUDYMATE_METRICS_ENABLED", "") != "":
        _obs.metrics_enabled = os.getenv("STUDYMATE_METRICS_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    if os.getenv("STUDYMATE_METRICS_PORT", "") != "":
        try:
            _obs.metrics_port = int(os.getenv("STUDYMATE_METRICS_PORT", ""))
        except ValueError:
            pass
    if os.getenv("STUDYMATE_METRICS_HOST", "") != "":
        _obs.metrics_host = os.getenv("STUDYMATE_METRICS_HOST", "").strip()
    if os.getenv("STUDYMATE_TRACE_MAX_CHARS", "") != "":
        try:
            _obs.trace_max_chars = int(os.getenv("STUDYMATE_TRACE_MAX_CHARS", ""))
        except ValueError:
            pass
    if os.getenv("LANGFUSE_ENABLED", "") != "":
        _obs.langfuse_enabled = os.getenv("LANGFUSE_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    if os.getenv("LANGFUSE_HOST", "") != "":
        _obs.langfuse_host = os.getenv("LANGFUSE_HOST", "").strip()
    if os.getenv("STUDYMATE_HEALTH_ENABLED", "") != "":
        _obs.health_enabled = os.getenv("STUDYMATE_HEALTH_ENABLED", "").strip().lower() in ("1", "true", "yes", "on")
    if os.getenv("STUDYMATE_HEALTH_INTERVAL_SECONDS", "") != "":
        try:
            _obs.health_interval_seconds = float(os.getenv("STUDYMATE_HEALTH_INTERVAL_SECONDS", ""))
        except ValueError:
            pass
    # Secrets: env-only, never from YAML (mirrors the LLM API-key pattern).
    if os.getenv("LANGFUSE_PUBLIC_KEY", "") != "":
        _obs.langfuse_public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    if os.getenv("LANGFUSE_SECRET_KEY", "") != "":
        _obs.langfuse_secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()

    return config
