# Production Execution Design: From Benchmarks to Real-World Automation

**Version**: 1.0
**Date**: January 2026
**Status**: Proposal

---

## Executive Summary

This document addresses a critical gap in the OpenAdapt architecture: the **EXECUTE phase** is currently limited to benchmark evaluation (`openadapt-evals`), but production automation requires additional capabilities beyond measuring performance. This proposal synthesizes industry best practices from leading GUI automation frameworks to define what "production execution" means and how OpenAdapt should support it.

**Key Recommendation**: Create a new `openadapt-agent` package for production execution, keeping `openadapt-evals` focused on benchmarking. The safety module in `openadapt-ml` should be extracted and shared.

---

## Table of Contents

1. [Problem Statement](#1-problem-statement)
2. [Literature Review](#2-literature-review)
3. [Gap Analysis](#3-gap-analysis)
4. [Architectural Options](#4-architectural-options)
5. [Recommendation](#5-recommendation)
6. [README Improvement Proposal](#6-readme-improvement-proposal)
7. [Implementation Roadmap](#7-implementation-roadmap)
8. [References](#8-references)

---

## 1. Problem Statement {#1-problem-statement}

### Current Architecture

OpenAdapt's three-phase pipeline:

```
RECORD (capture) --> TRAIN (ml) --> EXECUTE (evals)
```

**The Problem**: `openadapt-evals` serves benchmark evaluation but lacks production execution capabilities.

| Capability | Benchmark Evaluation | Production Execution |
|-----------|---------------------|---------------------|
| **Purpose** | Measure agent performance | Automate real user tasks |
| **Environment** | Controlled VMs, sandboxes | Real user machines |
| **Stakes** | Low (test data) | High (real data, actions) |
| **Human Oversight** | Optional, for debugging | Required, for safety |
| **Error Recovery** | Restart from checkpoint | Graceful degradation, undo |
| **Logging** | Metrics collection | Audit trail, compliance |

### Why This Matters

1. **User Expectation Gap**: Users expect "execute" to mean "run my agent on my tasks," not just "benchmark my agent"
2. **Safety Requirements**: Production execution on real systems requires safety gates not needed in sandboxed benchmarks
3. **Feature Completeness**: Without production execution, OpenAdapt is a training/eval tool, not an automation solution

---

## 2. Literature Review {#2-literature-review}

### 2.1 Microsoft UFO (2024-2025)

**Overview**: UFO evolved through three versions from a GUI Agent (UFO, Feb 2024) to a Desktop AgentOS (UFO2, Apr 2025) to Multi-Device Orchestration (UFO3, Nov 2025).

**Key Architecture Patterns**:
- **HostAgent + AppAgents**: Centralized task decomposition with application-specialized execution
- **Hybrid Control Detection**: Fuses Windows UI Automation (UIA) with vision-based parsing
- **Picture-in-Picture (PiP)**: Isolated virtual desktop allowing agents and users to operate concurrently without interference

**Production vs Evaluation**:
- UFO2 evaluated across 20+ Windows applications, demonstrating improvements over "conceptual prototype" CUAs
- Most CUAs remain "hindered by shallow OS integration, fragile screenshot-based interaction, and disruptive execution"
- Windows Agent Arena (WAA) provides benchmark environment: 154 tasks across 15 applications

**Key Insight**: Deep OS integration unlocks scalable, reliable automation. Screenshot-only approaches are fragile.

**Source**: [UFO Documentation](https://microsoft.github.io/UFO/)

### 2.2 Claude Computer Use (Anthropic, 2024-2025)

**Overview**: Production-grade VLM agent API with explicit safety guardrails.

**Safety Architecture**:
- **Human-in-the-Loop**: Required for sensitive tasks (financial transfers, system deletions)
- **Hallucinated Click Problem**: Vision models misinterpret buttons or get stuck in loops
- **Tiered Workflows**:
  - Low-risk tasks (documentation): Highly autonomous
  - High-risk tasks (triage suggestions): Mandatory human review

**Production Safeguards**:
- Pre-deployment testing to verify training holds under pressure
- Detection methods for misuse (spam generation discovered pre-launch)
- Option to disable tool for accounts showing misuse
- Built-in VM isolation for Cowork desktop agent
- Support for browser automation with explicit approvals for high-impact actions

**Security Incidents**:
- Chinese state-backed hacking group used Claude for "first documented case of a large-scale cyberattack executed without substantial human intervention"
- Prompt injection attacks remain an active area of development

**Key Insight**: "Agent safety - the task of securing Claude's real-world actions - is still an active area of development."

**Source**: [Anthropic Building Safeguards for Claude](https://www.anthropic.com/news/building-safeguards-for-claude)

### 2.3 OSWorld (CMU, NeurIPS 2024)

**Overview**: First scalable, real computer environment for multimodal agents across Ubuntu, Windows, macOS.

**Benchmark Characteristics**:
- 369 computer tasks involving real web and desktop apps
- Open domains with OS file I/O and multi-application workflows
- Execution-based evaluation with reproducible setup

**Performance Reality Check**:
- Humans: 72.36% success
- Best AI model: 12.24% success (as of 2024)
- Agent S2 with Claude 3.7: 34.5% (as of late 2025)
- Primary failures: GUI grounding and operational knowledge

**Infrastructure**:
- Docker support for virtualized platforms
- AWS/Azure parallel evaluation (reduces evaluation to < 1 hour)
- Host-Client architecture for large-scale evaluation

**Key Insight**: Real-world computer tasks remain challenging. Best models achieve <35% of human performance.

**Source**: [OSWorld GitHub](https://github.com/xlang-ai/OSWorld)

### 2.4 WebArena & ST-WebAgentBench (2024)

**Overview**: WebArena provides web automation benchmarks; ST-WebAgentBench extends with safety/trust evaluation.

**Progress**:
- 2-year leap from 14% to ~60% success rate on WebArena
- Convergence on "standard model" architecture: Planner + Executor + Memory

**Safety Gap Identified**:
- Traditional benchmarks focus only on task success
- ST-WebAgentBench adds:
  - **Completion Under Policy (CuP)**: Success while following organizational rules
  - **Risk Ratio**: Proportion of unsafe actions
- Most state-of-the-art agents "not yet enterprise-ready" due to policy violations

**Key Insight**: Task success != production readiness. Policy compliance and trustworthiness are separate dimensions.

**Source**: [ST-WebAgentBench Paper](https://arxiv.org/html/2410.06703v2)

### 2.5 OpenCUA (2025)

**Overview**: Open-source framework achieving state-of-the-art among open-source models.

**Key Innovation**: Chain-of-Thought (CoT) augmentation of trajectories
- Planning, memory, and reflection in structured reasoning
- OpenCUA-72B: 45% on OSWorld-Verified (new SOTA for open-source)
- OpenCUA-32B: Surpassed GPT-4o-based CUA

**Agent Architecture**:
- Iterative perception via screenshots
- Reflective long CoT as inner monologue
- Predicted next action execution

**Source**: [OpenCUA GitHub](https://github.com/xlang-ai/OpenCUA)

### 2.6 Industry State (2025)

**Market Reality**:
- Global agentic AI market: $7.6 billion (2025)
- Only 2% of organizations deployed at scale
- 61% stuck in exploration phases
- Gartner: >40% projects will be canceled by 2027 due to costs, unclear value, or inadequate risk controls

**Key Insight**: The gap between experimentation and production is a primary blocker. Framework selection is an architectural decision, not a library decision.

---

## 3. Gap Analysis {#3-gap-analysis}

### 3.1 What OpenAdapt Has

| Component | Package | Status |
|-----------|---------|--------|
| **Safety Validator** | `openadapt-ml/safety/` | Implemented |
| **Pattern Detection** | `openadapt-ml/safety/patterns.py` | Implemented |
| **Confirmation Handlers** | `openadapt-ml/safety/confirmation.py` | Implemented |
| **Benchmark Runner** | `openadapt-evals/benchmarks/runner.py` | Implemented |
| **Execution Traces** | `openadapt-evals/benchmarks/data_collection.py` | Implemented |
| **Live Tracking** | `openadapt-evals/benchmarks/live_tracker.py` | Implemented |

### 3.2 What's Missing for Production

#### 3.2.1 Safety Gates (Partial)

**Exists** (`openadapt-ml/safety/`):
- `SafetyValidator`: Pattern-based action validation
- `SafetyConfig`: Mode configuration (DISABLED, PERMISSIVE, STANDARD, STRICT, PARANOID)
- Pattern categories: DESTRUCTIVE, SYSTEM, IRREVERSIBLE, CREDENTIAL, NETWORK, FILE_SYSTEM
- `ConfirmationHandler`: CLI, callback, queue-based confirmation

**Missing**:
- Integration with execution loop (exists in isolation, not wired to runner)
- GUI confirmation handlers (falls back to CLI with warning)
- Learning from confirmations (user overrides don't persist across sessions)
- Dynamic risk assessment based on context

#### 3.2.2 Human-in-the-Loop Confirmation

**Exists**:
- `CLIConfirmationHandler`: Text-based prompts
- `CallbackConfirmationHandler`: Custom integration hooks
- `QueueConfirmationHandler`: Batch processing

**Missing**:
- GUI dialog integration (noted as TODO)
- Tray application integration for desktop notifications
- Mobile/web confirmation for remote operation
- Timeout policies (exists but crude - blocks entire execution)
- Escalation paths (always block if no confirmation)

#### 3.2.3 Rollback/Undo Capability

**Completely Missing**:
- No state snapshots before action execution
- No undo registry for reversible actions
- No checkpoint/restore mechanism
- No transaction boundaries

**Industry Comparison**:
- LangGraph: Built-in checkpointers saving state after each step
- UFO: PiP isolation prevents interference with user session
- Claude Desktop: VM isolation for Cowork agent

**Real-World Incidents Showing Need**:
- Replit AI deleted user's production database, then incorrectly advised rollback was impossible
- Gemini CLI agent deleted user files with no recovery

#### 3.2.4 Session Management

**Partially Exists** (scattered):
- `ValidationContext` tracks state visits and user overrides
- `EvaluationConfig` has run_name and output_dir

**Missing**:
- Persistent session store (sessions lost on restart)
- Session pause/resume
- Session sharing/handoff
- Session templates for common workflows
- Session isolation (multi-user scenarios)

#### 3.2.5 Error Recovery

**Exists** (basic):
- Try/except in runner with error logging
- `BenchmarkResult.error` field for failure capture

**Missing**:
- Retry policies (exponential backoff, max retries)
- Fallback strategies (alternative action paths)
- Degraded mode operation (continue with reduced capabilities)
- Error categorization (transient vs permanent)
- Recovery suggestions

#### 3.2.6 Logging/Audit Trail

**Exists** (for benchmarks):
- Execution traces with screenshots
- Metrics collection
- Live tracking JSON

**Missing for Production**:
- Structured audit logs (who, what, when, why)
- Compliance-ready formats (SOC2, HIPAA considerations)
- Log retention policies
- Tamper-evident logging
- Real-time alerting on anomalies

### 3.3 Capability Matrix

| Capability | Benchmark (evals) | Production (needed) | Gap |
|-----------|------------------|--------------------|----|
| Execute actions | Yes | Yes | - |
| Safety validation | No (in ml) | Yes | Integration |
| Human confirmation | No | Yes | New |
| Rollback/undo | No | Yes | New |
| Session persistence | No | Yes | New |
| Error recovery | Basic | Advanced | Enhancement |
| Audit logging | Basic | Compliance-grade | Enhancement |
| Isolation | Sandbox assumed | User system | Architecture |
| Concurrent operation | No | Yes (PiP-style) | New |

---

## 4. Architectural Options {#4-architectural-options}

### Option A: Rename/Expand openadapt-evals

**Approach**: Add production execution capabilities to `openadapt-evals`, rename to `openadapt-execute` or keep name.

**Pros**:
- Single package for all execution
- Simpler dependency graph
- Existing infrastructure (runner, traces, tracking)

**Cons**:
- Violates single-responsibility principle
- Benchmark users get production deps they don't need
- Production users get benchmark deps they don't need
- Name confusion: "evals" implies evaluation, not execution
- Risk of breaking existing benchmark integrations

**Package Structure**:
```
openadapt-evals/
  benchmarks/        # Keep existing
  production/        # New
    runner.py        # Production execution loop
    safety.py        # Wire safety from ml
    session.py       # Session management
    recovery.py      # Error recovery
  shared/            # Common utilities
```

### Option B: Create openadapt-agent

**Approach**: New package specifically for production agent execution.

**Pros**:
- Clear separation of concerns
- Users install what they need
- Independent release cycles
- "agent" is industry-standard terminology
- Can depend on both evals (for runner patterns) and ml (for safety)

**Cons**:
- Another package to maintain
- Some code duplication initially
- Users may confuse agent vs evals

**Package Structure**:
```
openadapt-agent/
  runtime/
    executor.py      # Production execution loop
    session.py       # Session management
    recovery.py      # Error recovery
  safety/            # Extract from ml or import
    validator.py
    confirmation.py
    patterns.py
  persistence/
    state.py         # Checkpoint/restore
    audit.py         # Audit logging
  ui/
    confirmation.py  # GUI confirmation dialogs
    tray.py          # System tray integration
```

**Dependency Graph**:
```
openadapt-agent
  ├── openadapt-ml (for safety, models)
  ├── openadapt-grounding (for element detection)
  ├── openadapt-capture (for observation capture)
  └── openadapt-evals (optional, for testing)
```

### Option C: Keep Execution in openadapt-ml

**Approach**: Add production execution to `openadapt-ml` as part of the model runtime.

**Pros**:
- Safety module already there
- Policy and execution naturally coupled
- Fewer packages overall

**Cons**:
- `ml` becomes a monolith
- Users wanting production execution must install training deps
- Naming confusion: "ml" doesn't suggest execution
- Benchmark evals would need to depend on ml for production features

**Package Structure**:
```
openadapt-ml/
  runtime/           # Existing
    policy.py
  execution/         # New
    runner.py
    session.py
    recovery.py
  safety/            # Existing
    validator.py
    confirmation.py
```

### Option D: Hybrid - Shared Safety Package

**Approach**: Extract safety into `openadapt-safety`, create `openadapt-agent` for execution.

**Pros**:
- Maximum modularity
- Safety can be used by any package
- Clear responsibility boundaries

**Cons**:
- Most complex dependency graph
- Another package to publish/maintain
- Possibly over-engineered for current scale

**Dependency Graph**:
```
openadapt-safety (new)
  └── (minimal deps)

openadapt-agent (new)
  ├── openadapt-safety
  ├── openadapt-grounding
  └── openadapt-capture

openadapt-ml
  └── openadapt-safety

openadapt-evals
  └── openadapt-safety (optional)
```

---

## 5. Recommendation {#5-recommendation}

### Primary Recommendation: Option B (openadapt-agent)

**Rationale**:

1. **Clear Terminology**: "agent" is the industry-standard term for autonomous execution systems
2. **Separation of Concerns**: Benchmarking and production execution have different requirements
3. **User Clarity**: `pip install openadapt-agent` clearly communicates intent
4. **Safety Integration**: Can import from `openadapt-ml` initially, extract later if needed
5. **Pragmatic**: Avoids over-engineering while maintaining modularity

### Phase 1: Create openadapt-agent (Q1 2026)

```
openadapt-agent/
  __init__.py
  runtime/
    __init__.py
    executor.py         # Production execution loop
    session.py          # Session management
    state.py            # Checkpoint/restore
  safety/
    __init__.py         # Re-exports from openadapt-ml
  recovery/
    __init__.py
    retry.py            # Retry policies
    fallback.py         # Fallback strategies
  persistence/
    __init__.py
    audit.py            # Structured audit logging
    store.py            # Session persistence
  cli.py                # CLI entry point
```

**Minimal Dependencies**:
```toml
[project]
dependencies = [
  "openadapt-ml",       # For safety, models
  "openadapt-grounding", # For element detection
  "openadapt-capture",   # For observations
]

[project.optional-dependencies]
tray = ["openadapt-tray"]  # Desktop integration
full = ["openadapt-evals"]  # Include benchmark testing
```

### Phase 2: Enhance Safety Integration

1. Wire `SafetyValidator` into production executor
2. Add GUI confirmation handler via `openadapt-tray`
3. Implement user override persistence
4. Add risk-based escalation paths

### Phase 3: Implement Rollback/Undo

1. State snapshots before action execution
2. Undo registry for reversible actions
3. Checkpoint/restore mechanism
4. Transaction boundaries

### CLI Integration

Update main `openadapt` CLI:

```
openadapt agent start --task "Process invoices"     # Start production agent
openadapt agent status                               # Check running agent
openadapt agent pause                                # Pause execution
openadapt agent resume                               # Resume execution
openadapt agent stop                                 # Stop agent

openadapt agent session list                         # List sessions
openadapt agent session resume <id>                  # Resume session

openadapt eval run --benchmark waa                   # Benchmarking (unchanged)
```

---

## 6. README Improvement Proposal {#6-readme-improvement-proposal}

### Current Issues

1. **Too Verbose**: 443 lines, overwhelming for newcomers
2. **Buried Value Prop**: Core innovation (demo-conditioned automation) lost in details
3. **Phase Confusion**: EXECUTE = evals, not production automation
4. **Package Overload**: 7 packages listed upfront

### Proposed Structure

```markdown
# OpenAdapt: Show, Don't Tell

**Learn from demonstrations. Automate with AI.**

OpenAdapt watches you work, learns your patterns, and automates your tasks.

## Quick Start

```bash
pip install openadapt[all]

# 1. Record
openadapt capture start --name my-task

# 2. Train
openadapt train --capture my-task

# 3. Run
openadapt agent start --task my-task
```

## How It Works

```
[Record Demo] --> [Train Model] --> [Run Agent]
     |                  |                |
   Capture          Learn from       Automate
   your actions     your patterns    your tasks
```

**The key insight**: Instead of writing prompts, you *demonstrate*. The AI learns from what you do, not what you say.

## Installation

| Goal | Install Command |
|------|-----------------|
| Everything | `pip install openadapt[all]` |
| Just record & train | `pip install openadapt[core]` |
| Evaluate against benchmarks | `pip install openadapt[evals]` |
| Production automation | `pip install openadapt[agent]` |

## Packages

| Phase | Package | Purpose |
|-------|---------|---------|
| **RECORD** | openadapt-capture | Screen/event capture |
| **TRAIN** | openadapt-ml | Model training |
| **EXECUTE** | openadapt-agent | Production automation |
| | openadapt-evals | Benchmark evaluation |

*Optional*: openadapt-grounding, openadapt-retrieval, openadapt-privacy, openadapt-viewer

## Safety

Production automation includes:
- Pre-action validation (dangerous pattern detection)
- Human-in-the-loop confirmation for high-risk actions
- Audit logging for compliance
- Session management with pause/resume

## Links

- [Documentation](https://docs.openadapt.ai)
- [Discord](https://discord.gg/yF527cQbDG)
- [Architecture](../architecture-evolution.md)

## License

MIT
```

### Key Changes

1. **"Show, Don't Tell" as Headline**: Captures the core differentiator
2. **3-Step Quick Start**: Immediate value demonstration
3. **Visual Flow**: ASCII diagram showing the pipeline
4. **Clear Phase-to-Package Mapping**: EXECUTE includes both agent AND evals
5. **Safety Section**: Addresses enterprise concerns upfront
6. **Reduced Length**: ~100 lines vs 443

---

## 7. Implementation Roadmap {#7-implementation-roadmap}

### Q1 2026: Foundation

| Week | Deliverable |
|------|-------------|
| 1-2 | Create `openadapt-agent` package skeleton |
| 3-4 | Implement basic `Executor` with safety integration |
| 5-6 | Add session management (start, pause, resume, stop) |
| 7-8 | CLI integration and documentation |

### Q2 2026: Production Features

| Week | Deliverable |
|------|-------------|
| 1-2 | GUI confirmation handlers via tray |
| 3-4 | Checkpoint/restore mechanism |
| 5-6 | Retry policies and fallback strategies |
| 7-8 | Audit logging for compliance |

### Q3 2026: Enterprise Readiness

| Week | Deliverable |
|------|-------------|
| 1-4 | Concurrent operation (PiP-style isolation) |
| 5-8 | Undo registry for reversible actions |

### Success Metrics

| Metric | Target |
|--------|--------|
| Time to first successful production run | < 10 minutes |
| Safety validation coverage | 100% of high-risk actions |
| Session recovery success rate | > 95% |
| User confirmation response time | < 30 seconds |

---

## 8. References {#8-references}

### GUI Automation Frameworks

1. **Microsoft UFO**: [Documentation](https://microsoft.github.io/UFO/) | [GitHub](https://github.com/microsoft/UFO)
2. **Claude Computer Use**: [Anthropic Safety](https://www.anthropic.com/news/building-safeguards-for-claude)
3. **OSWorld**: [Paper](https://arxiv.org/abs/2404.07972) | [GitHub](https://github.com/xlang-ai/OSWorld)
4. **WebArena**: [Website](https://webarena.dev/)
5. **ST-WebAgentBench**: [Paper](https://arxiv.org/html/2410.06703v2)
6. **OpenCUA**: [GitHub](https://github.com/xlang-ai/OpenCUA)

### Safety Research

7. **Remediation: What happens after AI goes wrong?**: [Jack Vanlightly Blog](https://jack-vanlightly.com/blog/2025/7/28/remediation-what-happens-after-ai-goes-wrong)
8. **Agentic AI Frameworks: Complete Enterprise Guide 2025**: [SpaceO](https://www.spaceo.ai/blog/agentic-ai-frameworks/)

### OpenAdapt Internal

9. **Architecture Evolution**: `/docs/architecture-evolution.md`
10. **Safety Module**: `/openadapt-ml/openadapt_ml/safety/`
11. **Evaluation Runner**: `/openadapt-evals/openadapt_evals/benchmarks/runner.py`

---

## Appendix A: Safety Module Analysis

The existing safety module in `openadapt-ml/openadapt_ml/safety/` provides:

### Validation Flow

```python
from openadapt_ml.safety import SafetyValidator, SafetyConfig, ValidationDecision

validator = SafetyValidator(SafetyConfig())

result = validator.validate(action, observation)

if result.allowed:
    execute(action)
elif result.requires_confirmation:
    if confirm_with_user(result):
        execute(action)
else:  # blocked
    log_blocked_action(result.reason)
```

### Pattern Categories

| Category | Examples | Default Behavior |
|----------|----------|------------------|
| DESTRUCTIVE | rm, delete, drop, format | Block |
| SYSTEM | sudo, admin, reboot | Block |
| IRREVERSIBLE | send, submit, purchase | Confirm |
| CREDENTIAL | password, secret, key | Confirm |
| NETWORK | upload, post, send | Confirm |
| FILE_SYSTEM | write, modify, create | Allow (log) |

### Safety Modes

| Mode | Description |
|------|-------------|
| DISABLED | Bypass all safety (testing only) |
| PERMISSIVE | Log warnings, allow most |
| STANDARD | Block dangerous, confirm irreversible |
| STRICT | Block suspicious, confirm most |
| PARANOID | Block everything not explicitly allowed |

---

## Appendix B: Glossary

| Term | Definition |
|------|------------|
| **CUA** | Computer-Using Agent - AI system that controls desktop/web UIs |
| **HITL** | Human-in-the-Loop - requiring human approval for actions |
| **PiP** | Picture-in-Picture - isolated virtual desktop for agent operation |
| **Rollback** | Reverting system state to a previous checkpoint |
| **Session** | A bounded execution context with state persistence |
| **Safety Gate** | Pre-action validation layer preventing harmful operations |

---

*Document prepared by Claude Opus 4.5 for OpenAdapt project. Last updated: January 2026.*
