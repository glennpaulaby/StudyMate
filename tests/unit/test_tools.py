import pytest
from pathlib import Path
from study_agent.config.settings import FilesystemToolConfig
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import (
    PathSecurityManager,
    CreateFolderTool,
    WriteFileTool,
    ReadFileTool,
    ListFilesTool,
    SearchTextTool,
    EditFileTool,
    register_filesystem_tools,
)


@pytest.fixture
def sandbox_env(tmp_path: Path):
    """Creates a sandboxed filesystem environment for testing."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = workspace / "data"
    data_dir.mkdir()
    artifacts_dir = workspace / "artifacts"
    artifacts_dir.mkdir()

    config = FilesystemToolConfig(
        enabled=True,
        allow_delete=False,
        allowed_read_paths=[str(workspace)],
        allowed_write_paths=[str(artifacts_dir)]
    )
    security = PathSecurityManager(base_dir=workspace, config=config)
    registry = ToolRegistry()
    register_filesystem_tools(registry, base_dir=workspace, config=config)

    return {
        "workspace": workspace,
        "data_dir": data_dir,
        "artifacts_dir": artifacts_dir,
        "security": security,
        "registry": registry,
        "config": config,
    }


def test_tool_registry_schemas(sandbox_env):
    """Verify ToolRegistry generates valid OpenAI tool definitions."""
    registry = sandbox_env["registry"]
    schemas = registry.list_schemas()

    assert len(schemas) == 6
    names = {s["function"]["name"] for s in schemas}
    assert names == {"create_folder", "write_file", "read_file", "list_files", "search_text", "edit_file"}

    for s in schemas:
        assert s["type"] == "function"
        assert "name" in s["function"]
        assert "description" in s["function"]
        assert "parameters" in s["function"]


def test_create_folder_and_write_file(sandbox_env):
    """Verify create_folder and write_file within permitted artifacts directory."""
    registry = sandbox_env["registry"]
    
    # Create subfolder in artifacts
    res_mkdir = registry.execute("create_folder", {"path": "artifacts/flashcards"})
    assert "Successfully created folder" in res_mkdir
    assert (sandbox_env["artifacts_dir"] / "flashcards").is_dir()

    # Write file
    res_write = registry.execute(
        "write_file",
        {"path": "artifacts/flashcards/deck1.md", "content": "# Deck 1\nQ: What is QuickSort?\nA: Divide and conquer"}
    )
    assert "Successfully wrote" in res_write
    assert (sandbox_env["artifacts_dir"] / "flashcards" / "deck1.md").is_file()


def test_read_file_and_pagination(sandbox_env):
    """Verify read_file with offset and limit."""
    registry = sandbox_env["registry"]
    artifacts = sandbox_env["artifacts_dir"]
    
    test_file = artifacts / "notes.txt"
    lines = [f"Line {i}" for i in range(1, 11)]
    test_file.write_text("\n".join(lines), encoding="utf-8")

    # Read all
    res_full = registry.execute("read_file", {"path": "artifacts/notes.txt"})
    assert "Total lines: 10" in res_full
    assert "Line 1" in res_full
    assert "Line 10" in res_full

    # Read with offset and limit (lines 3 to 5)
    res_slice = registry.execute("read_file", {"path": "artifacts/notes.txt", "offset": 3, "limit": 3})
    assert "Showing lines 3-5" in res_slice
    assert "Line 3" in res_slice
    assert "Line 5" in res_slice
    assert "Line 1" not in res_slice
    assert "Line 6" not in res_slice


def test_list_files(sandbox_env):
    """Verify list_files inspects directories and outputs format."""
    registry = sandbox_env["registry"]
    data_dir = sandbox_env["data_dir"]
    
    (data_dir / "lecture1.pdf").write_text("dummy", encoding="utf-8")
    (data_dir / "exercises").mkdir()

    res = registry.execute("list_files", {"path": "data"})
    assert "Contents of 'data':" in res
    assert "[DIR]  exercises/" in res
    assert "[FILE] lecture1.pdf" in res


def test_search_text(sandbox_env):
    """Verify search_text finds matches recursively."""
    registry = sandbox_env["registry"]
    data_dir = sandbox_env["data_dir"]
    
    sub = data_dir / "subdir"
    sub.mkdir()
    (sub / "doc.txt").write_text("The fast Fourier transform is an algorithm.\nAnother line.", encoding="utf-8")

    res = registry.execute("search_text", {"query": "fourier", "path": "data"})
    assert "Found 1 match" in res
    assert "Fourier transform" in res


def test_edit_file(sandbox_env):
    """Verify edit_file surgically modifies text."""
    registry = sandbox_env["registry"]
    artifacts = sandbox_env["artifacts_dir"]
    
    doc = artifacts / "study_guide.md"
    doc.write_text("# Chapter 1\nStatus: Draft\nContent here.", encoding="utf-8")

    res = registry.execute(
        "edit_file",
        {
            "path": "artifacts/study_guide.md",
            "target": "Status: Draft",
            "replacement": "Status: Complete"
        }
    )
    assert "Successfully edited" in res
    assert "Status: Complete" in doc.read_text(encoding="utf-8")


def test_edit_file_errors(sandbox_env):
    """Verify error handling when target text is missing or duplicate."""
    registry = sandbox_env["registry"]
    artifacts = sandbox_env["artifacts_dir"]
    
    doc = artifacts / "doc.md"
    doc.write_text("apple apple apple", encoding="utf-8")

    # Missing target
    res_missing = registry.execute(
        "edit_file",
        {"path": "artifacts/doc.md", "target": "banana", "replacement": "orange"}
    )
    assert "Error: Target text not found" in res_missing

    # Ambiguous / multiple occurrences
    res_multi = registry.execute(
        "edit_file",
        {"path": "artifacts/doc.md", "target": "apple", "replacement": "orange"}
    )
    assert "appears 3 times" in res_multi


def test_security_path_jail_escape_attempt(sandbox_env):
    """Verify that attempting to escape the sandbox via traversal is blocked."""
    registry = sandbox_env["registry"]

    # Attempt to read outside sandbox
    res_escape_read = registry.execute("read_file", {"path": "../../etc/passwd"})
    assert "Access denied" in res_escape_read or "outside allowed" in res_escape_read

    # Attempt to write outside sandbox
    res_escape_write = registry.execute("write_file", {"path": "../../malicious.sh", "content": "echo bad"})
    assert "Write access denied" in res_escape_write or "outside permitted" in res_escape_write


def test_security_write_to_read_only_data_dir(sandbox_env):
    """Verify that write operations to data_dir (read-only) are blocked."""
    registry = sandbox_env["registry"]

    res_write = registry.execute(
        "write_file",
        {"path": "data/injected.txt", "content": "illegal modification"}
    )
    assert "Write access denied" in res_write


def test_unregistered_tool_execution(sandbox_env):
    """Verify execution of non-existent tool returns clear error."""
    registry = sandbox_env["registry"]
    res = registry.execute("delete_file", {"path": "artifacts/file.txt"})
    assert "Error: Tool 'delete_file' is not registered." in res
