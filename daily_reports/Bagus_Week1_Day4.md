# My contributions:
- bugfix: Enforced the `workspace/` directory as the filesystem sandbox root, rejecting configured read or write paths outside the workspace
- feature: Added the safe `rename_file` tool for natural-language requests such as renaming an artifact note
- bugfix: Improved natural-language agent instructions for mind-map generation, file editing, rename operations, and verification
- bugfix: Changed repeated empty model responses from a successful empty result into a clear agent failure with an actionable error message
- bugfix: Changed the CLI error label from the ingestion-specific `Ingestion stopped` to the general `Agent stopped`
- bugfix: Increased the configured ReAct iteration budget from 15 to 50 so multi-step ingestion workflows can finish
- bugfix: Capped PDF extraction requests at 10 pages per call and added continuation instructions for reading the next page range
- test: Added regression tests for workspace containment, safe artifact renaming, repeated empty responses, and oversized PDF requests
- test: Ran the focused regression suite successfully with 47 passing tests

# Notes:
- The full suite still contains one pre-existing configuration mismatch: the code default for `max_iterations` is 60 while an existing test expects 15. The configured runtime value is now 50.
