import pytest
from pathlib import Path
from study_agent.config.settings import AppConfig, LLMConfig, AgentConfig
from study_agent.llm.mock import MockLLMClient


@pytest.fixture
def sample_config() -> AppConfig:
    """Returns a valid default AppConfig for testing."""
    return AppConfig(
        llm=LLMConfig(
            model="test-model",
            temperature=0.0,
            api_key="test-api-key",
            base_url="https://mock-endpoint.local/v1"
        ),
        agent=AgentConfig(
            name="TestAgent",
            max_iterations=5,
            workspace_dir="./test_workspace"
        )
    )


@pytest.fixture
def mock_llm() -> MockLLMClient:
    """Returns a pre-configured MockLLMClient."""
    return MockLLMClient()


@pytest.fixture
def temp_workspace(tmp_path: Path) -> Path:
    """Creates a temporary workspace directory tree with data and artifacts dirs."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "data").mkdir()
    (workspace / "artifacts").mkdir()
    return workspace
