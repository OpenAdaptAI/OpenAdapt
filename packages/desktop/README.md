# OpenAdapt Desktop

[![Tests](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Cross-platform desktop app for continuous screen recording and AI training data collection, built for [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt).

## Screenshots

**Dashboard** -- start/stop recording, monitor storage, view recent captures:

![Dashboard](https://raw.githubusercontent.com/OpenAdaptAI/openadapt-desktop/main/screenshots/01_dashboard_idle.png)

**Recording** -- active recording with duration and status indicator:

![Recording](https://raw.githubusercontent.com/OpenAdaptAI/openadapt-desktop/main/screenshots/02_dashboard_recording.png)

**Upload Review** -- review pending recordings before any data leaves the machine:

![Review](https://raw.githubusercontent.com/OpenAdaptAI/openadapt-desktop/main/screenshots/03_review_panel.png)

<details>
<summary>Generate screenshots locally</summary>

```bash
uv sync --extra dev
uv run playwright install chromium
uv run python scripts/generate_screenshots.py --output screenshots/
```

</details>

## What is OpenAdapt Desktop?

OpenAdapt Desktop is a system tray application (macOS, Windows, Linux) that continuously captures desktop activity -- screen recordings, mouse events, keyboard events, window metadata, and optionally audio -- for training AI agents via demonstration.

**Key principles:**

- **Raw recordings stay local** -- nothing leaves your machine without explicit review and approval
- **Human-in-the-loop scrubbing** -- PII detection and redaction with before/after comparison
- **Build-time trust guarantees** -- enterprise builds physically exclude upload code paths
- **Multiple upload backends** -- S3, HuggingFace Hub, Cloudflare R2, MinIO, Magic Wormhole, or federated learning

## Architecture

```
Tauri Shell (Rust + WebView)        Python Engine (sidecar)
+----------------------------+      +---------------------------+
|  System tray icon          |      |  controller.py            |
|  Start/stop recording      | IPC  |    -> openadapt-capture   |
|  Settings panel            |<---->|  scrubber.py              |
|  Upload review UI          | JSON |    -> openadapt-privacy   |
|  Consent dialogs           |      |  storage_manager.py       |
+----------------------------+      |  upload_manager.py        |
                                    |  review.py (state machine)|
                                    |  audit.py (network log)   |
                                    |  backends/                |
                                    |    s3, hf, wormhole, fl   |
                                    +---------------------------+
```

The Tauri shell provides a lightweight native window (~2-10 MB) while the Python engine handles all recording, scrubbing, and upload logic. They communicate via JSON-over-stdin/stdout IPC.

## Recording Review State Machine

Every recording must pass through a review gate before any data can leave the machine:

```
  CAPTURED (raw on disk, blocked from ALL egress)
     |
     +-- scrub --> SCRUBBED (pending user review)
     |                |
     |                +-- approve --> REVIEWED (scrubbed copy cleared)
     |
     +-- dismiss --> DISMISSED (user accepted PII risks)
     |
     +-- delete --> DELETED
```

**All outbound paths are gated** -- not just storage uploads, but also VLM API calls (OpenAI Vision, Anthropic Claude, Google Gemini), annotation pipelines, federated learning gradient uploads, and Magic Wormhole sharing.

## Storage Backends

| Backend | Use Case | Cost | Delete? |
|---------|----------|------|---------|
| Local only | Air-gapped / offline | Free | Yes |
| AWS S3 | Enterprise (Enterprise, etc.) | ~$0.023/GB/mo | Yes |
| Cloudflare R2 | S3-compatible, free egress | ~$0.015/GB/mo | Yes |
| HuggingFace Hub | Community dataset sharing | Free (public) | Yes |
| MinIO | Self-hosted S3-compatible | Free (self-hosted) | Yes |
| Magic Wormhole | Peer-to-peer ad-hoc transfer | Free | N/A |
| Federated Learning | Model improvement without data sharing | Free | N/A |

Enterprise users can verify that unwanted backends are excluded at the binary level (`strings openadapt-engine | grep huggingface` returns nothing in enterprise builds).

## Project Status

This project is in **early development** (v0.1.0). The Python engine scaffold is complete with passing tests. The Tauri shell has IPC command stubs. See [DESIGN.md](DESIGN.md) for the full design document.

### What's working

- Python engine: config, review state machine, audit logging, storage backend protocol
- Storage backends: S3 cost estimation, Wormhole credential check, protocol conformance
- Controller: recording state enum, idle detection
- E2E tests: Playwright-based UI tests for all HTML pages, IPC protocol tests
- Automated screenshot generation: `scripts/generate_screenshots.py`
- CI: 52 tests passing on all platforms (macOS, Windows, Linux) x (Python 3.11, 3.12)

### What's next

- [ ] Wire up `openadapt-capture` recording engine to the controller
- [ ] Implement PII scrubbing with `openadapt-privacy`
- [ ] Build the upload review UI (extending `openadapt-viewer` components)
- [ ] Implement S3 multipart upload
- [ ] PyInstaller sidecar bundling
- [ ] Tauri IPC wiring (Rust <-> Python sidecar)
- [ ] Native installers (DMG, MSI, AppImage)
- [ ] Auto-update via Tauri updater plugin

## Development

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) (recommended) or pip
- Rust + Cargo (for Tauri shell, optional for engine-only development)
- Node.js 18+ (for Tauri CLI)

### Setup

```bash
git clone https://github.com/OpenAdaptAI/openadapt-desktop.git
cd openadapt-desktop

# Install Python dependencies
uv sync --extra dev

# Run tests
uv run pytest tests/ -v

# Lint
uv run ruff check engine/ tests/
```

### Optional extras

```bash
uv sync --extra enterprise    # S3/R2/MinIO (boto3)
uv sync --extra community     # HuggingFace Hub
uv sync --extra federated     # Flower + PyTorch
uv sync --extra full          # Everything
```

### Project Structure

```
openadapt-desktop/
+-- engine/                  Python sidecar (the recording engine)
|   +-- controller.py        Recording start/stop/pause lifecycle
|   +-- ipc.py               JSON-over-stdin/stdout protocol
|   +-- storage_manager.py   Local storage tiers + cleanup
|   +-- upload_manager.py    Multi-backend upload queue
|   +-- scrubber.py          PII scrubbing orchestration
|   +-- review.py            Upload review state machine
|   +-- config.py            Settings (pydantic-settings)
|   +-- monitor.py           Health monitoring (memory, disk)
|   +-- audit.py             Network audit logging (JSONL)
|   +-- backends/
|       +-- protocol.py      StorageBackend protocol
|       +-- s3.py            S3/R2/MinIO backend
|       +-- huggingface.py   HuggingFace Hub backend
|       +-- wormhole.py      Magic Wormhole P2P backend
|       +-- federated.py     Flower federated learning
+-- src-tauri/               Tauri shell (Rust)
|   +-- src/main.rs          Entry point, tray, plugin init
|   +-- src/commands.rs      IPC commands (13 endpoints)
|   +-- src/sidecar.rs       Python process management
|   +-- src/tray.rs          System tray setup
|   +-- Cargo.toml           Rust dependencies + feature flags
+-- src/                     WebView frontend (HTML/CSS/JS)
|   +-- index.html           Main dashboard
|   +-- review.html          Upload review panel
|   +-- settings.html        Configuration panel
+-- tests/                   Python engine tests
+-- .github/workflows/       CI (test + build)
+-- DESIGN.md                Comprehensive design document (v2.0)
+-- pyproject.toml            Python package config (hatchling)
+-- package.json             Node/Tauri config
```

## Configuration

Settings are loaded from environment variables (prefixed with `OPENADAPT_`) via pydantic-settings:

```bash
# .env
OPENADAPT_STORAGE_MODE=enterprise    # air-gapped | enterprise | community | full
OPENADAPT_MAX_STORAGE_GB=50
OPENADAPT_RECORDING_QUALITY=standard # low | standard | high | lossless

# S3 (enterprise mode)
OPENADAPT_S3_BUCKET=my-recordings
OPENADAPT_S3_REGION=us-east-1
OPENADAPT_S3_ACCESS_KEY_ID=...
OPENADAPT_S3_SECRET_ACCESS_KEY=...

# HuggingFace Hub (community mode)
OPENADAPT_HF_REPO=OpenAdaptAI/desktop-recordings
OPENADAPT_HF_TOKEN=hf_...

# Federated learning
OPENADAPT_FL_ENABLED=true
OPENADAPT_FL_SERVER=https://fl.openadapt.ai
```

## Related Projects

| Project | Description |
|---------|-------------|
| [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt) | Desktop automation with demo-conditioned AI agents |
| [openadapt-capture](https://github.com/OpenAdaptAI/openadapt-capture) | Multi-process screen recording engine |
| [openadapt-privacy](https://github.com/OpenAdaptAI/openadapt-privacy) | PII detection and redaction via Presidio |
| [openadapt-viewer](https://github.com/OpenAdaptAI/openadapt-viewer) | Reusable visualization components |
| [openadapt-evals](https://github.com/OpenAdaptAI/openadapt-evals) | GUI agent evaluation infrastructure |
| [openadapt-ml](https://github.com/OpenAdaptAI/openadapt-ml) | Training and policy runtime |

## License

[MIT](https://opensource.org/licenses/MIT)
