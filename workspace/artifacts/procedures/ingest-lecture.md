---
name: "ingest-lecture"
triggers: ["ingest", "lecture slides", "study notes", "import material"]
scope: "Applies when the student asks to ingest source material (slides, notes, PDFs) into Obsidian notes. Does not apply to single-concept questions or grilling."
version: 1
status: "approved"
supersedes: ""
---

## Steps
1. List all files in the source directory first (list_files).
2. Read each file fully (read_file; for PDFs, repeat read_pdf in page ranges until every page is covered).
3. For each source file: write one lecture summary note and one concept note per key concept.
4. Save lecture summaries as `workspace/artifacts/lecture-<slug>.md` and concept notes as `workspace/artifacts/concept-<slug>.md` (write_file creates parents; never create the artifact root manually).
5. Cross-link every concept note to its lecture with `[[wikilinks]]`.
6. Finish with a concise completion summary listing all created artifacts (never an empty response).

## Rationale
Listing first prevents silent skips of files the model didn't know existed. Page-ranged PDF reads exist because one read_pdf call only returns a limited page range. The completion summary is required because the ReAct loop treats an empty final response as a failure and re-prompts.

## Pitfalls
- Reading only the first file and assuming the rest match it.
- Writing lecture and concept notes without `[[wikilinks]]` (orphans the graph; `/lint` will flag it).
- Creating the artifact root folder manually (unnecessary; write_file handles parents).

## Example
Student: "/ingest workspace/data" with lecture01.pdf (40 pages) → 4× read_pdf calls, lecture-lecture01.md + concept notes, all cross-linked, completion summary listing 6 artifacts.
