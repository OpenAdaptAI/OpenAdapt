# OpenAdapt Roadmap

**Updated**: January 17, 2026 | **Version**: 1.1.0

Goal: **Record -> Train -> Evaluate** GUI automation workflows.

---

## Packages (All Published)

| Package | Python | Notes |
|---------|--------|-------|
| `openadapt` (meta) | >=3.10 | |
| `openadapt-capture` | >=3.10 | |
| `openadapt-ml` | >=3.10 | PR #5 fixes Python version |
| `openadapt-evals` | >=3.10 | |
| `openadapt-viewer` | >=3.10 | |
| `openadapt-grounding` | >=3.10 | 53 tests passing |
| `openadapt-retrieval` | >=3.10 | 28 tests passing |
| `openadapt-privacy` | >=3.10 | |

---

## P0 - Critical (This Week)

### 1. Fix CI - Ruff Format
- **PR**: #969, branch `fix/ruff-format-config`
- **Action**: Merge PR, verify CI passes (Python 3.10-3.12, macOS/Ubuntu)

### 2. Fix Docker Build
- **Location**: `legacy/deploy/deploy/models/omniparser/Dockerfile`
- **Actions**:
  - Test `docker build`
  - Verify CUDA/GPU support
  - Test model download (huggingface-cli)

### 3. Verify Meta-Package Installation
- Test `pip install openadapt[core]` and `openadapt[all]` on Python 3.10+
- Verify: `python -c "from openadapt.cli import main"`

### 4. End-to-End Workflow
Test the core workflow:
```bash
openadapt capture start --name my-task
openadapt train start --capture my-task
openadapt eval run --checkpoint model.pt
```

**Blockers**: `capture stop` TODO (uses Ctrl+C), macOS needs Accessibility + Screen Recording permissions

---

## P1 - High (1-2 Weeks)

### 5. Baseline Adapters
- Package: `openadapt-ml`
- Test: Claude, GPT-4V, Gemini, Qwen3-VL adapters
- Add rate limit / API failure handling

### 6. Demo Conditioning in Evals
- **Key result**: 46.7% -> 100% first-action accuracy with demo conditioning (on shared-entry-point benchmark)
- Integrate `openadapt-retrieval` with `openadapt-ml` adapters
- Add `--demo` flag to `openadapt eval run`

### 7. WAA Benchmark Validation
- Blocked on: Azure VM with nested virtualization
- Goal: Validate demo-conditioning claims on Windows Agent Arena

---

## P2 - Medium (This Month)

### 8. Safety Gates
- Pre-action validation, dangerous action detection
- Optional human-in-the-loop confirmation

### 9. Grounding Improvements
- Integrate with `openadapt-ml` action replay
- Providers: OmniGrounder (CUDA), GeminiGrounder (API), SoMGrounder (CUDA)

### 10. Viewer Dashboard
- Add video playback, action timeline, side-by-side comparison
- Filter by action type

---

## P3 - Backlog

### 11. Telemetry (GlitchTip)
- Create `openadapt-telemetry` package
- GlitchTip/Sentry integration with privacy filtering
- Design: `docs/design/telemetry-design.md`

### 12. Additional Benchmarks
- WebArena, OSWorld, MiniWoB++

### 13. Documentation Site
- Deploy MkDocs to docs.openadapt.ai

---

## Success Criteria

| Phase | Criteria |
|-------|----------|
| P0 | CI green, Docker builds, `openadapt[core]` installs, basic workflow works |
| P1 | API agents with demo conditioning, WAA baseline established |
| P2 | Safety gates, grounding integration, viewer with video |
| P3 | Telemetry published, docs live, additional benchmarks |

---

## Technical Debt

- `capture stop` uses Ctrl+C instead of signal/file
- Legacy code in `/legacy/` (not blocking v1.0)

---

## Resources

| Resource | Status |
|----------|--------|
| Azure credits (WAA) | Available |
| API keys (Claude, GPT-4V, Gemini) | In `openadapt-ml/.env` |
| DNS (docs.openadapt.ai) | Configured |
