# Claude Code Instructions for openadapt-agent

## Overview

Production execution engine for OpenAdapt GUI automation agents. Provides the runtime infrastructure to safely execute trained models in production environments with safety gates, human-in-the-loop confirmation, session management, and audit logging.

**Key distinction**: `openadapt-evals` is for benchmark evaluation, `openadapt-agent` is for production execution on real systems.

## Quick Start

```bash
# Install
uv sync

# Run CLI
uv run python -m openadapt_agent.cli --help

# Start a session
uv run python -m openadapt_agent.cli start "Open Notepad and type hello"

# Check status
uv run python -m openadapt_agent.cli status

# List sessions
uv run python -m openadapt_agent.cli session list
```

## Architecture

```
src/openadapt_agent/
    __init__.py       # Package exports, optional safety import from openadapt-ml
    executor.py       # AgentExecutor - main execution engine with safety validation
    session.py        # Session/SessionManager - persistence and state management
    config.py         # AgentConfig, SafetyMode, ConfirmationMode
    cli.py            # Click-based CLI
```

### Key Classes

| Class | File | Description |
|-------|------|-------------|
| `AgentExecutor` | executor.py | Main execution engine with safety, confirmation, audit |
| `Session` | session.py | Execution session with state, history, persistence |
| `SessionManager` | session.py | Session creation, loading, saving, listing |
| `AgentConfig` | config.py | Main configuration (pydantic-settings) |
| `SafetyMode` | config.py | Enum: DISABLED, PERMISSIVE, STANDARD, STRICT, PARANOID |

### Integration Points

**Optional dependencies (graceful degradation):**
- `openadapt-ml` - Safety module (`SafetyValidator`) and trained policies
- `openadapt-grounding` - UI element detection
- `openadapt-capture` - Screen observation capture
- `openadapt-tray` - GUI confirmation dialogs
- `openadapt-evals` - Benchmark testing

```python
# Check if safety module is available
from openadapt_agent import has_safety_module
if has_safety_module():
    from openadapt_agent import SafetyValidator
```

## Safety Modes

| Mode | Behavior |
|------|----------|
| DISABLED | No safety checks (testing only) |
| PERMISSIVE | Log warnings, allow most actions |
| STANDARD | Block dangerous, confirm irreversible |
| STRICT | Block suspicious, confirm most |
| PARANOID | Block everything not explicitly allowed |

## Key Files

| File | Description |
|------|-------------|
| `executor.py` | AgentExecutor, Action, Observation, ExecutionResult |
| `session.py` | Session, SessionManager, SessionState |
| `config.py` | AgentConfig, SafetyMode, ConfirmationMode, ExecutionConfig |
| `cli.py` | Click CLI: start, status, pause, resume, stop, session list/show/delete |

## Design Decisions

1. **Standalone by default**: Works without openadapt-ml but integrates when available
2. **Pydantic-settings for config**: Environment variables with OPENADAPT_AGENT_ prefix
3. **Session persistence**: JSON files in ~/.openadapt/agent/sessions/
4. **Protocol-based policy interface**: Any class with `predict()` and `reset()` works
5. **Pluggable confirmation**: CLI, GUI (via openadapt-tray), callback, or auto modes

## TODO / Implementation Roadmap

### Phase 1: Foundation (Current)
- [x] Package skeleton
- [x] Basic AgentExecutor with safety validation stubs
- [x] Session management with persistence
- [x] CLI with start/stop/pause/resume/list
- [ ] Integration with openadapt-ml safety module
- [ ] Integration with openadapt-capture for observations

### Phase 2: Production Features
- [ ] GUI confirmation via openadapt-tray
- [ ] Checkpoint/restore mechanism
- [ ] Retry policies with exponential backoff
- [ ] Audit logging with retention policies

### Phase 3: Enterprise
- [ ] Concurrent operation (PiP-style isolation)
- [ ] Undo registry for reversible actions
- [ ] Compliance-ready logging formats

## Development

```bash
# Install dev dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/ -v

# Type checking (when mypy is added)
uv run mypy src/

# Linting
uv run ruff check src/
```

## Related Documentation

- Design document: `/Users/abrichr/oa/src/OpenAdapt/docs/design/production-execution-design.md`
- Safety module: `openadapt-ml/openadapt_ml/safety/`
- Evaluation runner: `openadapt-evals/openadapt_evals/benchmarks/runner.py`

## Environment Variables

| Variable | Description |
|----------|-------------|
| `OPENADAPT_AGENT_SAFETY_MODE` | Safety enforcement mode |
| `OPENADAPT_AGENT_CONFIRMATION_MODE` | How to handle confirmations |
| `OPENADAPT_AGENT_SESSION_DIR` | Session storage directory |

## Don't Do

- Don't bypass safety checks in production code
- Don't use raw action execution without validation
- Don't store sensitive data in session metadata
- Don't push directly to main - always use PRs
