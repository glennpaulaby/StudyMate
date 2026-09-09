"""Shared service implementations used by the MCP server and local tool bridge."""

from __future__ import annotations

import json
import logging
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote_plus, urldefrag, urljoin, urlparse

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


class _WebPageParser(HTMLParser):
    """Extracts structured Markdown and links from an HTML page."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.links: list[str] = []
        self._output: list[str] = []
        self._skip_depth = 0
        self._in_pre = False
        self._link_url: str | None = None
        self._link_text: list[str] = []
        self._list_stack: list[str] = []
        self._ol_counters: list[int] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}:
            self._skip_depth += 1
            return
        if self._skip_depth > 0:
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            level = int(tag[1])
            self._output.append(f"\n\n{'#' * level} ")
        elif tag in {"p", "section", "article"}:
            self._output.append("\n\n")
        elif tag == "br":
            self._output.append("\n")
        elif tag == "hr":
            self._output.append("\n\n---\n\n")
        elif tag == "blockquote":
            self._output.append("\n\n> ")
        elif tag == "pre":
            self._in_pre = True
            self._output.append("\n\n```\n")
        elif tag == "code" and not self._in_pre:
            self._output.append("`")
        elif tag in {"strong", "b"}:
            self._output.append("**")
        elif tag in {"em", "i"}:
            self._output.append("*")
        elif tag == "ul":
            self._list_stack.append("ul")
            self._output.append("\n")
        elif tag == "ol":
            self._list_stack.append("ol")
            self._ol_counters.append(1)
            self._output.append("\n")
        elif tag == "li":
            indent = "  " * max(0, len(self._list_stack) - 1)
            if self._list_stack and self._list_stack[-1] == "ol" and self._ol_counters:
                idx = self._ol_counters[-1]
                self._ol_counters[-1] += 1
                self._output.append(f"\n{indent}{idx}. ")
            else:
                self._output.append(f"\n{indent}- ")
        elif tag == "a":
            href = attributes.get("href")
            if href:
                self.links.append(href)
                self._link_url = href
                self._link_text = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "noscript", "svg", "nav", "footer", "header", "aside", "form"}:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if self._skip_depth > 0:
            return

        if tag in {"h1", "h2", "h3", "h4", "h5", "h6", "p", "section", "article", "blockquote"}:
            self._output.append("\n\n")
        elif tag == "pre":
            self._in_pre = False
            self._output.append("\n```\n\n")
        elif tag == "code" and not self._in_pre:
            self._output.append("`")
        elif tag in {"strong", "b"}:
            self._output.append("**")
        elif tag in {"em", "i"}:
            self._output.append("*")
        elif tag == "ul":
            if self._list_stack:
                self._list_stack.pop()
            self._output.append("\n")
        elif tag == "ol":
            if self._list_stack:
                self._list_stack.pop()
            if self._ol_counters:
                self._ol_counters.pop()
            self._output.append("\n")
        elif tag == "a" and self._link_url is not None:
            text = "".join(self._link_text).strip()
            if text:
                self._output.append(f"[{text}]({self._link_url})")
            else:
                self._output.append(self._link_url)
            self._link_url = None
            self._link_text = []

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        if self._in_pre:
            self._output.append(data)
            self.text_parts.append(data.strip())
            return
        if self._link_url is not None:
            self._link_text.append(data)
            self.text_parts.append(data.strip())
            return

        cleaned = re.sub(r"[ \t\r\f\v]+", " ", data)
        if cleaned:
            self._output.append(cleaned)
            part = cleaned.strip()
            if part:
                self.text_parts.append(part)

    def get_markdown(self) -> str:
        """Returns normalized Markdown string."""
        raw = "".join(self._output)
        lines = [line.rstrip() for line in raw.splitlines()]
        cleaned = "\n".join(lines)
        return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


class WebResearchService:
    """Searches the public web and optionally follows relevant source pages."""

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
        topic, max_results, domains = self._validate_search_inputs(
            topic, max_results, domains
        )
        results, queries = self._search_results(topic, max_results, domains)

        return json.dumps(
            {
                "topic": topic,
                "queries": queries,
                "sources": results,
                "source_count": len(results),
            },
            ensure_ascii=False,
        )

    def deep_search(
        self,
        topic: str,
        max_results: int = 5,
        domains: list[str] | None = None,
        depth: int = 1,
        max_pages: int = 10,
    ) -> str:
        """Searches multiple sources and follows same-site links to gather context."""
        topic, max_results, domains = self._validate_search_inputs(
            topic, max_results, domains
        )
        if not 0 <= depth <= 2:
            raise ValueError("depth must be between 0 and 2")
        if not 1 <= max_pages <= 20:
            raise ValueError("max_pages must be between 1 and 20")

        sources, queries = self._search_results(topic, max_results, domains)
        client = self._client or httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "StudyMate/1.0 (academic research assistant)"},
        )
        close_client = self._client is None
        try:
            for source in sources:
                source["pages"] = self._crawl(
                    client, source["url"], depth, max_pages
                )
        finally:
            if close_client:
                client.close()

        return json.dumps(
            {
                "topic": topic,
                "queries": queries,
                "sources": sources,
                "source_count": len(sources),
                "page_count": sum(len(source["pages"]) for source in sources),
            },
            ensure_ascii=False,
        )

    def _search_results(
        self, topic: str, max_results: int, domains: list[str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        """Runs independent searches so requested domains contribute sources."""
        queries = [topic] if not domains else [
            f"{topic} site:{domain}" for domain in domains
        ]
        client = self._client or httpx.Client(
            timeout=self.timeout,
            follow_redirects=True,
            headers={"User-Agent": "StudyMate/1.0 (academic research assistant)"},
        )
        close_client = self._client is None
        results: list[dict[str, str]] = []
        seen_urls: set[str] = set()
        try:
            for query in queries:
                response = client.get(f"{self.endpoint}?q={quote_plus(query)}")
                response.raise_for_status()
                parser = _SearchResultParser()
                parser.feed(response.text)
                for result in parser.results:
                    url = self._normalize_result_url(result["url"])
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)
                    results.append({**result, "url": url})
                    if len(results) >= max_results:
                        return results, queries
        finally:
            if close_client:
                client.close()
        return results, queries

    @staticmethod
    def _validate_search_inputs(
        topic: str, max_results: int, domains: list[str] | None
    ) -> tuple[str, int, list[str]]:
        topic = topic.strip()
        if not topic:
            raise ValueError("topic must not be empty")
        if not 1 <= max_results <= 10:
            raise ValueError("max_results must be between 1 and 10")
        clean_domains = [domain.strip() for domain in (domains or []) if domain.strip()]
        return topic, max_results, clean_domains

    def _crawl(
        self, client: httpx.Client, start_url: str, depth: int, max_pages: int
    ) -> list[dict[str, Any]]:
        """Crawls one origin with a bounded breadth-first traversal."""
        start = self._normalize_result_url(start_url)
        origin = urlparse(start).netloc
        queue: list[tuple[str, int]] = [(start, 0)]
        visited: set[str] = set()
        pages: list[dict[str, Any]] = []
        while queue and len(pages) < max_pages:
            url, current_depth = queue.pop(0)
            if url in visited or urlparse(url).netloc != origin:
                continue
            visited.add(url)
            try:
                response = client.get(url)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning("Unable to fetch research page %s: %s", url, exc)
                continue
            content_type = response.headers.get("content-type", "")
            if content_type and "html" not in content_type.lower():
                continue
            parser = _WebPageParser()
            parser.feed(response.text)
            pages.append(
                {
                    "url": url,
                    "depth": current_depth,
                    "content": (parser.get_markdown() or " ".join(parser.text_parts))[:8000],
                }
            )
            if current_depth < depth:
                for link in parser.links:
                    child = self._normalize_result_url(urljoin(url, link))
                    if child and urlparse(child).netloc == origin:
                        queue.append((child, current_depth + 1))
        return pages

    @staticmethod
    def _normalize_result_url(raw_url: str) -> str:
        """Normalizes search redirect URLs and rejects non-web schemes."""
        parsed = urlparse(raw_url)
        if parsed.netloc == "duckduckgo.com" and parsed.path == "/l/":
            target = parse_qs(parsed.query).get("uddg", [""])[0]
            parsed = urlparse(target)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return ""
        normalized, _ = urldefrag(parsed.geturl())
        return normalized.rstrip("/")


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

    def edit_note(self, path: str, target: str, replacement: str) -> str:
        """Replaces one unique text fragment in an existing Markdown note."""
        note = self._resolve(path)
        if note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be edited")
        if not note.is_file():
            raise FileNotFoundError(f"Note not found: {path}")
        if not target:
            raise ValueError("target must not be empty")
        content = note.read_text(encoding="utf-8")
        occurrences = content.count(target)
        if occurrences != 1:
            raise ValueError(
                f"target must appear exactly once; found {occurrences} occurrences"
            )
        note.write_text(content.replace(target, replacement), encoding="utf-8")
        return json.dumps(
            {"path": path.replace("\\", "/"), "edited": True},
            ensure_ascii=False,
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

    @staticmethod
    def _extract_wikilinks(content: str) -> list[str]:
        """Extracts unique wikilink targets in order of appearance."""
        raw_links = re.findall(r"\[\[(.*?)\]\]", content)
        seen: set[str] = set()
        targets: list[str] = []
        for raw in raw_links:
            target = raw.split("|")[0].split("#")[0].strip()
            if target and target not in seen:
                seen.add(target)
                targets.append(target)
        return targets

    def get_outgoing_links(self, path: str) -> str:
        """Extracts all [[wikilinks]] from a Markdown note."""
        note = self._resolve(path)
        if note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be inspected")
        if not note.is_file():
            raise FileNotFoundError(f"Note not found: {path}")
        content = note.read_text(encoding="utf-8")
        links = self._extract_wikilinks(content)
        return json.dumps(
            {
                "path": path.replace("\\", "/"),
                "outgoing_links": links,
                "count": len(links),
            },
            ensure_ascii=False,
        )

    def get_backlinks(self, path: str) -> str:
        """Finds all notes in the vault that link to the specified note via [[wikilinks]]."""
        target_note = self._resolve(path)
        if target_note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be inspected")
        if not target_note.is_file():
            raise FileNotFoundError(f"Note not found: {path}")

        target_stem = target_note.stem.casefold()
        target_name = target_note.name.casefold()
        target_rel = str(target_note.relative_to(self.root)).replace("\\", "/").casefold()
        target_rel_no_ext = str(target_note.relative_to(self.root).with_suffix("")).replace("\\", "/").casefold()
        target_identifiers = {target_stem, target_name, target_rel, target_rel_no_ext}

        backlinks: list[str] = []
        for candidate in sorted(self.root.rglob("*.md")):
            if not candidate.is_file() or candidate.resolve() == target_note.resolve():
                continue
            content = candidate.read_text(encoding="utf-8", errors="replace")
            links = self._extract_wikilinks(content)
            if any(link.casefold() in target_identifiers for link in links):
                backlinks.append(str(candidate.relative_to(self.root)).replace("\\", "/"))

        return json.dumps(
            {
                "path": path.replace("\\", "/"),
                "backlinks": backlinks,
                "count": len(backlinks),
            },
            ensure_ascii=False,
        )

    def insert_into_section(self, path: str, heading: str, content: str) -> str:
        """Inserts content at the end of a specific Markdown heading section."""
        note = self._resolve(path)
        if note.suffix.lower() != ".md":
            raise ValueError("Only Markdown notes can be edited")
        if not note.is_file():
            raise FileNotFoundError(f"Note not found: {path}")

        clean_heading = heading.strip()
        if not clean_heading:
            raise ValueError("heading must not be empty")

        clean_heading_text = re.sub(r"^#+\s*", "", clean_heading).strip()
        if not clean_heading_text:
            raise ValueError("heading text must not be empty")

        lines = note.read_text(encoding="utf-8").splitlines()

        heading_idx = -1
        heading_level = 0
        for idx, line in enumerate(lines):
            match = re.match(r"^(#{1,6})\s+(.*?)$", line.strip())
            if match and match.group(2).strip().casefold() == clean_heading_text.casefold():
                heading_idx = idx
                heading_level = len(match.group(1))
                break

        if heading_idx == -1:
            raise ValueError(f"Heading '{heading}' not found in note: {path}")

        insert_idx = len(lines)
        for idx in range(heading_idx + 1, len(lines)):
            match = re.match(r"^(#{1,6})\s+", lines[idx].strip())
            if match and len(match.group(1)) <= heading_level:
                insert_idx = idx
                break

        prefix_lines = list(lines[:insert_idx])
        while prefix_lines and prefix_lines[-1].strip() == "" and len(prefix_lines) > heading_idx + 1:
            prefix_lines.pop()

        suffix_lines = lines[insert_idx:]
        content_lines = content.strip().splitlines()

        new_lines: list[str] = list(prefix_lines)
        new_lines.extend(content_lines)
        if suffix_lines:
            new_lines.append("")
            new_lines.extend(suffix_lines)
        else:
            new_lines.append("")

        note.write_text("\n".join(new_lines), encoding="utf-8")
        return json.dumps(
            {
                "path": path.replace("\\", "/"),
                "heading": heading,
                "inserted": True,
            },
            ensure_ascii=False,
        )

