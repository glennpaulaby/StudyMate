"""Shared service implementations used by the MCP server and local tool bridge."""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urlparse

import httpx

logger = logging.getLogger(__name__)


class _SearchResultParser(HTMLParser):
    """Extracts result links and snippets from DuckDuckGo's HTML response."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self._field: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        classes = (attributes.get("class") or "").split()
        if tag == "a" and "result__a" in classes:
            self._current = {
                "title": "",
                "url": attributes.get("href", ""),
                "snippet": "",
            }
            self._field = "title"
        elif self._current and tag in {"a", "div"} and "result__snippet" in classes:
            self._field = "snippet"

    def handle_data(self, data: str) -> None:
        if self._current and self._field:
            self._current[self._field] += data

    def handle_endtag(self, tag: str) -> None:
        if self._current and tag == "a" and self._field == "title":
            self._field = None
        elif self._current and tag in {"a", "div"} and self._field == "snippet":
            result = {
                key: re.sub(r"\s+", " ", value).strip()
                for key, value in self._current.items()
            }
            if result["title"] and result["url"]:
                self.results.append(result)
            self._current = None
            self._field = None


class WebResearchService:
    """Searches the public web and returns deduplicated source metadata."""

    def __init__(
        self,
        endpoint: str = "https://html.duckduckgo.com/html/",
        timeout: float = 15.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.timeout = timeout
        self._client = client

    def search(
        self, topic: str, max_results: int = 5, domains: list[str] | None = None
    ) -> str:
        """Searches for a topic and serializes the aggregated sources as JSON."""
        topic = topic.strip()
        if not topic:
            raise ValueError("topic must not be empty")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")

        query = topic
        if domains:
            query += " " + " ".join(
                f"site:{domain}" for domain in domains if domain.strip()
            )

        client = self._client or httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "StudyMate/1.0 (academic research assistant)"},
        )
        close_client = self._client is None
        try:
            response = client.get(f"{self.endpoint}?q={quote_plus(query)}")
            response.raise_for_status()
            parser = _SearchResultParser()
            parser.feed(response.text)
        finally:
            if close_client:
                client.close()

        results: list[dict[str, str]] = []
        seen_urls = set()
        for result in parser.results:
            url = self._normalize_result_url(result["url"])
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            results.append({**result, "url": url})
            if len(results) >= max_results:
                break

        return json.dumps(
            {
                "topic": topic,
                "query": query,
                "sources": results,
                "source_count": len(results),
            },
            ensure_ascii=False,
        )

    @staticmethod
    def _normalize_result_url(raw_url: str) -> str:
        """Normalizes search redirect URLs and rejects non-web schemes."""
        parsed = urlparse(raw_url)
        if parsed.netloc == "duckduckgo.com" and parsed.path == "/l/":
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        return parsed.geturl()


class ObsidianVaultService:
    """Provides safe, non-destructive operations over a configured Obsidian vault."""

    def __init__(self, vault_path: str | Path) -> None:
        self.root = Path(vault_path).expanduser().resolve()
        if not self.root.exists() or not self.root.is_dir():
            raise ValueError(
                f"Obsidian vault does not exist or is not a directory: {self.root}"
            )

    def _resolve(self, relative_path: str) -> Path:
        """Resolves a vault-relative path and blocks traversal outside the vault."""
        candidate = (self.root / relative_path).resolve()
        try:
            candidate.relative_to(self.root)
        except ValueError as exc:
            raise PermissionError("Path escapes the configured Obsidian vault") from exc
        return candidate

    def list_notes(self, prefix: str = "", limit: int = 100) -> str:
        """Lists Markdown notes under an optional vault-relative prefix."""
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        directory = self._resolve(prefix or ".")
        if not directory.is_dir():
            raise ValueError(f"Vault directory does not exist: {prefix}")
        notes = sorted(
            str(path.relative_to(self.root)).replace("\\", "/")
            for path in directory.rglob("*.md")
            if path.is_file()
        )[:limit]
        return json.dumps(
            {"vault": str(self.root), "notes": notes, "count": len(notes)}
        )

    def read_note(self, path: str) -> str:
        """Reads one Markdown note from the vault."""
        note = self._resolve(path)
        if note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be read")
        if not note.is_file():
            raise FileNotFoundError(f"Note not found: {path}")
        return json.dumps(
            {
                "path": path.replace("\\", "/"),
                "content": note.read_text(encoding="utf-8"),
            },
            ensure_ascii=False,
        )

    def search_notes(self, query: str, limit: int = 20) -> str:
        """Searches note content case-insensitively without following external files."""
        query = query.strip()
        if not query:
            raise ValueError("query must not be empty")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        matches: list[dict[str, Any]] = []
        for note in sorted(self.root.rglob("*.md")):
            if not note.is_file():
                continue
            content = note.read_text(encoding="utf-8", errors="replace")
            lines = [
                {"line": number, "text": line.strip()}
                for number, line in enumerate(content.splitlines(), 1)
                if query.casefold() in line.casefold()
            ]
            if lines:
                matches.append(
                    {
                        "path": str(note.relative_to(self.root)).replace("\\", "/"),
                        "matches": lines[:10],
                    }
                )
            if len(matches) >= limit:
                break
        return json.dumps(
            {"query": query, "notes": matches, "count": len(matches)},
            ensure_ascii=False,
        )

    def write_note(self, path: str, content: str, overwrite: bool = False) -> str:
        """Creates a Markdown note, refusing accidental overwrites by default."""
        note = self._resolve(path)
        if note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be written")
        if note.exists() and not overwrite:
            raise FileExistsError(
                f"Note already exists: {path}; set overwrite=true to replace it"
            )
        note.parent.mkdir(parents=True, exist_ok=True)
        note.write_text(content, encoding="utf-8")
        return json.dumps(
            {
                "path": path.replace("\\", "/"),
                "written": True,
                "overwritten": note.exists() and overwrite,
            }
        )

    def append_note(self, path: str, content: str) -> str:
        """Appends content to an existing Markdown note or creates it."""
        note = self._resolve(path)
        if note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be appended")
        note.parent.mkdir(parents=True, exist_ok=True)
        with note.open("a", encoding="utf-8") as handle:
            if note.exists() and note.stat().st_size > 0:
                handle.write("\n")
            handle.write(content)
        return json.dumps({"path": path.replace("\\", "/"), "appended": True})
