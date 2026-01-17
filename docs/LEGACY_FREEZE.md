# Legacy OpenAdapt Codebase Freeze

**Date**: January 2026
**Status**: Legacy Codebase Frozen
**Last Version**: v0.46.0

---

## Decision Summary

The monolithic OpenAdapt codebase in this repository has been **frozen** as of version 0.46.0. Active development has moved to a new **modular meta-package architecture** that provides better maintainability, faster iteration, and cleaner separation of concerns.

**This codebase remains available for:**
- Reference and historical purposes
- Existing users who need to maintain current installations
- Migration planning and compatibility testing

**No new features will be added to this codebase.**

---

## Last Known Working Version

| Property | Value |
|----------|-------|
| **Version** | 0.46.0 |
| **PyPI Package** | `openadapt==0.46.0` |
| **Python Support** | 3.10 - 3.11 |
| **Release Date** | February 2025 |
| **Repository** | https://github.com/OpenAdaptAI/OpenAdapt |

**To install the legacy version:**
```bash
pip install openadapt==0.46.0
```

**Or clone and install from this repository:**
```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt.git
cd OpenAdapt
pip install poetry
poetry install
```

---

## New Modular Architecture

The new OpenAdapt ecosystem is split into focused, independently-versioned packages:

| Package | Description | Installation |
|---------|-------------|--------------|
| **openadapt** | Meta-package (unified CLI + re-exports) | `pip install openadapt` (v1.0.0+) |
| **openadapt-ml** | ML engine, training, inference | `pip install openadapt-ml` |
| **openadapt-capture** | Event recording and storage | `pip install openadapt-capture` |
| **openadapt-evals** | Benchmark evaluation infrastructure | `pip install openadapt-evals` |
| **openadapt-viewer** | HTML visualization components | `pip install openadapt-viewer` |
| **openadapt-grounding** | UI element localization (optional) | `pip install openadapt-grounding` |
| **openadapt-retrieval** | Multimodal demo retrieval (optional) | `pip install openadapt-retrieval` |

**Quick start with new packages:**
```bash
# Install everything
pip install openadapt[all]

# Or install what you need
pip install openadapt-ml openadapt-capture
```

---

## Migration Guide

### For Library Users

**Before (Legacy v0.46.0):**
```python
from openadapt.record import record
from openadapt.replay import replay
from openadapt.models import Recording, ActionEvent
from openadapt.strategies.visual import VisualReplayStrategy
```

**After (New Architecture v1.0.0+):**
```python
# Recording
from openadapt import CaptureSession, Recorder

# ML (training and inference)
from openadapt import AgentPolicy
from openadapt_ml.training import train_supervised

# Evaluation
from openadapt import ApiAgent, evaluate_agent_on_benchmark

# Viewer
from openadapt import PageBuilder
```

### For Application Users (Desktop Tray App)

The legacy PySide6-based tray application is **not included** in the new packages. Options:

1. **Continue using legacy**: Pin to `openadapt==0.46.0`
2. **Use CLI**: The new `openadapt` CLI provides all core functionality
3. **Future**: A new cross-platform application is planned

### For Contributors

**Legacy development setup:**
```bash
git clone https://github.com/OpenAdaptAI/OpenAdapt
cd OpenAdapt
poetry install
```

**New development setup:**
```bash
# Clone specific sub-package
git clone https://github.com/OpenAdaptAI/openadapt-ml
cd openadapt-ml
uv sync  # or pip install -e ".[dev]"
```

### Data Migration

**Database format changed:**

| Legacy | New |
|--------|-----|
| SQLite database (`openadapt.db`) | JSON/Parquet captures |
| `Recording` SQLAlchemy model | `Capture` Pydantic model |
| `ActionEvent` with foreign keys | `ActionEvent` standalone |

A migration script will be provided to convert legacy recordings to the new format.

### CLI Changes

| Legacy CLI | New CLI |
|------------|---------|
| `python -m openadapt.record "task"` | `openadapt capture start --name "task"` |
| `python -m openadapt.replay visual` | `openadapt replay --strategy visual` |
| `python -m openadapt.visualize` | `openadapt capture view <name>` |

### Python Version

| Legacy | New |
|--------|-----|
| Python 3.10 - 3.11 | Python 3.12+ |

---

## Benefits of the New Architecture

### 1. Faster Development Cycles
- Each package has independent CI/CD
- Changes to `openadapt-ml` don't require rebuilding `openadapt-capture`
- Smaller test suites per package = faster feedback

### 2. Cleaner Dependencies
- Legacy had 80+ direct dependencies
- New packages have minimal, focused dependencies
- Optional heavy dependencies (grounding, retrieval) are truly optional

### 3. Better Versioning
- Semantic versioning per package
- Breaking changes in ML don't force capture upgrades
- Easier to pin specific versions

### 4. Modern Python Support
- Python 3.12+ with modern typing
- Native async/await support
- Better performance

### 5. Easier Contribution
- Contributors can focus on one package
- Clear ownership and responsibility
- Smaller codebases to understand

### 6. Optional Features
- Install only what you need
- `openadapt[grounding]` for UI grounding
- `openadapt[retrieval]` for demo retrieval
- `openadapt[all]` for everything

---

## Frequently Asked Questions

### Q: Will the legacy package be removed from PyPI?
**A:** No. `openadapt==0.46.0` will remain available indefinitely. New versions (1.0.0+) will be the meta-package.

### Q: Can I still file issues against the legacy codebase?
**A:** Critical security fixes will be considered. New features should be requested against the new packages.

### Q: What about my existing recordings?
**A:** A migration tool will be provided. Contact us on Discord if you need help migrating.

### Q: Is the desktop tray app still supported?
**A:** The legacy tray app works with `openadapt==0.46.0`. A new cross-platform app is planned for the new architecture.

---

## Support

- **Discord**: https://discord.gg/yF527cQbDG
- **GitHub Issues**: https://github.com/OpenAdaptAI/OpenAdapt/issues (legacy)
- **New Packages Issues**: See individual package repositories

---

## Related Documents

- [Legacy Transition Plan](https://github.com/OpenAdaptAI/openadapt-evals/blob/main/docs/research/legacy-transition-plan.md)
- [New Architecture Design](https://github.com/OpenAdaptAI/openadapt-ml/blob/main/docs/new_openadapt_architecture.md)
- [OpenAdapt Meta-Package](https://github.com/OpenAdaptAI/openadapt) (coming soon)

---

*This document was created as part of the OpenAdapt modular architecture transition. The legacy codebase served the community well from 2023-2025 and its patterns inform the new design.*
