# StudyMate ??

> **StudyMate** is an autonomous AI agent harness built from scratch in Python 3.12 for the *AI Engineering Lab (SoSe 2026)* at the University of Passau. It empowers university students to prepare for courses and exams by autonomously analyzing source materials and generating structured study artifacts (flashcards, mind maps, concept glossaries, and practice exercises).

---

## ?? Overview & Features

StudyMate operates directly from the terminal via an interactive CLI (studymate). Built on the **ReAct (Reason + Act)** paradigm, it acts as an autonomous study assistant:

- **Source Material Analysis**: Deeply inspects and synthesizes lecture slides, research papers, exercise sheets, and textbook notes stored in workspace/data/.
- **Study Artifact Generation**: Generates persistent study resources directly in workspace/artifacts/:
  - ??? **Flashcards**: Markdown-formatted question-answer decks (compatible with Anki/Obsidian).
  - ??? **Mind Maps**: Visual concept hierarchies and topic maps in standard Mermaid.js.
  - ?? **Glossaries & Key Concepts**: Structured term breakdowns and relationship diagrams.
  - ?? **Practice Exams & Quizzes**: Tailored question sets with reasoning explanations.
- **Strict Sandbox & Security Boundaries**:
  - workspace/data/: **Read-Only** (the immutable source of truth and knowledge base).
  - workspace/artifacts/: **Read, Write, and Edit** (where generated study artifacts reside).
  - **No Deletion Guarantee**: The agent is strictly prohibited from deleting any files or directories across all paths.
  - **Jail Containment**: All file operations are sandboxed strictly within ./workspace/; directory traversal outside this boundary is rejected.

---

## ??? Architecture

`mermaid
flowchart TD
    User([Student / Terminal]) <-->|CLI: studymate| CLI[Terminal UI / REPL]
    CLI <--> Core[ReAct Engine / State Machine]
    
    subgraph Harness["StudyMate Agent Harness"]
        Core <--> Context[Context Manager & Budget Guards]
        Core <--> LLM[LLM Client / InnKube Endpoint]
        Core <--> Registry[Tool Registry]
        Registry <--> Security[Security & Path Jail Policy]
        Security <--> FS[Filesystem Tools]
    end
    
    subgraph Sandbox["Workspace Sandbox"]
        FS -.->|Read Only| Data[("workspace/data/<br/>(Course Documents)")]
        FS <-->|Read / Write / Edit| Artifacts[("workspace/artifacts/<br/>(Study Artifacts)")]
    end
`

---

## ?? Project Structure

`	ext
project/group-11/
+-- config/
¦   +-- config.yaml             # Model, agent parameters, and tool sandbox settings
¦   +-- soul.md                 # Agent persona, directives, and output conventions
+-- src/
¦   +-- study_agent/
¦       +-- cli/                # Terminal interactive REPL (studymate)
¦       +-- config/             # Configuration schemas and YAML/env loader
¦       +-- core/               # ReAct loop, context manager, and state machine
¦       +-- llm/                # InnKube (OpenAI-compatible) client and test mocks
¦       +-- tools/              # Tool registry and sandboxed filesystem tools
¦       +-- memory/             # Persistent long-term memory (Week 2)
¦       +-- observability/      # Prometheus telemetry & lifecycle hooks (Week 3)
¦       +-- security/           # Multi-tier permission management (Week 3)
¦       +-- subagents/          # Specialized sub-agent coordination (Week 3)
+-- tests/
¦   +-- unit/                   # Unit tests (tools, parser, ReAct state transitions)
¦   +-- e2e/                    # End-to-end file and agent workflows
+-- workspace/
¦   +-- data/                   # Input study materials (Read-Only)
¦   +-- artifacts/              # Generated study artifacts (Read/Write/Edit)
+-- Dockerfile                  # Container definition (python:3.12-slim-bookworm)
+-- pyproject.toml              # Modern Python packaging & CLI entrypoint
+-- requirements.txt            # Locked project dependencies
`

---

## ?? Getting Started

### 1. Prerequisites
- Python 3.12+
- Docker or Podman (for containerized execution)

### 2. Local Setup
`ash
# Clone the repository and navigate to the project root
cd project/group-11

# Create and activate a Python 3.12 virtual environment
python -m venv venv
# On Windows:
.\venv\Scripts\Activate.ps1
# On Linux/macOS:
source venv/bin/activate

# Install dependencies and install package in editable mode
pip install -r requirements.txt
pip install -e .
`

### 3. Environment Configuration
Create a .env file from the provided template and insert your InnKube API token:
`ash
cp .env.example .env
`
Edit .env:
`env
INNKUBE_API_KEY=your_innkube_token_here
`

### 4. Running StudyMate
Once installed, launch the interactive agent terminal anytime using:
`ash
studymate
`

Alternatively, run via Python module:
`ash
python -m study_agent
`

---

## ?? Testing

Run the automated test suite with pytest:
`ash
# Run all tests
pytest

# Run with coverage report
pytest --cov=study_agent
`

---

## ?? Containerization (Docker / Podman)

To run StudyMate inside an isolated Debian-based container (python:3.12-slim-bookworm):

`ash
# Build the container image
podman build -t studymate .

# Run the agent interactively with mounted workspace
podman run -it --rm \
  -e INNKUBE_API_KEY="your_api_key" \
  -v ./workspace:/app/workspace \
  studymate
`

---

## ?? Roadmap & Milestones

- [x] **Week 1: Core ReAct Harness & Sandboxed File Tools**
  - Interactive terminal CLI (studymate)
  - YAML configuration with environment variable secret handling
  - Sandboxed file tools (create, read, modify, search; deletion prohibited)
  - Pytest unit & E2E suite; container setup
- [ ] **Week 2: Model Context Protocol (MCP) & Long-Term Memory**
  - Dynamic MCP client integration & runtime tool discovery
  - Custom non-trivial MCP tool extension
  - Persistent long-term memory across sessions
- [ ] **Week 3: Observability, Permissions & Sub-Agents**
  - Prometheus metrics instrumentation & dashboard
  - Multi-tier permission system (llow, deny, equire-user-confirmation)
  - Specialized sub-agents with task delegation on the same harness
