# OpenAdapt Agent

[![Build Status](https://github.com/OpenAdaptAI/openadapt-agent/workflows/Publish%20to%20PyPI/badge.svg?branch=main)](https://github.com/OpenAdaptAI/openadapt-agent/actions)
[![PyPI version](https://img.shields.io/pypi/v/openadapt-agent.svg)](https://pypi.org/project/openadapt-agent/)
[![Downloads](https://img.shields.io/pypi/dm/openadapt-agent.svg)](https://pypi.org/project/openadapt-agent/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.10+](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/downloads/)

Production execution engine for OpenAdapt GUI automation agents. Wraps trained models with safety gates, human-in-the-loop confirmation, session management, and audit logging.

## Overview

`openadapt-agent` bridges the gap between benchmark evaluation and real-world automation. While `openadapt-evals` focuses on measuring agent performance in controlled environments, `openadapt-agent` provides the runtime infrastructure needed to safely execute agents on real user systems.

**Key capabilities:**

- **Safety Gates**: Pre-action validation using pattern-based rules and ML-powered safety checks
- **Human-in-the-Loop**: Configurable confirmation for high-risk actions (CLI, GUI, or custom callbacks)
- **Session Management**: Pause, resume, and persist execution sessions
- **Audit Logging**: Comprehensive logging for compliance and debugging
- **Error Recovery**: Retry policies and graceful degradation

## Installation

```bash
pip install openadapt-agent
```

Or with uv:
```bash
uv add openadapt-agent
```

### Optional Dependencies

```bash
# With ML safety module (recommended)
pip install openadapt-agent[ml]

# With all integrations
pip install openadapt-agent[all]
```

## Quick Start

### Command Line

```bash
# Start an agent session
openadapt-agent start "Open Notepad and type hello world"

# Check session status
openadapt-agent status

# Pause/resume execution
openadapt-agent pause <session_id>
openadapt-agent resume <session_id>

# List all sessions
openadapt-agent session list

# Show agent info
openadapt-agent info
```

### Python API

```python
from openadapt_agent import AgentExecutor, AgentConfig, SafetyMode

# Configure the agent
config = AgentConfig(
    safety_mode=SafetyMode.STANDARD,  # Block dangerous, confirm irreversible
    confirmation_mode="cli",           # CLI prompts for confirmation
)

# Create executor (policy loaded from openadapt-ml)
executor = AgentExecutor(policy=my_trained_policy, config=config)

# Start a session
session = executor.start_session(goal="Open Notepad and type hello")

# Execute steps
while not session.is_complete:
    result = executor.step()
    if not result.success:
        print(f"Step failed: {result.error}")
        break

print(f"Session completed: {session.state}")
```

## Safety Modes

| Mode | Description |
|------|-------------|
| `DISABLED` | No safety checks (testing only) |
| `PERMISSIVE` | Log warnings but allow most actions |
| `STANDARD` | Block dangerous, confirm irreversible (default) |
| `STRICT` | Block suspicious, confirm most actions |
| `PARANOID` | Block everything not explicitly allowed |

## Architecture

```
openadapt-agent/
  src/openadapt_agent/
    __init__.py       # Package exports
    executor.py       # AgentExecutor - main execution engine
    session.py        # Session management and persistence
    config.py         # Configuration classes
    cli.py            # Command-line interface
```

### Integration with OpenAdapt Ecosystem

```
openadapt-agent
  |-- openadapt-ml (optional)      # Safety module, trained policies
  |-- openadapt-grounding (optional) # UI element detection
  |-- openadapt-capture (optional)   # Screen observation
  |-- openadapt-tray (optional)      # GUI confirmation dialogs
  |-- openadapt-evals (optional)     # Benchmark testing
```

## Session Management

Sessions can be paused, resumed, and persisted across process restarts:

```python
from openadapt_agent import SessionManager, SessionState

manager = SessionManager()

# List recent sessions
sessions = manager.list_sessions(limit=10)
for s in sessions:
    print(f"{s.session_id}: {s.state} - {s.goal}")

# Resume a paused session
session = manager.load_session("abc123...")
if session.state == SessionState.PAUSED:
    executor.resume_session(session.session_id)
```

## Configuration

Configuration can be set via environment variables (prefixed with `OPENADAPT_AGENT_`), config files, or the API:

```bash
# Environment variables
export OPENADAPT_AGENT_SAFETY_MODE=strict
export OPENADAPT_AGENT_CONFIRMATION_MODE=cli
```

```yaml
# config.yaml
safety_mode: strict
confirmation_mode: gui
execution:
  max_steps: 50
  step_timeout_seconds: 30
audit:
  enabled: true
  include_screenshots: true
```

```python
# Python API
from openadapt_agent import AgentConfig

config = AgentConfig.from_file("config.yaml")
# or
config = AgentConfig(safety_mode="strict")
```

## Custom Confirmation Handlers

```python
def my_confirmation_handler(action, reason):
    """Custom logic to approve/deny actions."""
    if "delete" in str(action).lower():
        return False  # Never allow delete
    # Show custom dialog, send webhook, etc.
    return True

executor = AgentExecutor(
    policy=my_policy,
    config=AgentConfig(confirmation_mode="callback"),
    confirmation_callback=my_confirmation_handler,
)
```

## Comparison: openadapt-agent vs openadapt-evals

| Aspect | openadapt-evals | openadapt-agent |
|--------|-----------------|-----------------|
| **Purpose** | Benchmark evaluation | Production automation |
| **Environment** | Controlled VMs, sandboxes | Real user machines |
| **Stakes** | Low (test data) | High (real data, actions) |
| **Human Oversight** | Optional, for debugging | Required, for safety |
| **Error Recovery** | Restart from checkpoint | Graceful degradation |
| **Logging** | Metrics collection | Audit trail, compliance |

## Development

```bash
# Clone and install for development
git clone https://github.com/OpenAdaptAI/openadapt-agent.git
cd openadapt-agent
uv sync

# Run tests
uv run pytest

# Type checking
uv run mypy src/
```

## License

MIT

## Related Projects

- [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) - Training and policy runtime
- [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) - Benchmark evaluation
- [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) - Screen recording
- [openadapt-grounding](https://github.com/OpenAdaptAI/openadapt-grounding) - UI element localization
- [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) - Main project and CLI
