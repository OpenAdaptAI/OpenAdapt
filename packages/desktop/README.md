# OpenAdapt Desktop

[![Tests](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml/badge.svg)](https://github.com/OpenAdaptAI/openadapt-desktop/actions/workflows/test.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)

Cross-platform desktop app for continuous screen recording and AI training data collection, built for [OpenAdapt](https://github.com/OpenAdaptAI/OpenAdapt).

## What is OpenAdapt Desktop?

OpenAdapt Desktop captures desktop activity -- screen recordings, mouse events, keyboard events, window metadata, and optionally audio -- for training AI agents via demonstration.

The Python engine works as a **standalone CLI** today. A Tauri-based system tray app (macOS, Windows, Linux) is planned for a future release.

**Key principles:**

- **Raw recordings stay local** -- nothing leaves your machine without explicit review and approval
- **Human-in-the-loop scrubbing** -- PII detection and redaction with before/after comparison
- **Build-time trust guarantees** -- enterprise builds physically exclude upload code paths
- **Multiple upload backends** -- S3, HuggingFace Hub, Cloudflare R2, MinIO, Magic Wormhole, or federated learning

## Quick Start

```bash
# Install
git clone https://github.com/OpenAdaptAI/openadapt-desktop.git
cd openadapt-desktop
uv sync

# Record a session
uv run openadapt record --task "Demo task"    # Ctrl+C to stop

# Scrub PII, review, and upload
uv run openadapt scrub <CAPTURE_ID> --level basic
uv run openadapt approve <CAPTURE_ID>
uv run openadapt upload <CAPTURE_ID> --backend s3

# Other commands
uv run openadapt list                         # List captures
uv run openadapt review                       # Show pending reviews
uv run openadapt storage                      # Show disk usage
uv run openadapt health                       # Show memory/disk health
uv run openadapt cleanup                      # Enforce storage limits
uv run openadapt config                       # Show current configuration
uv run openadapt backends                     # Show available backends
```

## Architecture

```
Tauri Shell (Rust + WebView)        Python Engine (sidecar / CLI)
+----------------------------+      +---------------------------+
|  System tray icon          |      |  cli.py (CLI entry point) |
|  Start/stop recording      | IPC  |  controller.py            |
|  Settings panel            |<---->|    -> openadapt-capture   |
|  Upload review UI          | JSON |  scrubber.py              |
|  Consent dialogs           |      |    -> openadapt-privacy   |
+----------------------------+      |  db.py (SQLite index)     |
                                    |  storage_manager.py       |
                                    |  upload_manager.py        |
                                    |  review.py (egress gate)  |
                                    |  monitor.py (health)      |
                                    |  audit.py (network log)   |
                                    |  backends/                |
                                    |    s3, hf, wormhole, fl   |
                                    +---------------------------+
```

The Python engine works standalone via the CLI. When the Tauri shell is built, it communicates with the engine via JSON-over-stdin/stdout IPC.

## CLI Commands

| Command | Description |
|---------|-------------|
| `openadapt record [--quality standard] [--task "..."]` | Start recording (Ctrl+C to stop) |
| `openadapt list [--limit 10] [--status captured]` | List captures |
| `openadapt info <ID>` | Show capture details |
| `openadapt scrub <ID> [--level basic\|standard\|enhanced]` | Scrub PII from capture |
| `openadapt review` | List captures pending review |
| `openadapt approve <ID>` | Approve scrubbed capture for upload |
| `openadapt dismiss <ID>` | Skip scrubbing, accept PII risks |
| `openadapt upload <ID> --backend s3\|huggingface\|wormhole` | Upload capture |
| `openadapt backends` | List available backends |
| `openadapt storage` | Show storage usage |
| `openadapt health` | Show memory/disk health |
| `openadapt cleanup` | Run storage cleanup |
| `openadapt config` | Show current configuration |

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

**All outbound paths are gated** -- not just storage uploads, but also VLM API calls, annotation pipelines, federated learning gradient uploads, and Magic Wormhole sharing. The single enforcement point is `review.py:check_egress_allowed()`.

## Storage Backends

| Backend | Use Case | Cost | Delete? |
|---------|----------|------|---------|
| Local only | Air-gapped / offline | Free | Yes |
| AWS S3 | Enterprise | ~$0.023/GB/mo | Yes |
| Cloudflare R2 | S3-compatible, free egress | ~$0.015/GB/mo | Yes |
| HuggingFace Hub | Community dataset sharing | Free (public) | Yes |
| MinIO | Self-hosted S3-compatible | Free (self-hosted) | Yes |
| Magic Wormhole | Peer-to-peer ad-hoc transfer | Free | N/A |
| Federated Learning | Model improvement without data sharing | Free | N/A |

Enterprise users can verify that unwanted backends are excluded at the binary level (`strings openadapt-engine | grep huggingface` returns nothing in enterprise builds).

## Project Status

**v0.1.0** -- The Python engine is **fully functional end-to-end** as a standalone CLI. 106 tests pass, 0 skipped. See [DESIGN.md](DESIGN.md) for the full design document.

### What's working

- **Full recording pipeline**: record -> scrub -> review -> upload via CLI
- **Recording controller**: wraps `openadapt-capture`, crash recovery, state tracking
- **PII scrubbing**: regex (basic), Presidio NER (standard/enhanced), image scrubbing
- **Review state machine**: DB-persisted egress gating with audit logging
- **Storage management**: SQLite index DB, hot/warm/cold tiers, tar.gz archival, cleanup
- **Upload manager**: persistent queue, egress checks, multi-backend dispatch
- **Storage backends**: S3 (boto3), HuggingFace Hub, Magic Wormhole -- all implemented
- **Health monitoring**: memory (psutil) and disk monitoring with daemon threads
- **CLI**: 13 commands via argparse (`openadapt record/list/scrub/approve/upload/...`)
- **Audit logging**: append-only JSONL log of all network activity
- **CI**: 106 tests passing on all platforms (macOS, Windows, Linux) x (Python 3.11, 3.12)

### What's next

- [ ] Pause/resume recording (requires openadapt-capture support)
- [ ] Build the upload review UI (Tauri WebView)
- [ ] Tauri IPC wiring (Rust <-> Python sidecar)
- [ ] PyInstaller sidecar bundling
- [ ] Native installers (DMG, MSI, AppImage)
- [ ] Auto-update via Tauri updater plugin
- [ ] Upload scheduling (cron/idle)
- [ ] Bandwidth limiting (token bucket)
- [ ] zstd compression for warm-tier archives

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

# CLI smoke test
uv run python -m engine list
uv run python -m engine storage
uv run python -m engine health
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
+-- engine/                  Python engine (standalone CLI + sidecar)
|   +-- cli.py               CLI entry point (13 commands)
|   +-- db.py                SQLite index database (WAL mode)
|   +-- controller.py        Recording start/stop lifecycle
|   +-- scrubber.py          PII scrubbing (regex + Presidio)
|   +-- review.py            Egress gate state machine
|   +-- storage_manager.py   Hot/warm/cold tiers + cleanup
|   +-- upload_manager.py    Persistent upload queue
|   +-- monitor.py           Memory + disk health monitoring
|   +-- config.py            Settings (pydantic-settings)
|   +-- audit.py             Network audit logging (JSONL)
|   +-- ipc.py               JSON-over-stdin/stdout protocol
|   +-- main.py              Entry point (CLI or IPC mode)
|   +-- backends/
|       +-- protocol.py      StorageBackend protocol
|       +-- s3.py            S3/R2/MinIO backend (boto3)
|       +-- huggingface.py   HuggingFace Hub backend
|       +-- wormhole.py      Magic Wormhole P2P backend
|       +-- federated.py     Flower federated learning (v2.0)
+-- src-tauri/               Tauri shell (Rust, future)
|   +-- src/main.rs          Entry point, tray, plugin init
|   +-- src/commands.rs      IPC commands (13 endpoints)
|   +-- src/sidecar.rs       Python process management
|   +-- src/tray.rs          System tray setup
|   +-- Cargo.toml           Rust dependencies + feature flags
+-- src/                     WebView frontend (HTML/CSS/JS, future)
+-- tests/
|   +-- test_engine/         Unit tests (db, controller, scrubber, ...)
|   +-- test_e2e/            E2E pipeline + IPC tests
+-- DESIGN.md                Comprehensive design document (v2.0)
+-- pyproject.toml           Python package config (hatchling)
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
