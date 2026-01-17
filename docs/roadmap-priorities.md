# OpenAdapt Roadmap - Priorities

**Last Updated**: January 17, 2026
**Version**: 1.1.0
**Status**: Active Development

---

## Executive Summary

This document outlines the prioritized roadmap for OpenAdapt, focusing on ensuring the modular meta-package architecture is stable, functional, and delivers on the core promise: **Record -> Train -> Evaluate** GUI automation workflows.

---

## Current State Assessment

### PyPI Packages Published

| Package | Version | Python | Status |
|---------|---------|--------|--------|
| `openadapt` | 1.0.0 (meta) | >=3.10 | Published |
| `openadapt-capture` | 0.1.0 | >=3.10 | Published |
| `openadapt-ml` | 0.2.0 | >=3.12 | Published |
| `openadapt-evals` | 0.1.0 | >=3.10 | Published |
| `openadapt-viewer` | 0.1.0 | >=3.10 | Published |
| `openadapt-grounding` | 0.1.0 | >=3.10 | Published |
| `openadapt-retrieval` | 0.1.0 | >=3.10 | Published |
| `openadapt-privacy` | 0.1.0 | >=3.10 | Published |

**Note**: `openadapt-ml` requires Python 3.12+, which may cause compatibility issues with other packages requiring 3.10.

### CI/Test Status

- **Main repo**: CI runs on macOS and Ubuntu, Python 3.10/3.11/3.12
- **Lint check**: `ruff check` and `ruff format --check` - **Currently Passing**
- **Tests verified**:
  - `openadapt-grounding`: 53 tests passing
  - `openadapt-retrieval`: 28 tests passing
- **Known issues**: PR #969 addresses ruff format, Docker build needs verification

### Meta-Package Structure

The `openadapt` meta-package v1.0.0 uses:
- Hatchling build system
- Lazy imports to avoid heavy dependencies
- Optional extras: `[capture]`, `[ml]`, `[evals]`, `[viewer]`, `[grounding]`, `[retrieval]`, `[privacy]`, `[core]`, `[all]`

---

## Priority Definitions

| Priority | Urgency | Timeframe | Description |
|----------|---------|-----------|-------------|
| **P0** | Critical | This week | Blockers preventing basic functionality |
| **P1** | High | 1-2 weeks | Core feature completion, essential for v1.0 |
| **P2** | Medium | This month | Important enhancements, user experience |
| **P3** | Lower | Backlog | Nice to have, future considerations |

---

## P0 - Critical: Blocking Issues

### 1. Fix CI - Ruff Format (PR #969)

| Field | Value |
|-------|-------|
| **Status** | In Progress |
| **Effort** | Small (1-2 hours) |
| **Owner** | TBD |
| **PR** | #969 |
| **Branch** | `fix/ruff-format-config` |

**Description**: The CI workflow runs `ruff format --check openadapt/` which may fail if code is not formatted. A fix branch exists with formatting applied.

**Current State**: Local `ruff check` passes. Branch `fix/ruff-format-config` contains formatting fixes.

**Next Actions**:
- [ ] Review and merge PR #969
- [ ] Verify CI passes on all Python versions (3.10, 3.11, 3.12)
- [ ] Verify CI passes on all platforms (macOS, Ubuntu)

**Files**:
- `.github/workflows/main.yml`
- `openadapt/config.py`
- `openadapt/cli.py`

---

### 2. Fix Docker Build

| Field | Value |
|-------|-------|
| **Status** | Needs Investigation |
| **Effort** | Medium (2-4 hours) |
| **Owner** | TBD |
| **Location** | `legacy/deploy/deploy/models/omniparser/Dockerfile` |

**Description**: Docker build for OmniParser server may have issues. This is used for the grounding provider integration.

**Next Actions**:
- [ ] Test `docker build` for OmniParser Dockerfile
- [ ] Verify CUDA/GPU support works correctly
- [ ] Test model download during build (huggingface-cli)
- [ ] Document any missing dependencies or configuration

**Files**:
- `legacy/deploy/deploy/models/omniparser/Dockerfile`

---

### 3. Verify Meta-Package Installs Correctly

| Field | Value |
|-------|-------|
| **Status** | Needs Testing |
| **Effort** | Medium (2-4 hours) |
| **Owner** | TBD |

**Description**: Critical compatibility issue - `openadapt-ml` requires Python 3.12+, but `openadapt-capture` and others require 3.10+. Need to verify `pip install openadapt[all]` works.

**Test Matrix**:

| Installation | Python 3.10 | Python 3.11 | Python 3.12 |
|-------------|-------------|-------------|-------------|
| `openadapt` | Test | Test | Test |
| `openadapt[capture]` | Test | Test | Test |
| `openadapt[ml]` | Expected Fail | Expected Fail | Test |
| `openadapt[core]` | Expected Fail | Expected Fail | Test |
| `openadapt[all]` | Expected Fail | Expected Fail | Test |

**Next Actions**:
- [ ] Test `pip install openadapt[all]` on Python 3.12
- [ ] Test `pip install openadapt[core]` on Python 3.12
- [ ] Verify imports work: `python -c "from openadapt.cli import main"`
- [ ] Document minimum Python version clearly (3.12 if ml is needed)
- [ ] Consider downgrading `openadapt-ml` requirements to 3.10+ if feasible

---

### 4. Basic Capture -> Train -> Eval Workflow

| Field | Value |
|-------|-------|
| **Status** | Needs End-to-End Testing |
| **Effort** | Large (4-8 hours) |
| **Owner** | TBD |

**Description**: The core value proposition requires this workflow to function:

```bash
openadapt capture start --name my-task   # 1. Record demo
openadapt train start --capture my-task   # 2. Train model
openadapt eval run --checkpoint model.pt  # 3. Evaluate
```

**CLI Commands to Test**:

| Command | Status | Notes |
|---------|--------|-------|
| `openadapt capture start` | Needs Test | Requires macOS permissions |
| `openadapt capture list` | Needs Test | |
| `openadapt capture view <name>` | Needs Test | Generates HTML |
| `openadapt capture stop` | TODO | Uses Ctrl+C currently |
| `openadapt train start` | Needs Test | Requires openadapt-ml |
| `openadapt eval run --agent api-claude` | Needs Test | Requires API key |
| `openadapt eval mock --tasks 10` | Needs Test | Quick verification |

**Next Actions**:
- [ ] Test `openadapt capture start` on macOS (permissions required)
- [ ] Test `openadapt capture list` shows recordings
- [ ] Test `openadapt capture view <name>` generates HTML
- [ ] Test `openadapt train start` with real capture data
- [ ] Test `openadapt eval run --agent api-claude` with API key
- [ ] Test `openadapt eval mock --tasks 10` for quick verification
- [ ] Document any failures and create issues

**Known Blockers**:
- `capture stop` is TODO (uses Ctrl+C currently)
- macOS requires Accessibility + Screen Recording permissions

---

## P1 - High: Core Features

### 5. Complete Baseline Adapters

| Field | Value |
|-------|-------|
| **Status** | Partially Implemented |
| **Effort** | Medium (4-8 hours) |
| **Owner** | TBD |
| **Package** | `openadapt-ml` |

**Description**: API baseline adapters (Anthropic, OpenAI, Google) are implemented but need testing and validation.

**Adapter Status**:

| Provider | Adapter | Status | Notes |
|----------|---------|--------|-------|
| Anthropic | Claude | Implemented | Claude Computer Use patterns |
| OpenAI | GPT-4V | Implemented | Needs testing |
| Google | Gemini | Implemented | Needs testing |
| Qwen | Qwen3-VL | Implemented | Local model |

**Next Actions**:
- [ ] Test Anthropic adapter with Claude API
- [ ] Test OpenAI adapter with GPT-4V
- [ ] Test Google adapter with Gemini
- [ ] Verify prompts follow SOTA patterns (Claude CU, UFO, OSWorld)
- [ ] Add error handling for rate limits and API failures
- [ ] Document adapter usage and configuration

---

### 6. Demo Conditioning Integration in Evals

| Field | Value |
|-------|-------|
| **Status** | Designed, Needs Integration |
| **Effort** | Medium (4-8 hours) |
| **Owner** | TBD |
| **Packages** | `openadapt-retrieval`, `openadapt-evals` |

**Description**: Demo-conditioned prompting shows **33% -> 100% first-action accuracy improvement**. This is a key differentiator.

**Architecture**:
```
openadapt-retrieval (demo library) -> openadapt-ml (adapters) -> openadapt-evals (benchmark)
```

**Next Actions**:
- [ ] Integrate `openadapt-retrieval` with `openadapt-ml` adapters
- [ ] Add `--demo` flag to `openadapt eval run`
- [ ] Test with real demo library on WAA benchmark
- [ ] Document demo library format (JSON structure, screenshots)
- [ ] Add `--demo-library` option for multi-demo retrieval

---

### 7. WAA Benchmark Validation

| Field | Value |
|-------|-------|
| **Status** | Blocked on Azure VM Setup |
| **Effort** | Medium (4-8 hours) |
| **Owner** | TBD |
| **Package** | `openadapt-evals` |

**Description**: Need to validate demo-conditioning claims on full Windows Agent Arena benchmark. This provides credibility for landing page claims.

**Infrastructure Required**:
- Azure VM with nested virtualization (Windows 10/11)
- WAA server running
- API keys for Claude/GPT-4V

**Target Metrics**:

| Metric | Baseline (No Demo) | With Demo | Target |
|--------|-------------------|-----------|--------|
| First-action accuracy | ~33% | ~100% | Validate |
| Episode success rate | TBD | TBD | Measure |
| Average steps | TBD | TBD | Measure |

**Next Actions**:
- [ ] Start Azure VM with WAA server (nested virtualization)
- [ ] Run `openadapt eval run --agent api-claude --server <waa-url>`
- [ ] Record metrics: episode success rate, avg steps, failure modes
- [ ] Generate HTML report with `openadapt-viewer`
- [ ] Document results for landing page claims

---

## P2 - Medium: Enhancements

### 8. Safety Gate Implementation

| Field | Value |
|-------|-------|
| **Status** | Design Phase |
| **Effort** | Medium (4-8 hours) |
| **Owner** | TBD |
| **Package** | `openadapt-ml` |

**Description**: Implement safety gates to prevent harmful or unintended actions during agent execution.

**Safety Categories**:
1. **Pre-action validation**: Check action against allowed patterns
2. **Dangerous action detection**: Block destructive file ops, system commands
3. **Human-in-the-loop confirmation**: Require approval for certain actions
4. **Rollback capability**: Undo recent actions if needed

**Next Actions**:
- [ ] Design safety gate API interface
- [ ] Implement pre-action validation hooks
- [ ] Add dangerous action detection (rm, format, delete, etc.)
- [ ] Add optional human confirmation prompts
- [ ] Document safety configuration options

---

### 9. Grounding Provider Improvements

| Field | Value |
|-------|-------|
| **Status** | Package Published (53 tests passing) |
| **Effort** | Medium (4-6 hours) |
| **Owner** | TBD |
| **Package** | `openadapt-grounding` |

**Description**: `openadapt-grounding` provides UI element localization for improved click accuracy. Needs integration with ML package.

**Available Providers**:

| Provider | Backend | Status | GPU Required |
|----------|---------|--------|--------------|
| OmniGrounder | OmniParser | Working | Yes (CUDA) |
| GeminiGrounder | Gemini API | Working | No |
| SoMGrounder | Set-of-Marks | Working | Yes |

**Next Actions**:
- [ ] Integrate with `openadapt-ml` action replay
- [ ] Test OmniGrounder with recorded captures
- [ ] Test GeminiGrounder with API key
- [ ] Add grounding visualization to `openadapt-viewer`
- [ ] Document grounding provider selection
- [ ] Fix Docker build for OmniParser server

---

### 10. Viewer Dashboard Features

| Field | Value |
|-------|-------|
| **Status** | Basic HTML Generation Works |
| **Effort** | Medium (4-8 hours) |
| **Owner** | TBD |
| **Package** | `openadapt-viewer` |

**Description**: `openadapt-viewer` generates HTML but could be enhanced for better debugging and analysis.

**Requested Features**:

| Feature | Priority | Complexity |
|---------|----------|------------|
| Video playback from screenshots | High | Medium |
| Action timeline with seek | High | Medium |
| Side-by-side comparison view | Medium | Low |
| Filtering by action type | Medium | Low |
| Benchmark result integration | Medium | Medium |
| Failure analysis tools | Medium | High |

**Next Actions**:
- [ ] Add video playback (from captured screenshots)
- [ ] Add action timeline with seek
- [ ] Add side-by-side comparison view
- [ ] Add filtering by action type
- [ ] Integrate with benchmark results for failure analysis

---

## P3 - Lower: Nice to Have

### 11. Telemetry (GlitchTip)

| Field | Value |
|-------|-------|
| **Status** | Design Doc Complete |
| **Effort** | Large (1-2 weeks) |
| **Owner** | TBD |
| **Design Doc** | `docs/design/telemetry-design.md` |

**Description**: Create `openadapt-telemetry` package for unified error tracking and usage analytics across all packages.

**Key Features**:
- GlitchTip/Sentry SDK integration
- Privacy filtering (path sanitization, PII scrubbing)
- Internal user tagging (CI detection, dev mode)
- Opt-out mechanisms (DO_NOT_TRACK env var)

**Next Actions**:
- [ ] Create `openadapt-telemetry` package scaffold
- [ ] Implement Sentry/GlitchTip integration
- [ ] Add privacy filtering (path sanitization, PII scrubbing)
- [ ] Add internal user tagging (CI detection, dev mode)
- [ ] Create opt-out mechanisms (DO_NOT_TRACK env var)
- [ ] Integrate with openadapt-evals as pilot

---

### 12. Additional Benchmarks (WebArena, OSWorld)

| Field | Value |
|-------|-------|
| **Status** | Future Consideration |
| **Effort** | Large (2-4 weeks) |
| **Owner** | TBD |
| **Package** | `openadapt-evals` |

**Description**: Expand evaluation infrastructure beyond WAA.

**Target Benchmarks**:

| Benchmark | Type | Status | Priority |
|-----------|------|--------|----------|
| Windows Agent Arena (WAA) | Desktop | In Progress | High |
| WebArena | Web Browser | Not Started | Medium |
| OSWorld | Cross-Platform | Not Started | Medium |
| MiniWoB++ | Synthetic | Not Started | Low |

**Next Actions**:
- [ ] Implement WebArena adapter for browser automation
- [ ] Implement OSWorld adapter for cross-platform desktop
- [ ] Create unified metrics across benchmarks
- [ ] Add benchmark comparison view

---

### 13. Documentation Site (docs.openadapt.ai)

| Field | Value |
|-------|-------|
| **Status** | MkDocs Configured, Needs Deployment |
| **Effort** | Medium (4-6 hours) |
| **Owner** | TBD |
| **Config** | `mkdocs.yml` |

**Description**: Documentation site using MkDocs with existing markdown files.

**Existing Documentation**:
- `docs/index.md` - Home page
- `docs/architecture.md` - System architecture
- `docs/cli.md` - CLI reference
- `docs/packages/*.md` - Package documentation
- `docs/getting-started/*.md` - Installation, quickstart, permissions

**Next Actions**:
- [ ] Verify `mkdocs.yml` configuration
- [ ] Run `mkdocs build` and test locally
- [ ] Set up GitHub Actions for auto-deploy to GitHub Pages
- [ ] Configure CNAME for docs.openadapt.ai
- [ ] Add API reference (auto-generated from docstrings)
- [ ] Write getting-started tutorial (5-minute quickstart)

---

## Dependency Graph

```
P0: Fix CI (PR #969) ─────────────────────────────────────────────────┐
P0: Docker Build ─────────────────────────────────────────────────────┤
P0: Verify Meta-Package ──────────────────────────────────────────────┤
P0: Basic Workflow ───────────────────────────────────────────────────┤
                                                                      │
                                                                      v
P1: Baseline Adapters ────────────────────────────────────────────────┤
P1: Demo Conditioning ────────────────────────────────────────────────┤
P1: WAA Benchmark ────────────────────────────────────────────────────┘
        │
        v
P2: Safety Gates ─────────────────────────────────────────────────────┐
P2: Grounding Improvements ───────────────────────────────────────────┤
P2: Viewer Dashboard ─────────────────────────────────────────────────┘
        │
        v
P3: Telemetry (GlitchTip) ────────────────────────────────────────────┐
P3: Additional Benchmarks ────────────────────────────────────────────┤
P3: Documentation Site ───────────────────────────────────────────────┘
```

---

## Technical Debt

### Known Issues

| Issue | Severity | Package | Notes |
|-------|----------|---------|-------|
| Python version mismatch | Medium | `openadapt-ml` | Requires 3.12+, others 3.10+ |
| `capture stop` TODO | Low | `openadapt` CLI | Uses Ctrl+C instead of signal/file |
| `release-and-publish.yml` uses hatchling | Low | Main repo | Aligned with meta-package |
| Legacy code | Low | `/legacy/` | Many TODOs, not blocking v1.0 |

### Code Quality

| Package | TODOs | Notes |
|---------|-------|-------|
| `openadapt/cli.py` | 1 | Implement stop via signal/file |
| `legacy/` | 100+ | Historical, not blocking v1.0 |

---

## Success Criteria

### P0 Complete (This Week)

- [ ] CI passes on all matrix combinations (Python 3.10/3.11/3.12, macOS/Ubuntu)
- [ ] PR #969 merged
- [ ] Docker build succeeds for OmniParser
- [ ] `pip install openadapt[core]` works on Python 3.12
- [ ] Basic capture/eval workflow demonstrated

### P1 Complete (1-2 Weeks)

- [ ] API agents (Claude, GPT-4V) working with demo conditioning
- [ ] WAA baseline established with metrics
- [ ] First-action accuracy validated (33% -> 100% with demo)

### P2 Complete (This Month)

- [ ] Safety gates implemented and documented
- [ ] Grounding improving action accuracy
- [ ] Viewer dashboard with video playback

### P3 Complete (Backlog)

- [ ] Telemetry package published
- [ ] docs.openadapt.ai live
- [ ] Additional benchmarks integrated

---

## Resources Required

| Resource | Purpose | Status |
|----------|---------|--------|
| Azure credits | WAA benchmark VM | Available |
| Anthropic API key | Claude testing | Available (in openadapt-ml/.env) |
| OpenAI API key | GPT-4V testing | Available (in openadapt-ml/.env) |
| Google API key | Gemini testing | Available (in openadapt-ml/.env) |
| Test machines | Windows 10/11, Ubuntu 22.04/24.04 | Provision using existing tooling |
| DNS access | docs.openadapt.ai CNAME | Done |

---

## Appendix: Quick Reference

### PyPI Package URLs

- https://pypi.org/project/openadapt/
- https://pypi.org/project/openadapt-capture/
- https://pypi.org/project/openadapt-ml/
- https://pypi.org/project/openadapt-evals/
- https://pypi.org/project/openadapt-viewer/
- https://pypi.org/project/openadapt-grounding/
- https://pypi.org/project/openadapt-retrieval/
- https://pypi.org/project/openadapt-privacy/

### GitHub Repositories

- Main: https://github.com/OpenAdaptAI/openadapt
- Sub-packages: https://github.com/OpenAdaptAI/openadapt-{capture,ml,evals,viewer,grounding,retrieval,privacy}

### Related Documents

- Architecture: `/docs/architecture.md`
- Telemetry Design: `/docs/design/telemetry-design.md`
- Landing Page Strategy: `/docs/design/landing-page-strategy.md`
- Legacy Freeze: `/docs/legacy/freeze.md`

---

*This roadmap is a living document. Update as priorities shift based on user feedback and technical discoveries.*
