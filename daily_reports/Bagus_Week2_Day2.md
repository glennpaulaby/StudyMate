# My contributions:
- feature: Extended the MCP web research service toward a Perplexity-style workflow with independent searches across multiple requested domains
- feature: Added bounded same-site crawling with configurable link depth and page limits
- feature: Added HTML page parsing that extracts readable content and follows relevant internal links while ignoring external origins and non-HTML responses
- feature: Returned structured research evidence including search queries, source metadata, crawled page content, crawl depth, source counts, and page counts
- feature: Added safe `obsidian_edit_note` support for targeted Markdown edits
- safety: Required edit targets to occur exactly once, preserved vault path containment, retained Markdown-only operations, overwrite protection, and the no-delete guarantee
- integration: Exposed the new Obsidian edit operation through both the stdio MCP server and StudyMate's local `ToolRegistry`
- documentation: Updated the README to document multi-source deep research, filesystem-first Obsidian note-taking, anchored edits, and `[[wikilinks]]`
- maintenance: Repaired the virtual environment's stale editable install so the `studymate` command imports the current repository correctly
- validation: Added MCP tests for multi-domain searches, same-site crawling, extracted page content, targeted note edits, and duplicate-target rejection
- validation: MCP tests and lint checks pass; the full suite still has one pre-existing configuration mismatch where the test expects `max_iterations=15` while the current configuration provides `60`

# Notes:
- `research_topic` defaults to one level of same-site exploration; use `depth=0` for search-only behavior
- `domains` are searched independently so relevant sources can be gathered from multiple websites
- `max_pages` bounds the number of pages crawled per source
- `obsidian_edit_note` uses an exact text target and refuses ambiguous replacements
