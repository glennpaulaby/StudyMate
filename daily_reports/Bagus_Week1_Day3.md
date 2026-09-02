# My contributions:
- impl: BaseTool and ToolRegistry with OpenAI function calling schema generation
- impl: Sandboxed filesystem tools (create_folder, write_file, read_file, list_files, search_text, edit_file)
- impl: PathSecurityManager enforcing workspace path jail, read-only data boundaries, and strict deletion prohibition
- impl: AgentEngine executing the ReAct (Reason + Act) loop with iteration budget guards and action callbacks
- impl: Integrated AgentEngine and ToolRegistry into the interactive terminal REPL (studymate)
- test: Unit tests for ToolRegistry, filesystem tools, security boundary violations, and ReAct loop state transitions
- test: End-to-end benchmark tests for numbers_in_words and find_and_inspect

# Team:
- Aligned on branch separation (feat/tools-and-react-loop) off feat/studymate-core
- Prepared merge request and asked teammates for code review on ToolRegistry and ReAct engine implementation
