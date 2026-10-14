# Legacy OpenAdapt to Meta-Package Transition Plan

## Executive Summary

This document outlines the transition strategy for moving from the monolithic legacy `openadapt` repository to the new meta-package architecture. The new architecture splits functionality into focused sub-packages (`openadapt-ml`, `openadapt-capture`, `openadapt-evals`, `openadapt-viewer`, `openadapt-grounding`, `openadapt-retrieval`) with a unified meta-package providing a single entry point.

---

## 1. Current State Analysis

### 1.1 Legacy Repository (`github.com/OpenAdaptAI/openadapt`)

**PyPI Status:**
- Package name: `openadapt`
- Current version: 0.46.0 (Released Feb 20, 2025)
- Author: Richard Abrich (`abrichr`)
- Python support: >=3.10, <3.12
- Dependencies: 80+ direct dependencies (Poetry-managed)
- GitHub stars: ~1,400+

**Core Functionality in Legacy Repo:**

| Module | Description | Lines of Code | Migration Status |
|--------|-------------|---------------|------------------|
| `record.py` | Event recording with multiprocessing | ~1,400 | Partially in openadapt-capture |
| `models.py` | SQLAlchemy ORM models | ~1,200 | NOT migrated (needs work) |
| `replay.py` | Strategy-based replay | ~130 | NOT migrated |
| `strategies/` | Replay strategies (Visual, Vanilla, etc.) | ~85,000 | NOT migrated |
| `config.py` | Pydantic settings | ~350 | Partial in meta-package |
| `db/` | Database CRUD operations | ~500+ | NOT migrated |
| `privacy/` | PII scrubbing (Presidio, AWS, PrivateAI) | ~400 | In openadapt-privacy (separate) |
| `app/` | System tray app & dashboard | ~30,000+ | NOT migrated |
| `utils.py` | Shared utilities | ~900 | Partially duplicated |
| `video.py` | Video encoding/decoding | ~350 | In openadapt-capture |
| `vision.py` | OCR and image processing | ~500 | Partially in openadapt-ml |
| `plotting.py` | Performance visualization | ~800 | NOT migrated |
| `adapters/` | SoM, Replicate, Ultralytics | ~400 | In openadapt-grounding |
| `drivers/` | OpenAI, Anthropic, Google clients | ~300 | In openadapt-ml |
| `alembic/` | Database migrations | ~20 migrations | NOT migrated |

### 1.2 New Meta-Package Architecture (`/Users/abrichr/oa/src/openadapt-new`)

**Package Structure:**
```
openadapt (meta-package)
├── openadapt-ml        (ML engine, training, inference)
├── openadapt-capture   (Event recording, storage, processing)
├── openadapt-evals     (Benchmark evaluation infrastructure)
├── openadapt-viewer    (HTML visualization components)
├── openadapt-grounding (UI element localization) [optional]
└── openadapt-retrieval (Multimodal demo retrieval) [optional]
```

**New Meta-Package Features:**
- Python >=3.12 support
- Modern `pyproject.toml` with hatchling
- Unified CLI (`openadapt capture`, `openadapt train`, `openadapt eval`, etc.)
- Clean re-exports from sub-packages
- Optional extras: `[grounding]`, `[retrieval]`, `[all]`

### 1.3 Functionality Gap Analysis

**Already Migrated (Complete):**
- Event capture & storage (`openadapt-capture`)
- ML training & inference (`openadapt-ml`)
- Benchmark evaluation (`openadapt-evals`)
- HTML visualization (`openadapt-viewer`)
- UI grounding (`openadapt-grounding`)
- Demo retrieval (`openadapt-retrieval`)

**NOT Migrated (Gaps):**

| Legacy Component | Priority | Notes |
|------------------|----------|-------|
| **SQLAlchemy models** | HIGH | Recording, ActionEvent, Screenshot, WindowEvent, etc. |
| **Database layer** | HIGH | CRUD operations, Alembic migrations |
| **Replay strategies** | HIGH | Visual, Vanilla, Segment, Stateful, etc. |
| **System tray app** | MEDIUM | PySide6-based GUI |
| **Dashboard** | MEDIUM | FastAPI + web frontend |
| **Privacy scrubbing** | MEDIUM | Separate `openadapt-privacy` exists |
| **Plotting** | LOW | Performance visualization |
| **Build utilities** | LOW | PyInstaller bundling |

---

## 2. PyPI Transition Strategy

### 2.1 Options Analysis

#### Option A: Version Bump with Deprecation Warning (RECOMMENDED)

**Approach:**
1. Release `openadapt` v0.47.0 with deprecation warning
2. Update to depend on new sub-packages
3. Release `openadapt` v1.0.0 as the new meta-package

**Pros:**
- Maintains package name ownership
- Seamless upgrade path for existing users
- No namespace conflicts
- Preserves download statistics and SEO

**Cons:**
- Users may have pinned versions
- Need to maintain compatibility layer temporarily

**Implementation:**
```python
# In legacy openadapt v0.47.0 __init__.py
import warnings
warnings.warn(
    "openadapt is transitioning to a meta-package architecture. "
    "Please update your imports. See https://docs.openadapt.ai/migration",
    DeprecationWarning,
    stacklevel=2
)

# Gradually add re-exports from new packages
try:
    from openadapt_capture import CaptureSession, Recorder
    from openadapt_ml import AgentPolicy, train_supervised
except ImportError:
    pass  # Fallback to legacy
```

#### Option B: New Package Name (NOT Recommended)

**Approach:** Publish as `openadapt2` or `openadapt-core`

**Pros:**
- Clean break
- No legacy baggage

**Cons:**
- Loses PyPI namespace
- Confusing for users
- SEO disadvantage
- Someone else could claim `openadapt`

#### Option C: Yank Old Versions (NOT Recommended)

**Approach:** Remove old versions from PyPI

**Pros:**
- Forces migration

**Cons:**
- Breaks existing installations
- Bad user experience
- Against PyPI best practices

### 2.2 Recommended PyPI Timeline

| Phase | Version | Timeline | Action |
|-------|---------|----------|--------|
| 1 | 0.47.0 | Week 1-2 | Add deprecation warning, announce transition |
| 2 | 0.48.0 | Week 3-4 | Start re-exporting from new sub-packages |
| 3 | 0.49.0 | Week 5-6 | Full compatibility layer, document migration |
| 4 | 1.0.0 | Week 7-8 | Meta-package release, drop legacy code |

---

## 3. Repository Strategy

### 3.1 Options

#### Option A: Fork to `openadapt-legacy` (RECOMMENDED)

**Steps:**
1. Create `github.com/OpenAdaptAI/openadapt-legacy` as archive
2. Continue development in `OpenAdaptAI/openadapt` with new architecture
3. Update README to redirect users

**Pros:**
- Preserves git history
- Legacy users can still access old code
- Main repo URL stays familiar

**Cons:**
- Need to maintain redirect
- Two repos to manage temporarily

#### Option B: Rename in Place

**Steps:**
1. Rename main repo to `openadapt-legacy`
2. Create new `openadapt` repo from meta-package

**Pros:**
- Clean break

**Cons:**
- Breaks all existing GitHub links, stars, forks
- Loses SEO value
- Disrupts CI/CD integrations

#### Option C: Keep Monorepo (Hybrid)

**Steps:**
1. Move legacy code to `legacy/` directory
2. Add new structure at root level
3. Use monorepo tooling (e.g., Turborepo, Nx)

**Pros:**
- Single repo
- Gradual migration

**Cons:**
- Complex build setup
- Confusing structure
- Dependencies become complex

### 3.2 Recommended Repository Plan

```
# Create legacy archive
git clone git@github.com:OpenAdaptAI/openadapt.git openadapt-legacy
cd openadapt-legacy
git remote set-url origin git@github.com:OpenAdaptAI/openadapt-legacy.git
git push -u origin main

# Update main repo
cd ../openadapt
# Replace contents with meta-package
# Keep .git history
cp -r ../openadapt-new/* .
git add .
git commit -m "chore: transition to meta-package architecture"
git push
```

---

## 4. Migration Path for Users

### 4.1 For Library Users (pip install openadapt)

**Before (v0.46.0):**
```python
from openadapt.record import record
from openadapt.replay import replay
from openadapt.models import Recording, ActionEvent
from openadapt.strategies.visual import VisualReplayStrategy
```

**After (v1.0.0):**
```python
# Recording
from openadapt import CaptureSession, Recorder

# ML
from openadapt import AgentPolicy, train_supervised

# Evaluation
from openadapt import ApiAgent, evaluate_agent_on_benchmark

# Legacy replay strategies (if needed)
# pip install openadapt-legacy
from openadapt_legacy.strategies.visual import VisualReplayStrategy
```

### 4.2 For Application Users (Desktop App)

The desktop tray application will need separate migration:

1. **Short-term:** Continue using legacy app
2. **Medium-term:** Build new Electron/Tauri app using new packages
3. **Long-term:** Deprecate PyQt-based tray app

### 4.3 For Contributors

**Development Setup Change:**

**Before:**
```bash
git clone https://github.com/OpenAdaptAI/openadapt
cd openadapt
poetry install
```

**After:**
```bash
git clone https://github.com/OpenAdaptAI/openadapt
cd openadapt
uv sync  # or pip install -e ".[dev]"
```

**Sub-package Development:**
```bash
# Clone specific sub-package
git clone https://github.com/OpenAdaptAI/openadapt-ml
cd openadapt-ml
uv sync
```

---

## 5. Breaking Changes

### 5.1 Import Path Changes

| Legacy Import | New Import |
|--------------|------------|
| `from openadapt.record import record` | `from openadapt import Recorder` |
| `from openadapt.models import Recording` | `from openadapt_capture import Capture` |
| `from openadapt.models import ActionEvent` | `from openadapt import ActionEvent` |
| `from openadapt.strategies.visual import VisualReplayStrategy` | `from openadapt_ml import AgentPolicy` (different paradigm) |
| `from openadapt.vision import get_text_from_image` | `from openadapt_ml.perception import ocr` |
| `from openadapt.adapters.som import segment` | `from openadapt_grounding import OmniParserClient` |

### 5.2 Data Format Changes

| Legacy | New | Migration |
|--------|-----|-----------|
| SQLite database (`openadapt.db`) | JSON/Parquet captures | Converter script needed |
| `Recording` SQLAlchemy model | `Capture` Pydantic model | Schema mapping |
| `ActionEvent` with FKs | `ActionEvent` standalone | Flatten relationships |
| Video stored separately | Video embedded in capture | Archive integration |

### 5.3 CLI Changes

| Legacy CLI | New CLI |
|-----------|---------|
| `python -m openadapt.record "task"` | `openadapt capture start --name "task"` |
| `python -m openadapt.replay visual` | `openadapt replay --strategy visual` (TBD) |
| `visualize` | `openadapt capture view <name>` |

### 5.4 Python Version

| Legacy | New |
|--------|-----|
| Python 3.10-3.11 | Python 3.12+ |

---

## 6. Risk Assessment

### 6.1 High Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Database migration failure** | Users lose recordings | Provide backup tool, reversible migrations |
| **PyPI name conflict** | Unable to publish | We own the namespace already |
| **Replay strategies broken** | Core feature unusable | Maintain compatibility layer, test extensively |

### 6.2 Medium Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Documentation lag** | User confusion | Prioritize docs, migration guide |
| **CI/CD breakage** | Build failures | Set up new pipelines before cutover |
| **Community resistance** | Slow adoption | Clear communication, long deprecation period |

### 6.3 Low Risk

| Risk | Impact | Mitigation |
|------|--------|------------|
| **Performance regression** | Slower operations | Benchmark before/after |
| **Missing edge cases** | Some imports fail | Comprehensive import testing |

---

## 7. Timeline Recommendation

### Phase 1: Preparation (Weeks 1-2)
- [ ] Create `openadapt-legacy` archive repository
- [ ] Document all legacy imports and their new equivalents
- [ ] Create database migration scripts
- [ ] Set up CI/CD for meta-package
- [ ] Write migration guide documentation

### Phase 2: Soft Launch (Weeks 3-4)
- [ ] Release `openadapt` v0.47.0 with deprecation warnings
- [ ] Publish announcement on GitHub, Discord, blog
- [ ] Monitor for community feedback
- [ ] Fix any discovered issues

### Phase 3: Feature Parity (Weeks 5-8)
- [ ] Complete replay strategy migration (if needed)
- [ ] Complete database migration tooling
- [ ] Release v0.48.0, v0.49.0 with incremental changes
- [ ] Gather user feedback on migration experience

### Phase 4: Full Transition (Weeks 9-12)
- [ ] Release `openadapt` v1.0.0 as pure meta-package
- [ ] Archive legacy code
- [ ] Update all documentation
- [ ] Deprecate legacy-specific features

### Phase 5: Cleanup (Weeks 13+)
- [ ] Remove compatibility shims in v1.1.0+
- [ ] Finalize `openadapt-legacy` as read-only
- [ ] Complete migration guide with success stories

---

## 8. Checklist for Go-Live

### Technical Readiness
- [ ] All sub-packages published to PyPI
- [ ] Meta-package installs correctly
- [ ] All re-exports work
- [ ] CLI commands functional
- [ ] Tests pass (>90% coverage)
- [ ] Documentation complete

### Communication Readiness
- [ ] Migration guide published
- [ ] Blog post prepared
- [ ] Discord announcement ready
- [ ] GitHub release notes written
- [ ] Email to known enterprise users (if any)

### Support Readiness
- [ ] FAQ prepared
- [ ] Issue template for migration issues
- [ ] Rollback procedure documented
- [ ] Support channel monitored

---

## 9. Decision Matrix

### Question: Should we fork to openadapt-legacy?

| Factor | Fork | In-place Rename | Monorepo |
|--------|------|-----------------|----------|
| Preserves URLs | Yes | No | Yes |
| Clean separation | Yes | Yes | No |
| Ease of migration | High | Low | Medium |
| Maintenance burden | Medium | Low | High |
| User confusion | Low | High | Medium |
| **Recommendation** | **YES** | No | No |

### Question: What happens to existing users/installs?

1. **Pinned versions** (`openadapt==0.46.0`): Continue working, no change
2. **Unpinned** (`openadapt`): Get deprecation warning, then meta-package
3. **Fresh installs**: Get latest (meta-package after v1.0.0)

### Question: Should we rename in place?

**No.** Renaming breaks:
- GitHub links in blog posts, papers, tutorials
- CI/CD integrations
- Forks and stars count
- SEO ranking
- User muscle memory

---

## 10. Appendix

### A. Complete Module Inventory (Legacy)

```
openadapt/
├── __init__.py           # Package init
├── adapters/             # Vision model adapters
│   ├── prompt.py
│   ├── replicate.py
│   ├── som.py
│   └── ultralytics.py
├── alembic/              # Database migrations
├── app/                  # Desktop application
│   ├── dashboard/        # Web dashboard
│   ├── tray.py           # System tray
│   └── assets/
├── browser.py            # Browser automation
├── build.py              # Build scripts
├── cache.py              # Caching utilities
├── capture/              # Capture module (newer)
├── common.py             # Shared constants
├── config.py             # Configuration
├── contrib/              # Community contributions
├── custom_logger.py      # Logging
├── data/                 # Data utilities
├── db/                   # Database layer
├── drivers/              # LLM API drivers
├── entrypoint.py         # CLI entry
├── error_reporting.py    # Sentry integration
├── events.py             # Event definitions
├── extensions/           # Plugin system
├── models.py             # SQLAlchemy ORM
├── playback.py           # Event playback
├── plotting.py           # Visualization
├── privacy/              # PII scrubbing
├── productivity.py       # Metrics
├── prompts/              # LLM prompts
├── record.py             # Recording engine
├── replay.py             # Replay engine
├── scrub.py              # Scrubbing
├── share.py              # Recording sharing
├── spacy_model_helpers/  # NLP utilities
├── start.py              # Startup
├── strategies/           # Replay strategies
├── utils.py              # Utilities
├── video.py              # Video handling
├── vision.py             # Computer vision
├── visualize.py          # Recording viz
└── window/               # Window management
```

### B. Sub-Package PyPI Names

| Package | PyPI Name | Status |
|---------|-----------|--------|
| ML Engine | `openadapt-ml` | Published |
| Capture | `openadapt-capture` | Published |
| Evaluation | `openadapt-evals` | Published |
| Viewer | `openadapt-viewer` | Published |
| Grounding | `openadapt-grounding` | Published |
| Retrieval | `openadapt-retrieval` | Published |
| Privacy | `openadapt-privacy` | Published (separate) |
| Meta-Package | `openadapt` | **To be transitioned** |

### C. Environment Variable Changes

| Legacy | New |
|--------|-----|
| `OPENADAPT_OPENAI_API_KEY` | `OPENAI_API_KEY` |
| `OPENADAPT_ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY` |
| `OPENADAPT_DB_URL` | N/A (file-based captures) |
| N/A | `OPENADAPT_CAPTURE_DIR` |
| N/A | `OPENADAPT_TRAINING_OUTPUT_DIR` |

---

## 11. Conclusion

The transition from legacy `openadapt` to the meta-package architecture is achievable with careful planning. The recommended approach is:

1. **PyPI Strategy:** Version bump with deprecation (v0.47.0 -> v1.0.0)
2. **Repository Strategy:** Fork to `openadapt-legacy`, keep main repo URL
3. **Timeline:** 12-week phased transition
4. **User Impact:** Minimal with proper communication and compatibility layer

The modular architecture provides clear benefits:
- Faster CI/CD cycles per package
- Independent versioning
- Easier contribution
- Optional dependencies
- Modern Python support

**Next Steps:**
1. Approve this plan with stakeholders
2. Create migration scripts for database
3. Set up `openadapt-legacy` repository
4. Begin Phase 1 preparation work
