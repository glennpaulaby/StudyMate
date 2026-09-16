# My contributions:
- bugfix: DuckDuckGo MCP startup logs, deprecation warnings, and HTTP request diagnostics were leaking into the StudyMate REPL. The external MCP bridge now redirects the subprocess stderr to `DEVNULL` while preserving MCP tool results and StudyMate permission messages.
- bugfix: corrected the MCP worker startup indentation and retained `LOG_LEVEL=ERROR` and `PYTHONWARNINGS=ignore` for quieter subprocess behavior.
- improvement: added explicit tool completion feedback to the CLI. Successful calls now show `✓ Completed: <tool>` and failed calls show `✗ Failed: <tool>`, making it clear whether a multi-step request is waiting on a tool or on the next LLM response.
- investigation: verified that two sequential DuckDuckGo searches complete successfully in the bundled Docker image. The apparent freeze after the second search was not an MCP deadlock; it occurs while the agent waits for the LLM to synthesize the final response.
- maintenance: cleaned the project-scoped Docker containers, network, volumes, orphaned resources, and `studymate:latest` image so the image can be rebuilt from a clean state with `docker compose build --no-cache studymate`.
- validation: all 153 unit tests passed. The live DuckDuckGo MCP integration tests were skipped locally because the server binary is only prebaked into the Docker image.
