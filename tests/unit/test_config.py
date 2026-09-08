import os
from pathlib import Path
from study_agent.config.loader import load_config
from study_agent.config.settings import AppConfig


def test_default_config_loading():
    """Verify that load_config returns valid default AppConfig even if no files exist."""
    config = load_config(config_path="non_existent_config.yaml", env_path="non_existent.env")
    assert isinstance(config, AppConfig)
    assert config.agent.name == "StudyMate"
    assert config.llm.model == "qwen-agentworld-35b-a3b"
    assert config.llm.temperature == 0.2
    assert config.agent.max_iterations == 50
    assert config.tools.filesystem.allow_delete is False


def test_yaml_config_loading(tmp_path: Path):
    """Verify loading from a custom YAML file."""
    custom_yaml = tmp_path / "custom.yaml"
    custom_yaml.write_text(
        "llm:\n  model: gemma4-31b-it\n  temperature: 0.7\nagent:\n  name: CustomMate\n",
        encoding="utf-8"
    )
    
    config = load_config(config_path=custom_yaml, env_path="non_existent.env")
    assert config.llm.model == "gemma4-31b-it"
    assert config.llm.temperature == 0.7
    assert config.agent.name == "CustomMate"


def test_env_api_key_injection(tmp_path: Path, monkeypatch):
    """Verify that INNKUBE_API_KEY from environment or .env is injected into config."""
    monkeypatch.setenv("INNKUBE_API_KEY", "secret-test-key-12345")
    
    config = load_config(config_path="non_existent.yaml", env_path="non_existent.env")
    assert config.llm.api_key == "secret-test-key-12345"


def test_dotenv_file_parsing(tmp_path: Path, monkeypatch):
    """Verify that keys from .env file are loaded."""
    monkeypatch.delenv("INNKUBE_API_KEY", raising=False)
    
    env_file = tmp_path / ".env"
    env_file.write_text("INNKUBE_API_KEY=from-dotenv-file\n", encoding="utf-8")
    
    config = load_config(config_path="non_existent.yaml", env_path=env_file)
    assert config.llm.api_key == "from-dotenv-file"
