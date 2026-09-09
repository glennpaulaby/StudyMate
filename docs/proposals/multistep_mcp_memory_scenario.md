# Multi-Step Tool Usage Scenario Proposal: Autonomous Exam Prep & Knowledge Expansion

## 1. Executive Summary

This proposal defines a realistic multi-step benchmark scenario demonstrating StudyMate's advanced agentic capabilities required for **Week 2 Milestone Verification**. The scenario validates autonomous multi-step reasoning across three required core pillars:
1. **Existing Core / Filesystem & Web Search Tools** (`read_pdf`, `read_file`, `research_topic` web research via search provider).
2. **Custom MCP Server Capabilities** (Knowledge Graph & Vault tools via `FastMCP`: `obsidian_search_notes`, `obsidian_read_note`, `obsidian_get_outgoing_links`, `obsidian_get_backlinks`, `obsidian_insert_into_section`, and `obsidian_write_note`).
3. **Long-Term Memory Integration** (Episodic chat-thread continuity, durable preference extraction via `memory_store`, and semantic vector + keyword hybrid recall via `memory_recall`).

---

## 2. Realistic User Story & Scenario Walkthrough

### Context
A computer science student is preparing for an upcoming Algorithms & Data Structures exam. In a previous study session (Session 1), the student noted that they struggle with the differences and trade-offs between **Dijkstra's Algorithm** and the **Bellman-Ford Algorithm**, especially regarding negative edge weights and cycles.

In the new session (Session 2), the student asks StudyMate:
> *"I need to review Dijkstra vs. Bellman-Ford for my exam. Please personalize this based on what I struggled with earlier, inspect our Obsidian vault notes, fetch external insights on negative cycle detection, enrich the vault notes with those insights, and update my memory profile."*

```mermaid
sequenceDiagram
    autonumber
    actor Student
    participant Engine as ReAct Agent Engine
    participant Memory as Long-Term Memory (SQLite + Vector)
    participant CoreTools as Core & Web Research Tools
    participant CustomMCP as Custom Obsidian MCP Server

    Student->>Engine: Request personalized review on Dijkstra vs Bellman-Ford
    Engine->>Memory: memory_recall("dijkstra bellman-ford struggle")
    Memory-->>Engine: "Struggles with negative weight cycles & edge relaxation in Bellman-Ford"
    Engine->>CustomMCP: obsidian_search_notes("dijkstra")
    CustomMCP-->>Engine: Found `concepts/dijkstra.md`
    Engine->>CustomMCP: obsidian_read_note("concepts/dijkstra.md")
    CustomMCP-->>Engine: Note content (mentions non-negative weights only)
    Engine->>CustomMCP: obsidian_get_outgoing_links("concepts/dijkstra.md")
    CustomMCP-->>Engine: Outgoing links: ["graphs", "shortest-path"]
    Engine->>CoreTools: research_topic("Dijkstra vs Bellman-Ford negative weight cycles")
    CoreTools-->>Engine: Web sources explaining distance relaxation & cycle detection
    Engine->>CustomMCP: obsidian_insert_into_section("concepts/dijkstra.md", "Key Intuition", "Fails on negative edges; use [[bellman-ford]] instead.")
    CustomMCP-->>Engine: Section successfully updated
    Engine->>CustomMCP: obsidian_write_note("concepts/negative-cycles.md", "---\ntitle: Negative Cycles\nstatus: current\n---\n# Negative Cycles\n...")
    CustomMCP-->>Engine: Note created
    Engine->>Memory: memory_store("exam-prep", "Mastered negative cycle detection differences between Dijkstra and Bellman-Ford")
    Memory-->>Engine: Stored fact #2
    Engine-->>Student: Comprehensive explanation addressing their specific weakness with cross-linked notes
```

---

## 3. Multi-Step Execution Trace (Tool Calls & Reasoning)

| Step | Component | Tool Invoked | Arguments | Expected Outcome |
|---|---|---|---|---|
| **Step 1: Context Recall** | Long-Term Memory | `memory_recall` | `{"query": "dijkstra struggle"}` | Agent retrieves user's historical struggle: *"Student struggles with negative edge cycles and why Dijkstra fails on negative weights."* |
| **Step 2: Vault Inspection** | Custom MCP Server | `obsidian_search_notes` | `{"query": "dijkstra"}` | Locates `concepts/dijkstra.md` in the local study vault. |
| **Step 3: Vault Reading** | Custom MCP Server | `obsidian_read_note` | `{"path": "concepts/dijkstra.md"}` | Reads current note; identifies lack of depth on negative weight cycle handling. |
| **Step 4: Knowledge Graph Traversal** | Custom MCP Server | `obsidian_get_outgoing_links` | `{"path": "concepts/dijkstra.md"}` | Checks linked concepts (e.g. `[[graphs]]`, `[[shortest-path]]`). |
| **Step 5: External Web Research** | Search Provider / MCP | `research_topic` | `{"topic": "Dijkstra vs Bellman-Ford negative weight cycles", "max_results": 3, "depth": 1}` | Fetches web explanations, structured markdown snippets, and source references. |
| **Step 6: Targeted Vault Enrichment** | Custom MCP Server | `obsidian_insert_into_section` | `{"path": "concepts/dijkstra.md", "heading": "Key Intuition", "content": "- Fails with negative edge weights; use [[bellman-ford]] instead."}` | Enriches existing note without corrupting YAML frontmatter or existing content. |
| **Step 7: New Concept Note Creation** | Custom MCP Server | `obsidian_write_note` | `{"path": "concepts/negative-cycles.md", "content": "---\ntitle: Negative Cycles\nstatus: current\n---\n..."}` | Creates new connected note in compliance with vault standards. |
| **Step 8: Memory Consolidation** | Long-Term Memory | `memory_store` | `{"topic": "exam-prep", "fact": "Mastered negative cycle detection differences between Dijkstra and Bellman-Ford", "tags": "graphs,exam,dijkstra"}` | Persists new milestone in episodic memory database (`memory.db`). |
| **Step 9: Final Response** | ReAct Agent Engine | *None (Final Turn)* | `N/A` | Delivers structured Socratic explanation, summarizes vault edits, and confirms memory updates. |

---

## 4. Verification & Testing Implementation

This scenario is formally implemented and automated as an End-to-End benchmark test in [`tests/e2e/test_mcp_memory_scenario.py`](file:///C:/Users/LOQ/OneDrive/Desktop/codes/2026/AI/group-11/tests/e2e/test_mcp_memory_scenario.py).

Key Assertions:
1. **Memory Recalled & Stored**: Verify that `memory_recall` returned pre-seeded historical struggles and `memory_store` persisted the new milestone.
2. **MCP Vault Modifications**: Verify that `concepts/dijkstra.md` was enriched via `obsidian_insert_into_section` and `concepts/negative-cycles.md` was written.
3. **Outgoing & Backlink Graph Integrity**: Verify that links between notes are cleanly extracted and valid.
4. **No-Delete & Sandbox Containment**: All file operations respect jail boundaries without deletions.
