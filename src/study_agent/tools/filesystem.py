import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from study_agent.config.settings import FilesystemToolConfig
from study_agent.tools.base import BaseTool, ToolRegistry


def _atomic_write_text(target: Path, content: str) -> None:
    """Writes crash-atomically: temp file in the same dir + os.replace.

    A crash mid-write can never leave a truncated target: the target path
    only ever flips from old content to new content in one atomic rename.
    Temp file stays inside the already-validated parent dir (jail-safe).
    """
    fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=target.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, target)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


class PathSecurityManager:
    """
    Validates and resolves file paths against configured sandbox policies.
    Guarantees:
    1. Jail containment (no path traversal outside allowed directories).
    2. Read-only protection for source materials.
    3. Strict deletion prohibition.
    """

    def __init__(
        self,
        base_dir: Optional[Path] = None,
        config: Optional[FilesystemToolConfig] = None,
        workspace_root: Optional[Path] = None,
    ) -> None:
        self.base_dir = (base_dir or Path.cwd()).resolve()
        self.config = config or FilesystemToolConfig()
        raw_workspace_root = workspace_root or self.base_dir
        self.workspace_root = (
            (self.base_dir / raw_workspace_root).resolve()
            if not Path(raw_workspace_root).is_absolute()
            else Path(raw_workspace_root).resolve()
        )
        if not self._is_subpath(self.workspace_root, self.base_dir):
            raise ValueError("workspace_root must be contained by base_dir")
        
        # Resolve allowed paths relative to base_dir
        self.allowed_read_roots = [
            (self.base_dir / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            for p in self.config.allowed_read_paths
        ] if self.config.allowed_read_paths else [self.base_dir]

        self.allowed_write_roots = [
            (self.base_dir / p).resolve() if not Path(p).is_absolute() else Path(p).resolve()
            for p in self.config.allowed_write_paths
        ] if self.config.allowed_write_paths else [self.base_dir]
        if any(not self._is_subpath(root, self.workspace_root) for root in self.allowed_read_roots):
            raise PermissionError("Configured read paths must be contained by the workspace sandbox.")
        if any(not self._is_subpath(root, self.workspace_root) for root in self.allowed_write_roots):
            raise PermissionError("Configured write paths must be contained by the workspace sandbox.")

    def _is_subpath(self, target: Path, parent: Path) -> bool:
        """Checks if target is equal to or a subpath of parent."""
        try:
            target.relative_to(parent)
            return True
        except ValueError:
            return False

    def validate_read_path(self, raw_path: str | Path) -> Path:
        """Resolves and validates that the path is within permitted read boundaries."""
        target = (self.base_dir / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
        
        for allowed in self.allowed_read_roots:
            if self._is_subpath(target, allowed):
                return target
                
        raise PermissionError(
            f"Access denied: Path '{raw_path}' resolves outside allowed read boundaries: {[str(p) for p in self.allowed_read_roots]}"
        )

    def validate_write_path(self, raw_path: str | Path) -> Path:
        """Resolves and validates that the path is within permitted write boundaries."""
        target = (self.base_dir / raw_path).resolve() if not Path(raw_path).is_absolute() else Path(raw_path).resolve()
        
        for allowed in self.allowed_write_roots:
            if self._is_subpath(target, allowed):
                return target
                
        raise PermissionError(
            f"Write access denied: Path '{raw_path}' is outside permitted write boundaries: {[str(p) for p in self.allowed_write_roots]}"
        )


class CreateFolderTool(BaseTool):
    """Tool to create directories safely within the allowed write sandbox."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "create_folder"

    @property
    def description(self) -> str:
        return "Create a folder/directory at the specified path (including any intermediate directories)."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path of the folder to create (e.g. 'output' or 'workspace/artifacts/flashcards')"
                }
            },
            "required": ["path"]
        }

    def execute(self, path: str, **kwargs: Any) -> str:
        target = self.security.validate_write_path(path)
        target.mkdir(parents=True, exist_ok=True)
        return f"Successfully created folder: {path}"


class WriteFileTool(BaseTool):
    """Tool to create or overwrite files in the allowed write sandbox."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write text content to a file. Creates intermediate directories if needed."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to write (e.g. 'output/numbers.md' or 'workspace/artifacts/summary.md')"
                },
                "content": {
                    "type": "string",
                    "description": "The exact text content to write into the file"
                }
            },
            "required": ["path", "content"]
        }

    def execute(self, path: str, content: str, **kwargs: Any) -> str:
        target = self.security.validate_write_path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(target, content)
        line_count = len(content.splitlines())
        byte_count = len(content.encode("utf-8"))
        return f"Successfully wrote {byte_count} bytes ({line_count} lines) to {path}"


class ReadFileTool(BaseTool):
    """Tool to read content from files in allowed read locations."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return "Read text content from a file, optionally specifying line offset and limit."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to read (e.g. 'path_ABC/data/notes.txt')"
                },
                "offset": {
                    "type": "integer",
                    "description": "1-based starting line number to read from (default: 1)"
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return (default: 200)"
                }
            },
            "required": ["path"]
        }

    def execute(self, path: str, offset: int = 1, limit: int = 200, **kwargs: Any) -> str:
        target = self.security.validate_read_path(path)
        if not target.exists():
            return f"Error: File not found at {path}"
        if target.is_dir():
            return f"Error: Path '{path}' is a directory, not a file."

        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        total_lines = len(lines)

        start_idx = max(0, offset - 1)
        end_idx = min(total_lines, start_idx + limit)
        selected_lines = lines[start_idx:end_idx]

        header = f"[File: {path} | Total lines: {total_lines} | Showing lines {start_idx + 1}-{end_idx}]\n"
        body = "\n".join(f"{i + 1:4d} | {line}" for i, line in enumerate(selected_lines, start=start_idx))
        return header + body


class ListFilesTool(BaseTool):
    """Tool to inspect directories in allowed read locations."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "list_files"

    @property
    def description(self) -> str:
        return "List files and subdirectories inside a given directory path."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Directory path to list (e.g. 'path_ABC/' or 'workspace/data'). Defaults to base directory if empty."
                }
            },
            "required": []
        }

    def execute(self, path: str = ".", **kwargs: Any) -> str:
        target = self.security.validate_read_path(path or ".")
        if not target.exists():
            return f"Error: Directory not found at {path}"
        if not target.is_dir():
            return f"Error: Path '{path}' is a file, not a directory."

        entries = sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        if not entries:
            return f"Directory '{path}' is empty."

        output = [f"Contents of '{path}':"]
        for entry in entries:
            if entry.is_dir():
                output.append(f"  [DIR]  {entry.name}/")
            else:
                size_kb = entry.stat().st_size / 1024.0
                output.append(f"  [FILE] {entry.name} ({size_kb:.1f} KB)")
                
        return "\n".join(output)


class SearchTextTool(BaseTool):
    """Tool to search text or regex patterns in allowed files."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "search_text"

    @property
    def description(self) -> str:
        return "Search for a text string or regex pattern across files within a directory or file."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Text query or regex pattern to search for"
                },
                "path": {
                    "type": "string",
                    "description": "Directory or file path to search within (default: base directory)"
                }
            },
            "required": ["query"]
        }

    def execute(self, query: str, path: str = ".", **kwargs: Any) -> str:
        target = self.security.validate_read_path(path or ".")
        if not target.exists():
            return f"Error: Path not found at {path}"

        matches: List[str] = []
        files_to_search: List[Path] = []

        if target.is_file():
            files_to_search = [target]
        else:
            for root, _, files in os.walk(target):
                for f in files:
                    file_path = Path(root) / f
                    try:
                        self.security.validate_read_path(file_path)
                        files_to_search.append(file_path)
                    except PermissionError:
                        continue

        pattern = re.compile(re.escape(query), re.IGNORECASE)

        for file_path in files_to_search:
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
                for line_no, line in enumerate(content.splitlines(), start=1):
                    if pattern.search(line):
                        rel_path = file_path.relative_to(self.security.base_dir) if self.security._is_subpath(file_path, self.security.base_dir) else file_path
                        matches.append(f"{rel_path}:{line_no}: {line.strip()}")
                        if len(matches) >= 50:
                            break
            except Exception:
                continue

            if len(matches) >= 50:
                break

        if not matches:
            return f"No matches found for '{query}' in '{path}'."

        return f"Found {len(matches)} match(es):\n" + "\n".join(matches)


class EditFileTool(BaseTool):
    """Tool to surgically replace a target text snippet in a file."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return "Surgically edit a file by replacing target content with replacement content."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the file to edit"
                },
                "target": {
                    "type": "string",
                    "description": "Exact text block to replace"
                },
                "replacement": {
                    "type": "string",
                    "description": "New replacement text"
                }
            },
            "required": ["path", "target", "replacement"]
        }

    def execute(self, path: str, target: str, replacement: str, **kwargs: Any) -> str:
        target_path = self.security.validate_write_path(path)
        if not target_path.exists():
            return f"Error: File not found at {path}"
        if target_path.is_dir():
            return f"Error: Path '{path}' is a directory."

        content = target_path.read_text(encoding="utf-8")
        if target not in content:
            return f"Error: Target text not found in {path}. Make sure the target text matches exactly."

        count = content.count(target)
        if count > 1:
            return f"Error: Target text appears {count} times in {path}. Provide a more specific target snippet."

        new_content = content.replace(target, replacement, 1)
        _atomic_write_text(target_path, new_content)
        return f"Successfully edited {path} (replaced 1 occurrence)."


class RenameFileTool(BaseTool):
    """Tool to rename a file without allowing deletion or sandbox escapes."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "rename_file"

    @property
    def description(self) -> str:
        return "Rename a file within the writable artifacts sandbox."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "source": {"type": "string", "description": "Existing file path to rename"},
                "destination": {"type": "string", "description": "New file path"},
            },
            "required": ["source", "destination"],
        }

    def execute(self, source: str, destination: str, **kwargs: Any) -> str:
        source_path = self.security.validate_write_path(source)
        destination_path = self.security.validate_write_path(destination)
        if not source_path.exists():
            return f"Error: File not found at {source}"
        if source_path.is_dir():
            return f"Error: Path '{source}' is a directory."
        if destination_path.exists():
            return f"Error: Destination already exists at {destination}. Refusing to overwrite it."

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source_path, destination_path)
        return f"Successfully renamed {source} to {destination}."


class ReadPdfTool(BaseTool):
    """Tool to extract text from PDF files with page range pagination."""

    def __init__(self, security: PathSecurityManager) -> None:
        self.security = security

    @property
    def name(self) -> str:
        return "read_pdf"

    @property
    def description(self) -> str:
        return "Extract text content from a PDF document with optional page range pagination (1-based index)."

    @property
    def parameters(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to the PDF file to read (e.g. 'data/lecture-01.pdf')"
                },
                "page_start": {
                    "type": "integer",
                    "description": "1-based starting page number (default: 1)"
                },
                "page_end": {
                    "type": "integer",
                    "description": "1-based ending page number (default: page_start + 10)"
                }
            },
            "required": ["path"]
        }

    def execute(self, path: str, page_start: int = 1, page_end: Optional[int] = None, **kwargs: Any) -> str:
        target = self.security.validate_read_path(path)
        if not target.exists():
            return f"Error: PDF file not found at {path}"
        if target.is_dir():
            return f"Error: Path '{path}' is a directory, not a PDF file."

        try:
            from pypdf import PdfReader
            reader = PdfReader(str(target))
            total_pages = len(reader.pages)
            if total_pages == 0:
                return f"PDF '{path}' has 0 pages."

            start_idx = max(0, page_start - 1)
            end_limit = page_end if page_end is not None else (start_idx + 10)
            end_limit = min(end_limit, start_idx + 10)
            end_idx = min(total_pages, end_limit)

            extracted_pages = []
            for i in range(start_idx, end_idx):
                page = reader.pages[i]
                text = page.extract_text() or "(No readable text on this page)"
                extracted_pages.append(f"--- Page {i + 1}/{total_pages} ---\n{text.strip()}")

            header = (
                f"[PDF: {path} | Total pages: {total_pages} | "
                f"Showing pages {start_idx + 1}-{end_idx}]\n\n"
            )
            if page_end is not None and page_end > end_idx:
                header += (
                    f"Continue with page_start={end_idx + 1} and "
                    f"page_end={min(total_pages, end_idx + 10)} to read the next batch.\n\n"
                )
            return header + "\n\n".join(extracted_pages)
        except Exception as e:
            return f"Error reading PDF '{path}': {str(e)}"


def register_filesystem_tools(
    registry: ToolRegistry,
    base_dir: Optional[Path] = None,
    config: Optional[FilesystemToolConfig] = None,
    workspace_root: Optional[Path] = None,
) -> None:
    """Registers all sandboxed filesystem tools into the provided registry."""
    security = PathSecurityManager(
        base_dir=base_dir,
        config=config,
        workspace_root=workspace_root,
    )
    registry.register(CreateFolderTool(security))
    registry.register(WriteFileTool(security))
    registry.register(ReadFileTool(security))
    registry.register(ReadPdfTool(security))
    registry.register(ListFilesTool(security))
    registry.register(SearchTextTool(security))
    registry.register(EditFileTool(security))
    registry.register(RenameFileTool(security))
