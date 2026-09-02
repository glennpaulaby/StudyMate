from pathlib import Path
from study_agent.config.settings import AgentConfig, FilesystemToolConfig
from study_agent.core.context import ContextManager
from study_agent.core.engine import AgentEngine
from study_agent.core.types import ToolCall
from study_agent.llm.mock import MockLLMClient
from study_agent.tools.base import ToolRegistry
from study_agent.tools.filesystem import register_filesystem_tools


def test_e2e_numbers_in_words(tmp_path: Path):
    """
    Lab Benchmark Test 1: numbers_in_words
    Tools exercised: create_folder, write_file, search_text
    Prompt: Create a folder called output and inside it a file called numbers.md
            that contains the list with the numbers from 1 to 20 in words.
            At least, make sure the first and last numbers exist.
    Assert:
    - Folder output/ exists.
    - File output/numbers.md exists and contains the byte-exact texts 'one' and 'twenty'.
    - Final agent turn acknowledges.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = FilesystemToolConfig(
        allowed_read_paths=[str(workspace)],
        allowed_write_paths=[str(workspace)]
    )
    registry = ToolRegistry()
    register_filesystem_tools(registry, base_dir=workspace, config=config)

    mock_llm = MockLLMClient()
    # Step 1: Agent decides to create folder 'output'
    mock_llm.add_response(
        content="I will create the folder 'output'.",
        tool_calls=[ToolCall(id="call_1", name="create_folder", arguments={"path": "output"})]
    )
    # Step 2: Agent writes numbers 1-20 in words to 'output/numbers.md'
    numbers_content = (
        "1. one\n2. two\n3. three\n4. four\n5. five\n6. six\n7. seven\n8. eight\n"
        "9. nine\n10. ten\n11. eleven\n12. twelve\n13. thirteen\n14. fourteen\n"
        "15. fifteen\n16. sixteen\n17. seventeen\n18. eighteen\n19. nineteen\n20. twenty\n"
    )
    mock_llm.add_response(
        content="Now I will write the numbers list to 'output/numbers.md'.",
        tool_calls=[ToolCall(id="call_2", name="write_file", arguments={"path": "output/numbers.md", "content": numbers_content})]
    )
    # Step 3: Agent searches to verify 'twenty' exists
    mock_llm.add_response(
        content="Verifying the file content...",
        tool_calls=[ToolCall(id="call_3", name="search_text", arguments={"query": "twenty", "path": "output/numbers.md"})]
    )
    # Step 4: Final acknowledgement
    mock_llm.add_response(
        content="I have created the folder 'output' and wrote the list of numbers from 1 to 20 in words into 'output/numbers.md'."
    )

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=10))
    context = ContextManager()

    prompt = (
        "Create a folder called output and inside it a file called numbers.md that "
        "contains the a list with the numbers from 1 to 20 in words. At least, make sure "
        "the first and last numbers exist."
    )
    result = engine.run(prompt, context)

    # Asserts as required by course specification
    output_folder = workspace / "output"
    assert output_folder.is_dir(), "Folder output/ must exist"

    numbers_file = output_folder / "numbers.md"
    assert numbers_file.is_file(), "File output/numbers.md must exist"

    file_content = numbers_file.read_text(encoding="utf-8")
    assert "one" in file_content, "numbers.md must contain byte-exact 'one'"
    assert "twenty" in file_content, "numbers.md must contain byte-exact 'twenty'"

    assert result.is_success is True
    assert "output" in result.final_response.lower()
    assert result.tool_calls_count == 3


def test_e2e_find_and_inspect(tmp_path: Path):
    """
    Lab Benchmark Test 2: find_and_inspect
    Tools exercised: list_files, read_file
    Setup: Pre-populate path_ABC/ tree:
      path_ABC/
        data/
          notes.txt -> "This\nfile\nhas\none\nline" (5 lines)
          readme.md -> "Kartoffelsalat"
        empty/
    Prompt: Tell me what is inside path_ABC/. If notes.txt exists, read it and tell me how many lines it has.
    Assert:
    - Assistant mentions both notes.txt and readme.md in the listing.
    - Assistant reports 5 lines inside notes.txt.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    # Pre-populate tree
    path_abc = workspace / "path_ABC"
    data_dir = path_abc / "data"
    empty_dir = path_abc / "empty"
    data_dir.mkdir(parents=True)
    empty_dir.mkdir(parents=True)

    notes_file = data_dir / "notes.txt"
    notes_file.write_text("This\nfile\nhas\none\nline", encoding="utf-8")

    readme_file = data_dir / "readme.md"
    readme_file.write_text("Kartoffelsalat", encoding="utf-8")

    config = FilesystemToolConfig(
        allowed_read_paths=[str(workspace)],
        allowed_write_paths=[str(workspace)]
    )
    registry = ToolRegistry()
    register_filesystem_tools(registry, base_dir=workspace, config=config)

    mock_llm = MockLLMClient()
    # Step 1: Agent inspects path_ABC/data
    mock_llm.add_response(
        content="I will inspect the contents of path_ABC/data.",
        tool_calls=[ToolCall(id="call_list", name="list_files", arguments={"path": "path_ABC/data"})]
    )
    # Step 2: Agent reads notes.txt
    mock_llm.add_response(
        content="I see notes.txt and readme.md. Now reading notes.txt.",
        tool_calls=[ToolCall(id="call_read", name="read_file", arguments={"path": "path_ABC/data/notes.txt"})]
    )
    # Step 3: Agent final response reporting both files and line count
    mock_llm.add_response(
        content=(
            "Inside path_ABC/data/, I found notes.txt and readme.md. "
            "After inspecting notes.txt, it contains 5 lines."
        )
    )

    engine = AgentEngine(llm_client=mock_llm, tool_registry=registry, config=AgentConfig(max_iterations=10))
    context = ContextManager()

    prompt = "Tell me what is inside path_ABC/. If notes.txt exists, read it and tell me how many lines it has."
    result = engine.run(prompt, context)

    assert result.is_success is True
    assert "notes.txt" in result.final_response
    assert "readme.md" in result.final_response
    assert "5 lines" in result.final_response or "5" in result.final_response
