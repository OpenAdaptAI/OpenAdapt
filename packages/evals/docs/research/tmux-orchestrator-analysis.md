# Tmux-Orchestrator Analysis for OpenAdapt Ecosystem

**Author:** Research Analysis
**Date:** January 16, 2026
**Status:** Research Document

---

## Executive Summary

This document analyzes [Tmux-Orchestrator](https://github.com/Jedward23/Tmux-Orchestrator), a multi-agent AI orchestration framework, and evaluates its applicability to the OpenAdapt ecosystem development workflow.

**Key Finding:** Tmux-Orchestrator addresses a real problem (multi-project AI agent coordination) but has significant limitations for our use case. Better alternatives exist that integrate more seamlessly with our existing tools (uv, Claude Code, GitHub).

**Recommendation:** Do not adopt Tmux-Orchestrator. Instead, consider **Claude Code PM (CCPM)** for GitHub Issues-based orchestration, or **parallel-claude** for simpler multi-repo parallelization.

---

## 1. What is Tmux-Orchestrator?

### 1.1 Overview

Tmux-Orchestrator is a framework that enables Claude AI agents to work autonomously across multiple projects using tmux terminal sessions. It implements a hierarchical agent structure:

```
Orchestrator (coordination layer)
    |
    +-- Project Manager A (assigns tasks, enforces specs)
    |       |
    |       +-- Engineer A1 (implements code)
    |       +-- Engineer A2 (implements code)
    |
    +-- Project Manager B
            |
            +-- Engineer B1
```

### 1.2 Problem It Solves

Traditional AI agents face two core limitations:

1. **Context Window Exhaustion**: Single agents lose track of complex, multi-part tasks
2. **Session Persistence**: Work is lost when terminals close

Tmux-Orchestrator addresses these by:

- **Distributing work** across specialized agents (each with focused context)
- **Persisting sessions** via tmux (terminals survive disconnection)
- **Self-scheduling** via cron-like scripts (agents trigger their own check-ins)

### 1.3 Key Components

| Component | Purpose |
|-----------|---------|
| `send-claude-message.sh` | Inter-agent communication via tmux |
| `schedule_with_note.sh` | Agent self-scheduling for autonomous check-ins |
| `tmux_utils.py` | Python utilities for tmux interaction |
| `CLAUDE.md` | Agent behavior instructions |
| `LEARNINGS.md` | Accumulated knowledge and patterns |

### 1.4 How It Works

1. **Setup**: Create tmux sessions for orchestrator, project managers, and engineers
2. **Communication**: Agents send messages to each other via `send-claude-message.sh`
3. **Scheduling**: Agents schedule their own check-ins (e.g., every 30 minutes)
4. **Git Discipline**: Mandatory commits every 30 minutes to preserve work

---

## 2. Applicability to OpenAdapt Ecosystem

### 2.1 OpenAdapt Package Structure

The OpenAdapt ecosystem consists of 7+ interrelated packages:

| Package | Purpose | Dependencies |
|---------|---------|--------------|
| `openadapt-ml` | ML engine, VLM adapters, training pipeline | openadapt-capture |
| `openadapt-capture` | Screen capture and event recording | - |
| `openadapt-evals` | Benchmark evaluation infrastructure | openadapt-retrieval (optional) |
| `openadapt-viewer` | Visualization and replay tools | - |
| `openadapt-retrieval` | Demo retrieval for RAG agents | - |
| `openadapt-grounding` | UI element localization | - |
| `openadapt-new` | New core orchestration | Various |

### 2.2 Current Development Workflow

Based on analysis of the codebase:

- **Package Management**: `uv` for dependency management and virtual environments
- **Build System**: `hatchling` for all packages
- **Testing**: `pytest` with `ruff` for linting
- **Cloud**: Azure VMs for live WAA evaluation, Lambda Labs for GPU training
- **CLI Tools**: Rich CLI interfaces in each package (e.g., `openadapt_evals.benchmarks.cli`)

### 2.3 Mapping Tmux-Orchestrator to OpenAdapt

| Tmux-Orchestrator Concept | OpenAdapt Mapping |
|---------------------------|-------------------|
| Orchestrator | Master agent coordinating cross-package work |
| Project Managers | Per-package agents (ml, evals, grounding, etc.) |
| Engineers | Task-specific agents within a package |
| send-claude-message.sh | Inter-agent communication |

**Potential workflow:**

```
Orchestrator: "Run full integration test across ml, evals, and grounding"
    |
    +-- PM (openadapt-ml): "Run VLM training tests"
    |       +-- Engineer: Runs pytest, reports results
    |
    +-- PM (openadapt-evals): "Run benchmark mock tests"
    |       +-- Engineer: Runs CLI, reports results
    |
    +-- PM (openadapt-grounding): "Test OmniParser deployment"
            +-- Engineer: Runs deploy commands, reports status
```

---

## 3. Critical Evaluation

### 3.1 Strengths

| Strength | Benefit for OpenAdapt |
|----------|----------------------|
| Session persistence | Long-running Azure VM operations survive disconnection |
| Hierarchical structure | Maps well to our multi-package architecture |
| Git discipline | Aligns with our existing commit practices |
| Self-scheduling | Could automate periodic benchmark runs |

### 3.2 Weaknesses and Concerns

| Concern | Impact | Severity |
|---------|--------|----------|
| **macOS-only shell scripts** | Would need rewriting for team members on other platforms | Medium |
| **No GitHub integration** | Our workflow relies on GitHub Issues, PRs, Actions | High |
| **tmux complexity** | Additional infrastructure to maintain | Medium |
| **No uv support** | Scripts assume pip/manual installs | Medium |
| **Fragile inter-agent messaging** | Shell-based message passing is error-prone | High |
| **No parallelism control** | No concept of resource limits or queue management | High |
| **Limited error handling** | Agent failures can cascade silently | High |

### 3.3 Specific Incompatibilities

1. **Azure VM Management**: Our current CLI (`vm monitor`, `vm diag`, etc.) expects single-agent interaction. Tmux-Orchestrator's message-passing would conflict.

2. **uv Package Management**: All our packages use `uv sync` and `uv run`. Tmux-Orchestrator has no awareness of this.

3. **Claude Code Integration**: We already use Claude Code with `CLAUDE.md` files. Tmux-Orchestrator's approach duplicates this without the IDE integration benefits.

4. **SSH Tunnel Management**: Our `SSHTunnelManager` class handles VNC/WAA tunnels programmatically. Tmux-Orchestrator would need custom integration.

---

## 4. Alternatives Analysis

### 4.1 Claude Code PM (CCPM)

**Repository:** https://github.com/automazeio/ccpm

**Overview:** Project management system for Claude Code using GitHub Issues and Git worktrees for parallel agent execution.

| Feature | Benefit |
|---------|---------|
| GitHub Issues as database | Full visibility for team, easy handoffs |
| Git worktrees | Parallel execution without branch conflicts |
| PRD-to-task decomposition | Spec-driven development aligns with our approach |
| Claude Code integration | Works with existing `CLAUDE.md` files |

**Mapping to OpenAdapt:**

```
/pm:prd-new          Create PRD for new feature (e.g., "Add WebArena support")
/pm:epic-decompose   Break into tasks across packages
/pm:epic-sync        Push to GitHub Issues with labels
/pm:issue-start      Agents work on issues in parallel
```

**Pros:**
- Native GitHub integration (our primary workflow)
- Transparent to team members (no hidden tmux sessions)
- Supports 5-8 parallel tasks (their reported experience)
- Works with our existing Claude Code setup

**Cons:**
- Newer project, less battle-tested
- Requires GitHub Issues discipline

### 4.2 parallel-claude

**Repository:** https://github.com/saadnvd1/parallel-claude

**Overview:** CLI tool to spawn multiple Claude Code agents across repos with iTerm2 integration.

| Feature | Benefit |
|---------|---------|
| Multi-repo spawning | `parallel-claude spawn repo -t "task1" -t "task2"` |
| Automatic Git branches | Each worker gets isolated branch |
| iTerm2 split panes | Visual monitoring of all workers |
| Port orchestration | Prevents dev server conflicts |

**Mapping to OpenAdapt:**

```bash
# Spawn workers for each package
parallel-claude spawn https://github.com/OpenAdaptAI/openadapt-ml -t "Add WebArena adapter"
parallel-claude spawn https://github.com/OpenAdaptAI/openadapt-evals -t "Update CLI for new adapter"
parallel-claude spawn https://github.com/OpenAdaptAI/openadapt-grounding -t "Test with new benchmark"
```

**Pros:**
- Simple, single-purpose tool
- Works with existing iTerm2 workflow (macOS)
- Auto-creates feature branches
- Minimal setup required

**Cons:**
- macOS/iTerm2 only
- No inter-agent communication
- No orchestration layer

### 4.3 Claude Code --add-dir

**Built-in Feature:** Claude Code's `--add-dir` flag for multi-directory context.

```bash
claude --add-dir /path/to/openadapt-ml --add-dir /path/to/openadapt-evals
```

**Pros:**
- No additional tools needed
- Single context window across packages
- Already available

**Cons:**
- Context window limits still apply
- Sequential work, not parallel
- Best for cross-package analysis, not execution

### 4.4 Comparison Matrix

| Feature | Tmux-Orchestrator | CCPM | parallel-claude | --add-dir |
|---------|------------------|------|-----------------|-----------|
| GitHub Integration | No | Yes (native) | Limited | No |
| Parallel Execution | Yes | Yes | Yes | No |
| uv Compatibility | Manual | Requires setup | Manual | Native |
| Team Visibility | Low (tmux) | High (GitHub) | Low (local) | N/A |
| Setup Complexity | High | Medium | Low | None |
| Cross-Platform | Partial | Yes | macOS only | Yes |
| Orchestration | Yes | Yes | No | No |
| Our Current Tools | Incompatible | Compatible | Partially | Compatible |

---

## 5. Recommendations

### 5.1 Primary Recommendation: Do NOT Adopt Tmux-Orchestrator

**Rationale:**
1. High integration cost with our existing tooling
2. Limited visibility for team collaboration
3. Better alternatives exist with GitHub integration

### 5.2 Alternative Approach: Hybrid Strategy

**For Cross-Package Development:**

1. **Use Claude Code `--add-dir`** for analysis and planning across packages
2. **Use CCPM** for task decomposition and GitHub-based tracking
3. **Use parallel-claude** (or manual tmux sessions) for parallel execution

**Example Workflow:**

```bash
# 1. Analyze scope with multi-directory context
claude --add-dir /Users/abrichr/oa/src/openadapt-ml \
       --add-dir /Users/abrichr/oa/src/openadapt-evals \
       --add-dir /Users/abrichr/oa/src/openadapt-grounding

# 2. Create PRD and decompose with CCPM
/pm:prd-new "Add WebArena benchmark support"
/pm:epic-decompose

# 3. Execute in parallel (each agent on one package)
# Option A: parallel-claude for automated spawning
# Option B: Manual tmux sessions with our existing CLI commands
```

### 5.3 If Orchestration Is Required

If hierarchical agent orchestration becomes necessary:

1. **Evaluate CCPM first** - it provides orchestration with better GitHub integration
2. **Consider custom implementation** - adapt Tmux-Orchestrator's concepts to our CLI structure
3. **Integrate with existing CLIs** - our `vm monitor`, `vm diag`, etc. should be the execution layer

### 5.4 Immediate Actions

1. **No adoption of Tmux-Orchestrator** at this time
2. **Experiment with CCPM** on a small feature to evaluate fit
3. **Document multi-repo workflow** for team members using existing tools
4. **Consider parallel-claude** if macOS-only is acceptable for developer tooling

---

## 6. Appendix

### 6.1 Related Links

- [Tmux-Orchestrator GitHub](https://github.com/Jedward23/Tmux-Orchestrator)
- [CCPM (Claude Code PM)](https://github.com/automazeio/ccpm)
- [parallel-claude](https://github.com/saadnvd1/parallel-claude)
- [Claude Code --add-dir Documentation](https://claudelog.com/faqs/--add-dir/)
- [Polyrepo Synthesis with Claude Code](https://rajiv.com/blog/2025/11/30/polyrepo-synthesis-synthesis-coding-across-multiple-repositories-with-claude-code-in-visual-studio-code/)

### 6.2 AI Agent Orchestration Frameworks (2025-2026)

For reference, the broader ecosystem of multi-agent frameworks includes:

| Framework | Focus | Terminal/CLI Support |
|-----------|-------|---------------------|
| LangGraph | Low-latency multi-agent workflows | Limited |
| CrewAI | Role-based agent teams | Yes |
| AutoGen / Microsoft Agent Framework | Enterprise multi-agent systems | Yes |
| Google ADK | Agent development with CLI/Web UI | Yes |
| OpenAI Agents SDK | Lightweight orchestration with handoffs | Limited |

### 6.3 OpenAdapt Package Dependencies

```
openadapt-new
    |
    +-- openadapt-ml
    |       +-- openadapt-capture
    |
    +-- openadapt-evals
    |       +-- openadapt-retrieval (optional)
    |
    +-- openadapt-grounding
    |
    +-- openadapt-viewer
```

---

## Sources

- [Tmux-Orchestrator GitHub](https://github.com/Jedward23/Tmux-Orchestrator)
- [CCPM (Claude Code PM)](https://github.com/automazeio/ccpm)
- [parallel-claude](https://github.com/saadnvd1/parallel-claude)
- [Top AI Agent Frameworks 2026](https://www.lindy.ai/blog/best-ai-agent-frameworks)
- [AI Agent Orchestration Frameworks Guide](https://blog.n8n.io/ai-agent-orchestration-frameworks/)
- [Polyrepo Synthesis with Claude Code](https://rajiv.com/blog/2025/11/30/polyrepo-synthesis-synthesis-coding-across-multiple-repositories-with-claude-code-in-visual-studio-code/)
- [Claude Code Parallel Agents](https://medium.com/@joe.njenga/how-im-using-claude-code-parallel-agents-to-blow-up-my-workflows-460676bf38e8)
- [Better Multi-project Workflow Issue](https://github.com/anthropics/claude-code/issues/4707)
