"""
Obsidian Knowledge Base Linter for StudyMate.

Performs static analysis and graph health checks on Obsidian Markdown notes
in the vault/artifacts directory:
1. Validates YAML frontmatter metadata schema (checks required 'title' & 'status').
2. Extracts and checks [[wikilinks]] for broken references.
3. Detects orphan pages with zero inbound links.
4. Parses open questions logged under `## Open Questions`.
"""

import os
import re
import yaml
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Set, Tuple


@dataclass
class VaultLintReport:
    """Structured report produced by VaultLinter containing all vault health metrics."""
    total_files: int = 0
    total_links: int = 0
    broken_links: List[Tuple[str, str]] = field(default_factory=list)  # (source_file, broken_target)
    orphan_pages: List[str] = field(default_factory=list)  # note slugs with 0 inbound links
    open_questions: List[Tuple[str, str]] = field(default_factory=list)  # (source_file, question)
    metadata_issues: List[Tuple[str, str]] = field(default_factory=list)  # (source_file, description)

    @property
    def is_healthy(self) -> bool:
        """Returns True if the vault has zero broken links and zero metadata issues."""
        return len(self.broken_links) == 0 and len(self.metadata_issues) == 0


class VaultLinter:
    """
    Scans a vault or artifacts directory to ensure knowledge graph integrity:
    1. Broken wikilinks ([[missing-note]])
    2. Orphan notes (no inbound links)
    3. Pending open questions
    4. Frontmatter schema validation
    """

    WIKILINK_PATTERN = re.compile(r"\[\[(.*?)\]\]")
    FRONTMATTER_PATTERN = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)
    OPEN_QUESTIONS_PATTERN = re.compile(r"## Open Questions\s*\n(.*?)(?=\n## |\Z)", re.DOTALL)

    def __init__(self, vault_dir: Path | str) -> None:
        """
        Initializes the linter with the root directory of the Obsidian vault.

        Args:
            vault_dir: Path to the vault or artifacts folder.
        """
        self.vault_dir = Path(vault_dir).resolve()

    def lint(self) -> VaultLintReport:
        """
        Executes a full health check scan across all Markdown notes in the vault.
        
        Returns:
            VaultLintReport populated with detected issues and graph metrics.
        """
        report = VaultLintReport()

        if not self.vault_dir.exists():
            return report

        # Collect all markdown files
        md_files: List[Path] = []
        for root, _, files in os.walk(self.vault_dir):
            for f in files:
                if f.endswith(".md"):
                    md_files.append(Path(root) / f)

        report.total_files = len(md_files)
        if report.total_files == 0:
            return report

        # Map slug/filename to Path
        known_slugs: Dict[str, Path] = {}
        for file_path in md_files:
            slug = file_path.stem.lower()
            known_slugs[slug] = file_path
            # Also map full filename
            known_slugs[file_path.name.lower()] = file_path

        outbound_links: Dict[str, Set[str]] = {f.stem.lower(): set() for f in md_files}
        inbound_links: Dict[str, Set[str]] = {f.stem.lower(): set() for f in md_files}

        for file_path in md_files:
            slug = file_path.stem.lower()
            rel_name = file_path.relative_to(self.vault_dir).as_posix()
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                report.metadata_issues.append((rel_name, "Failed to read file (encoding error)"))
                continue

            # 1. Frontmatter Validation
            fm_match = self.FRONTMATTER_PATTERN.search(content)
            if fm_match:
                try:
                    fm_data = yaml.safe_load(fm_match.group(1)) or {}
                    if not isinstance(fm_data, dict):
                        report.metadata_issues.append((rel_name, "Frontmatter is not a valid YAML mapping"))
                    else:
                        for required_field in ("title", "status"):
                            if required_field not in fm_data:
                                report.metadata_issues.append((rel_name, f"Missing required frontmatter field: '{required_field}'"))
                except yaml.YAMLError as e:
                    report.metadata_issues.append((rel_name, f"Malformed YAML frontmatter: {e}"))
            elif content.startswith("---"):
                report.metadata_issues.append((rel_name, "Unclosed YAML frontmatter block"))

            # 2. Extract [[wikilinks]]
            links = self.WIKILINK_PATTERN.findall(content)
            for raw_link in links:
                report.total_links += 1
                # Clean link target (remove display text [[target|display]] or anchor [[target#section]])
                target = raw_link.split("|")[0].split("#")[0].strip().lower()
                if not target:
                    continue

                if target in known_slugs:
                    target_slug = known_slugs[target].stem.lower()
                    outbound_links[slug].add(target_slug)
                    inbound_links[target_slug].add(slug)
                else:
                    report.broken_links.append((rel_name, raw_link))

            # 3. Extract Open Questions
            oq_match = self.OPEN_QUESTIONS_PATTERN.search(content)
            if oq_match:
                oq_block = oq_match.group(1).strip()
                for line in oq_block.splitlines():
                    cleaned_line = line.strip()
                    if cleaned_line.startswith("- [ ]") or (cleaned_line.startswith("-") and not cleaned_line.startswith("- [x]")):
                        question = cleaned_line.lstrip("- [ ]").strip()
                        if question and question != "*(None logged)*":
                            report.open_questions.append((rel_name, question))

        # 4. Detect Orphan Pages (notes with 0 inbound links)
        for file_path in md_files:
            slug = file_path.stem.lower()
            rel_name = file_path.relative_to(self.vault_dir).as_posix()
            # Ignore index or summary hub pages as orphans
            if slug in ("index", "readme", "topics", "connections"):
                continue
            if len(inbound_links.get(slug, set())) == 0:
                report.orphan_pages.append(rel_name)

        return report

