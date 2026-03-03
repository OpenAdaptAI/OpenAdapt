# Claude Code Instructions for openadapt-desktop

## MANDATORY: Branches and Pull Requests

**NEVER push directly to main. ALWAYS use feature branches and pull requests.**

1. Create a feature branch: `git checkout -b feat/description` or `fix/description`
2. Make commits on the branch
3. Push the branch: `git push -u origin branch-name`
4. Create a PR: `gh pr create --title "..." --body "..."`
5. Only merge via PR (never `git push origin main`)

### PR Titles MUST Use Conventional Commit Format

```
fix: short description          -> patch bump (0.0.x)
feat: short description         -> minor bump (0.x.0)
fix(scope): short description   -> patch bump with scope
feat!: breaking change          -> major bump (x.0.0)
```

**Types**: feat, fix, docs, style, refactor, perf, test, chore, ci

---

## Overview

Cross-platform desktop app for continuous screen recording and AI training data collection. Built with Tauri 2.x (Rust shell) + Python sidecar (recording engine).

## Quick Start

```bash
uv sync --extra dev
uv run pytest tests/ -v
uv run ruff check engine/ tests/
```

## Architecture

Two-process model:
- **Tauri shell** (Rust + WebView): system tray, native window, IPC dispatch
- **Python engine** (sidecar): recording, scrubbing, storage, upload

Communication via JSON-over-stdin/stdout IPC protocol (see DESIGN.md Appendix B).

## Key Design Decisions

1. **Raw-then-review scrubbing**: Recordings saved raw to disk. Scrubbing is a separate user-reviewed step. `check_egress_allowed()` gates ALL outbound paths.
2. **Build-time trust**: Tauri Cargo.toml feature flags physically exclude upload code. Enterprise binary verifiable with `strings`.
3. **Multiple storage backends**: StorageBackend protocol in `engine/backends/protocol.py`. All backends conform to the same interface.
4. **Network audit logging**: Every outbound request logged to `audit.jsonl` (JSONL format).

## File Map

| File | Purpose |
|------|---------|
| `engine/controller.py` | Recording lifecycle (start/stop/pause) |
| `engine/review.py` | Upload review state machine (the egress gate) |
| `engine/scrubber.py` | PII scrubbing orchestration |
| `engine/config.py` | Settings (pydantic-settings, OPENADAPT_ prefix) |
| `engine/audit.py` | Network audit logger |
| `engine/backends/protocol.py` | StorageBackend protocol definition |
| `engine/backends/s3.py` | S3/R2/MinIO backend |
| `src-tauri/src/commands.rs` | IPC commands (13 endpoints) |
| `src-tauri/src/main.rs` | Tauri entry point |
| `DESIGN.md` | Comprehensive design document (v2.0, 1800 lines) |

## Running Tests

```bash
uv run pytest tests/ -v              # all tests
uv run ruff check engine/ tests/     # lint
```

## Dependencies

- Python: `openadapt-capture` (recording), `openadapt-privacy` (scrubbing), `pydantic-settings`
- Rust: `tauri`, `tauri-plugin-shell`, `tauri-plugin-notification`, `tauri-plugin-updater`
- Optional: `boto3` (S3), `huggingface_hub`, `flwr` + `torch` (federated)
