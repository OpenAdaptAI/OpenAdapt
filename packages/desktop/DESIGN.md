# OpenAdapt Desktop: Comprehensive Design Document

**Version**: 2.0 (Draft)
**Date**: 2026-03-03
**Status**: Design Phase
**Authors**: OpenAdapt Engineering

---

## Table of Contents

1. [Executive Summary](#1-executive-summary)
2. [Repository & Packaging Strategy](#2-repository--packaging-strategy)
3. [Cross-Platform Desktop App Framework](#3-cross-platform-desktop-app-framework)
4. [Recording Engine](#4-recording-engine)
5. [PII Scrubbing Pipeline](#5-pii-scrubbing-pipeline)
6. [Storage & Data Management](#6-storage--data-management)
7. [Storage Backends & Trust Architecture](#7-storage-backends--trust-architecture)
8. [Upload Review & Consent UX](#8-upload-review--consent-ux)
9. [Federated Learning](#9-federated-learning)
10. [Auto-Update Mechanism](#10-auto-update-mechanism)
11. [CI/CD & Distribution](#11-cicd--distribution)
12. [System Tray & UX](#12-system-tray--ux)
13. [Security & Privacy](#13-security--privacy)
14. [Architecture Diagram](#14-architecture-diagram)
15. [Dependency Management](#15-dependency-management)
16. [Testing Strategy](#16-testing-strategy)
17. [Licensing & Legal](#17-licensing--legal)
18. [MVP vs Full Vision](#18-mvp-vs-full-vision)

---

## 1. Executive Summary

OpenAdapt Desktop is a cross-platform desktop application (macOS, Windows, Linux) that continuously captures user desktop activity -- screen recordings, mouse events, keyboard events, window metadata, and optionally audio -- for the purpose of training AI agents via demonstration. The application runs as a system tray app, stores raw recordings locally, and provides a human-in-the-loop scrub-then-review pipeline before any data leaves the machine. It supports multiple upload backends (S3, HuggingFace Hub, Cloudflare R2, MinIO, IPFS, Magic Wormhole) with build-time feature flags for verifiable enterprise trust, and a federated learning mode that improves models without sharing raw data. It auto-updates itself and is distributed as native installers (DMG, MSI/EXE, AppImage/deb).

### Existing Components Being Integrated

| Repo | Role | Current State |
|------|------|---------------|
| `openadapt-capture` (v0.3.0) | Multi-process recorder: pynput for mouse/keyboard, mss for screenshots, PyAV for H.264 video, SQLAlchemy + SQLite per-capture databases | Functional on macOS; Windows/Linux platform handlers exist. Requires local display (pynput). |
| `openadapt-privacy` (v0.1.0) | PII/PHI detection and redaction via Microsoft Presidio + spaCy transformer models. Text scrubbing, image scrubbing, nested dict scrubbing. | Functional. Heavyweight dependency (spaCy `en_core_web_trf` model ~500MB). |
| `openadapt-tray` (v0.1.0) | System tray via pystray, desktop-notifier for notifications, global hotkeys via pynput, IPC client, state management, platform handlers (macOS/Windows/Linux) | Implementation complete but uncommitted. Relies on subprocess calls to `openadapt` CLI. |
| `openadapt-ml` (v0.11.2) | VLM adapters, training, inference, annotation | Not directly needed in the desktop app. Consumer of recordings. |
| `openadapt-evals` (v0.24.0) | Evaluation infrastructure, VM management | Not directly needed in the desktop app. Consumer of recordings. |
| `openadapt-grounding` (v0.1.0) | UI element localization | Not directly needed in the desktop app. Consumer of recordings. |

---

## 2. Repository & Packaging Strategy

### Options Considered

| Option | Description | Pros | Cons |
|--------|-------------|------|------|
| **A. New repo (`openadapt-desktop`)** | Standalone repo that depends on capture, privacy, and tray as pip packages | Clean separation of concerns; independent release cycle; clear ownership boundary | Cross-repo coordination for breaking changes; need to publish all deps to PyPI |
| **B. Extend `openadapt-tray`** | Evolve the existing tray repo into the full desktop app | Already has tray infrastructure; less new repo overhead | Tray is a thin UI layer; scope creep; conflates UI with recording engine |
| **C. Monorepo with workspaces** | Single repo with uv workspaces for capture, privacy, tray, desktop | Atomic cross-package changes; single CI; shared tooling | Complex build; harder to release individual packages; large repo |
| **D. Extend `openadapt-capture`** | Add desktop app shell to the capture repo | Tight integration with recording engine | Capture is a library; conflates library with application |

### Recommendation: Option A -- New repo `openadapt-desktop`

**Rationale:**
- The desktop app is a *product* that *consumes* libraries. Libraries (capture, privacy) should remain independent and pip-installable.
- The existing repos are published on PyPI and used by other consumers (openadapt-ml, openadapt-evals). Coupling them to a desktop app shell would break that.
- A dedicated repo provides a clean home for Tauri/Electron config, native installers, CI build matrices, and update infrastructure -- none of which belong in a Python library.
- `openadapt-tray` code can be directly absorbed into the new repo (it is v0.1.0 with uncommitted changes and has no external consumers).

### Dependency Graph

```
openadapt-desktop (new repo, the product)
  |
  +-- openadapt-capture (pip dependency, recording engine)
  |     +-- pynput, mss, av (PyAV), Pillow, sounddevice/soundfile
  |     +-- SQLAlchemy, loguru, psutil
  |
  +-- openadapt-privacy[presidio] (pip dependency, PII scrubbing)
  |     +-- presidio-analyzer, presidio-anonymizer, presidio-image-redactor
  |     +-- spacy, spacy-transformers, en_core_web_trf model
  |
  +-- pystray, desktop-notifier, pynput (tray; absorbed from openadapt-tray)
  |
  +-- (Tauri shell: Rust + HTML/CSS/JS for settings UI)
```

### Package Format

| Distribution Method | Purpose |
|---------------------|---------|
| Native installer (DMG/MSI/AppImage) | End-user distribution. Self-contained binary with bundled Python runtime. |
| pip-installable package | Developer/power-user installation via `pip install openadapt-desktop` or `uv add openadapt-desktop`. |
| Both | Ship installers for end users; keep pip-installable for developers and CI. |

**Recommendation**: Ship native installers as the primary distribution channel. Maintain pip-installability for development and CI testing, but do not require it for end users.

---

## 3. Cross-Platform Desktop App Framework

### Options Compared

| Criterion | Tauri 2.x | Electron | PyInstaller + Native Tray | Flutter Desktop |
|-----------|-----------|----------|--------------------------|----------------|
| **Language** | Rust backend, HTML/CSS/JS frontend | Node.js backend, HTML/CSS/JS frontend | Pure Python (pystray + pynput) | Dart |
| **Bundle size** | 2-10 MB (uses OS WebView) | 80-120 MB (bundles Chromium) | 30-80 MB (bundles Python) | 15-30 MB |
| **RAM usage (idle)** | 30-40 MB | 100+ MB | 20-40 MB | 40-60 MB |
| **Startup time** | <0.5s | 1-2s | 1-3s (Python startup) | <1s |
| **System tray** | Native (plugin) | electron-tray | pystray (already implemented) | No native support |
| **Auto-update** | Built-in updater plugin with signature verification | electron-updater (mature) | Manual (no built-in) | Manual |
| **Code signing** | Built-in (macOS notarization, Windows Authenticode) | electron-builder (mature) | Manual (codesign, signtool) | Manual |
| **Installer generation** | Built-in (DMG, NSIS, AppImage, deb) | electron-builder (DMG, NSIS, AppImage, snap, deb) | PyInstaller + custom scripts | flutter-distributor |
| **Python integration** | Sidecar process (bundle Python as executable) or IPC | Child process or HTTP IPC | Native (is Python) | FFI or subprocess |
| **Permissions prompts** | Native macOS/Windows prompts | Requires manual handling | Requires manual handling | Requires manual handling |
| **Native look** | Uses OS WebView (Safari/WebView2/WebKitGTK) | Chromium-based (not native) | Truly native (no web UI) | Custom rendering |
| **Settings/preferences UI** | HTML/CSS/JS in WebView | HTML/CSS/JS in Chromium | None (would need tkinter or web) | Dart UI |
| **CI build matrix** | `tauri-action` (GitHub Actions) | `electron-builder` (GitHub Actions) | Custom scripts per platform | Custom scripts per platform |
| **Community/maturity** | Rapidly growing; 17.7K Discord; v2 stable since late 2024 | Very mature; huge ecosystem | Mature for CLI tools; not for apps | Desktop support still maturing |
| **Security model** | Opt-in permissions; Rust memory safety | Broad Node.js API access | Full Python access | Dart sandbox |

### Analysis

**Why not pure PyInstaller + native tray (Option C)?**
The existing `openadapt-tray` implementation uses pystray + pynput + desktop-notifier, which provides basic tray functionality. However, it lacks: (a) a settings/preferences UI (currently opens a browser to localhost), (b) an onboarding wizard, (c) auto-update capability, (d) native installer generation, (e) code signing, (f) storage usage visualization. Building all of this in pure Python would require either tkinter (poor UX) or embedding a web server (reinventing Electron poorly).

**Why not Electron?**
Electron bundles Chromium, adding 80-120 MB to the installer. For an app that runs 24/7 in the background, the 100+ MB RAM overhead of Chromium is unacceptable. The recording engine already consumes significant resources; the app shell should be as lightweight as possible.

**Why not Flutter?**
Flutter desktop lacks native system tray support and has an immature desktop ecosystem. No built-in auto-update or code signing story. Would require rewriting existing Python logic in Dart or maintaining a complex FFI bridge.

### Recommendation: Tauri 2.x with Python Sidecar

**Architecture:**

```
+------------------------------------------+
|  Tauri Shell (Rust + WebView)            |
|  - System tray (native)                  |
|  - Settings UI (HTML/CSS/JS)             |
|  - Auto-update (built-in plugin)         |
|  - Installer generation (built-in)       |
|  - Code signing (built-in)               |
+------------------------------------------+
           |  IPC (JSON over stdin/stdout or HTTP localhost)
           v
+------------------------------------------+
|  Python Sidecar (bundled via PyInstaller) |
|  - openadapt-capture (recording engine)  |
|  - openadapt-privacy (PII scrubbing)     |
|  - Storage management                    |
|  - Upload manager                        |
+------------------------------------------+
```

**Rationale:**
- Tauri is 2-10 MB vs Electron's 80-120 MB, and 30-40 MB RAM vs 100+ MB.
- Tauri's built-in auto-update with signature verification, code signing support, and installer generation eliminates the need for custom infrastructure.
- The Python sidecar pattern (Tauri spawns a bundled Python executable) preserves all existing Python code (openadapt-capture, openadapt-privacy) without rewriting.
- Tauri's native system tray plugin provides the tray icon, and the WebView provides a proper settings UI.
- The `tauri-action` GitHub Action handles cross-platform builds, signing, and release generation automatically.

**Tauri Sidecar Details:**
- The Python backend is packaged into a standalone executable using PyInstaller (or Nuitka for better performance).
- Tauri's sidecar feature bundles this executable alongside the app and spawns it on startup.
- Communication between Tauri (Rust) and Python uses JSON messages over stdin/stdout (Tauri's built-in sidecar IPC) or a localhost HTTP API.
- The Python process handles all recording, scrubbing, storage, and upload logic.
- Tauri handles all UI, tray, notifications, updates, and native OS integration.

---

## 4. Recording Engine

### Current State of `openadapt-capture`

The capture engine (`/Users/abrichr/oa/src/openadapt-capture`) provides:

| Capability | Implementation | Details |
|------------|---------------|---------|
| **Mouse events** | `pynput.mouse.Listener` | move, click (down/up), scroll. Coordinates in physical pixels. |
| **Keyboard events** | `pynput.keyboard.Listener` | press, release. Key names, chars, virtual keycodes, canonical forms. |
| **Screen capture** | `mss` (python-mss) | Full-screen screenshots. Monitor 0 = all monitors combined. Configurable FPS via `SCREEN_CAPTURE_FPS` (default 10). |
| **Video encoding** | `PyAV` (ffmpeg/libx264) | H.264 MP4, yuv444p pixel format, CRF 0 (lossless), veryslow preset. `VideoWriter` and `ChunkedVideoWriter` classes. |
| **Audio** | `sounddevice` + `soundfile` | FLAC audio recording. Optional Whisper transcription (local or API). |
| **Window metadata** | Platform-specific modules | Active window title, app name, bounds, PID. macOS (Quartz), Windows (Win32 API), Linux (xdotool). |
| **Event processing** | `processing.py` | Merges raw events into higher-level actions: single click, double click, drag, type, scroll. |
| **Storage** | SQLAlchemy (per-capture `recording.db`) + Pydantic SQLite (`capture.db`) | Two parallel storage systems exist. Legacy SQLAlchemy from OpenAdapt, newer Pydantic-based `CaptureStorage`. |
| **Sharing** | Magic Wormhole | Peer-to-peer transfer via `capture share send/receive`. |
| **Browser events** | WebSocket bridge | Chrome extension integration for DOM events. |

### 24/7 Continuous Recording Design

Running recordings continuously requires solving several problems that do not exist in short demo recordings:

#### 4.1 Crash Recovery

**Problem**: A crash during a multi-hour recording could lose all data.

**Solution**: Chunked recording with WAL-mode SQLite.

- **Video**: Use `ChunkedVideoWriter` (already exists) with 10-minute chunks. Each chunk is a complete, independently playable MP4. On crash, only the current chunk (up to 10 minutes) is lost.
- **Events**: SQLite in WAL (Write-Ahead Logging) mode commits events durably to disk. Events are written as they arrive, not buffered in memory.
- **Metadata**: Write session metadata (`started_at`, `platform`, `screen_size`) at start; update `ended_at` periodically (every 60 seconds) and on clean shutdown.
- **Watchdog**: A separate watchdog thread monitors the recording process. If the main recording process dies, the watchdog restarts it and begins a new session linked to the same capture.
- **State file**: Write a `state.json` file on each chunk boundary containing the current state (chunk index, event count, last timestamp). On restart, resume from the last known good state.

#### 4.2 Memory Leak Prevention

**Problem**: Python long-running processes are prone to memory leaks, especially with PIL Image objects and numpy arrays.

**Solution**: Multi-process architecture with periodic recycling.

- The recorder already uses `multiprocessing` (reader threads + writer processes, inherited from legacy OpenAdapt).
- Add a memory monitoring thread that tracks RSS every 30 seconds. If RSS exceeds a threshold (e.g., 500 MB), trigger a graceful restart of the recording process (finish current chunk, start new process).
- Use `psutil.Process().memory_info().rss` for monitoring (already a dependency).
- Explicitly `del` and `gc.collect()` PIL Image objects after encoding each video frame.
- Use `pympler.tracker.SummaryTracker` (already a dependency) in debug mode to detect leak sources.

#### 4.3 Adaptive Frame Rate

**Problem**: Capturing at 10 FPS 24/7 generates enormous amounts of data even when the screen is static.

**Solution**: Action-gated + change-detection hybrid.

| Mode | Trigger | FPS | Use Case |
|------|---------|-----|----------|
| **Idle** | No user input for >5 seconds | 0.1 (1 frame every 10 seconds) | User reading, idle, AFK |
| **Active** | User input detected (mouse move, key press) | Up to 10 FPS (configurable) | Active use |
| **Burst** | Click, type, or drag events | Up to 30 FPS for 2 seconds after event | Capture exact state at action time |
| **Change-detection** | Compare current frame hash to previous | Skip frame if identical | Static screens (terminal, document reading) |

Implementation:
- Frame difference detection using perceptual hashing (average hash of 8x8 downscaled grayscale image). Cost: ~1ms per frame.
- The `SCREEN_CAPTURE_FPS` config already exists; extend it with `IDLE_FPS`, `ACTIVE_FPS`, `BURST_FPS` settings.

#### 4.4 Compression Strategy

| Strategy | Approach | Data Rate (1080p) | CPU Impact | Quality |
|----------|----------|-------------------|------------|---------|
| **Current (CRF 0)** | Lossless H.264 | ~50-100 MB/min | High (veryslow) | Perfect |
| **Lossy H.264 (CRF 23)** | Standard quality | ~5-10 MB/min | Medium | Good (imperceptible loss) |
| **Lossy H.264 (CRF 28)** | Lower quality | ~2-5 MB/min | Low | Acceptable (slight blur on small text) |
| **Adaptive** | CRF 18 for burst frames, CRF 28 for idle | ~3-8 MB/min | Medium | High where it matters |
| **Diff-based PNG** | Only encode changed regions | ~1-10 MB/min (varies) | Low | Perfect (for changed regions) |

**Recommendation**: Adaptive CRF with a faster preset.

- Default: CRF 23, preset `fast` (vs current CRF 0, preset `veryslow`). This reduces both storage and CPU by an order of magnitude while maintaining visually lossless quality.
- Burst frames (at time of user action): CRF 18 for maximum clarity.
- Store screenshots (PNG) for every action event, in addition to video. This enables frame-accurate retrieval without video seeking and serves as a backup if video is corrupted.
- Change the video pixel format from `yuv444p` (lossless) to `yuv420p` (standard, 50% smaller) for 24/7 mode. Keep `yuv444p` available as a config option for demo recording.

#### 4.5 Platform Permissions

| Platform | Permission | How to Request | Notes |
|----------|------------|----------------|-------|
| **macOS** | Screen Recording | `CGRequestScreenCaptureAccess()` (macOS 10.15+); prompt appears on first use | `mss` triggers this automatically. Must be granted in System Preferences > Privacy > Screen Recording. |
| **macOS** | Accessibility (keyboard/mouse) | `AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})` | `pynput` requires this. Already checked in `openadapt_capture.platform.darwin`. |
| **macOS** | Microphone (audio) | `AVCaptureDevice.requestAccess(for: .audio)` | Only if audio capture is enabled. |
| **Windows** | Screen capture | No special permission needed | Works out of the box. |
| **Windows** | Input capture | No special permission needed | Admin not required for pynput on Windows. |
| **Windows** | Microphone | Microphone privacy setting | Windows 10/11 has per-app microphone access. |
| **Linux (X11)** | Screen capture | `DISPLAY` env var must be set | Works out of the box with X11. |
| **Linux (Wayland)** | Screen capture | XDG Portal (PipeWire) | Wayland is significantly more restrictive. `mss` does not work on Wayland without XWayland. Requires `xdg-desktop-portal` and user consent each session. |
| **Linux (Wayland)** | Input capture | `libinput` or `/dev/input` (root required) or XDG Portal | `pynput` uses X11 extensions; does not work natively on Wayland without XWayland. |

**Wayland challenge**: Wayland is a significant problem for 24/7 recording. The security model prevents applications from capturing the screen or global input events without explicit, per-session user consent via the XDG Desktop Portal. This means:
- The app must use `xdg-desktop-portal` APIs (PipeWire for screen capture, RemoteDesktop portal for input).
- The user will see a consent dialog each time the app starts recording.
- There is no way to bypass this by design.

**Recommendation**: For v0.1, support X11 on Linux (which is what `mss` and `pynput` already support). Add Wayland support in a later release using PipeWire integration. Document the Wayland limitation clearly.

#### 4.6 Storage Format

**Recommendation**: Directory-based capture format.

```
captures/
  2026-03-02_14-30-00_abc123/
    meta.json                    # Session metadata (platform, screen size, duration, etc.)
    events.db                    # SQLite database (Pydantic-based CaptureStorage)
    video/
      chunk_0000.mp4             # 10-minute video chunks (ChunkedVideoWriter)
      chunk_0001.mp4
      ...
    screenshots/
      0001_1709394600.123_click.png   # Action screenshots (sequential + timestamp + type)
      0002_1709394601.456_type.png
      ...
    audio/
      audio.flac                 # Full audio recording (if enabled)
      transcript.json            # Whisper transcription (if enabled)
    state.json                   # Resumption state for crash recovery
```

**Why directory-based, not a single file?**
- Video chunks are independently recoverable on crash.
- Screenshots can be accessed without parsing video.
- SQLite database can be queried directly.
- Easy to compress/upload individual components.
- Compatible with existing `openadapt-capture` patterns.

---

## 5. PII Scrubbing Pipeline

### Current State of `openadapt-privacy`

The privacy module (`/Users/abrichr/oa/src/openadapt-privacy`) provides:

| Capability | Implementation | Performance |
|------------|---------------|-------------|
| **Text scrubbing** | Presidio Analyzer + Anonymizer with spaCy `en_core_web_trf` transformer model | ~50-200ms per text chunk |
| **Image scrubbing** | Presidio Image Redactor (OCR + NER + redaction) | ~1-5 seconds per image |
| **Dict scrubbing** | Recursive scrubbing of nested dictionaries by key name | Fast (delegates to text scrubbing) |
| **Detected entities** | PERSON, EMAIL_ADDRESS, PHONE_NUMBER, US_SSN, CREDIT_CARD, DATE_TIME, LOCATION | Configurable ignore list |
| **Configuration** | `PrivacyConfig` dataclass with scrub keys, fill color, language, model settings | Global config singleton |

### Design Philosophy: Raw-Then-Review

**The recording pipeline saves raw, unscrubbed data to local disk. Every recording has a review state that gates ALL outbound data transmission — not just storage uploads, but also VLM API calls (OpenAI, Anthropic, Google), annotation pipelines, federated learning, sharing, and any future feature that sends data off-machine.**

| Aspect | Scrub-before-write (rejected) | Raw-then-review (chosen) |
|--------|-------------------------------|--------------------------|
| Data fidelity | Lossy — can't undo over-scrubbing | Lossless — raw always available locally |
| Privacy risk (local) | Lower — PII never hits disk | Higher — but it's the user's own machine, their own data |
| Egress safety | Implicit (automated) | Explicit (human-in-the-loop) |
| User trust | "Trust us, we scrubbed it" | "Here's exactly what we're sending" |
| Training data quality | Scrubbing artifacts may hurt model | Clean separation: raw for local use, scrubbed for egress |
| Debuggability | Can't review what was scrubbed | Full audit trail of before/after |

**Rationale**: For an open-source project collecting user screen recordings, trust is paramount. Users should see EXACTLY what data leaves their machine. Automated scrubbing that the user can't review is a black box — and a black box that captures everything on your screen is a hard sell.

### Recording Review State Machine

Every recording has a **review status** that persists in the index database. This status gates all outbound data paths.

```
                          ┌──────────────┐
                          │   CAPTURED   │  ← Initial state. Raw on disk.
                          │  (pending)   │     NOTHING can send this data
                          └──────┬───────┘     off-machine.
                                 │
                    ┌────────────┼────────────┐
                    │            │            │
                    ▼            ▼            ▼
           ┌──────────┐  ┌───────────┐  ┌──────────┐
           │ SCRUBBED  │  │ DISMISSED │  │ DELETED  │
           │ (pending  │  │ (user     │  │          │
           │  review)  │  │  accepted │  └──────────┘
           └─────┬─────┘  │  risks)   │
                 │         └─────┬─────┘
                 ▼               │
           ┌──────────┐         │
           │ REVIEWED  │         │
           │ (user     │         │
           │  approved │         │
           │  scrubbed │         │
           │  copy)    │         │
           └─────┬─────┘         │
                 │               │
                 ▼               ▼
           ┌─────────────────────────┐
           │     CLEARED FOR EGRESS  │  ← Data can now be sent to:
           │                         │    storage backends, VLM APIs,
           │                         │    annotation pipelines, FL, etc.
           └─────────────────────────┘
```

**State definitions:**

| State | Meaning | Data can leave machine? |
|-------|---------|------------------------|
| `captured` | Raw recording just created. Pending review. | **No** — blocked from all egress |
| `scrubbed` | Scrub pass completed, awaiting user review | **No** — still pending human approval |
| `reviewed` | User reviewed scrubbed copy and approved | **Yes** — scrubbed copy only |
| `dismissed` | User skipped scrubbing, accepted PII risks | **Yes** — raw data, user's choice |
| `deleted` | User deleted the recording | N/A |

#### Where data can leave the machine (ALL gated by review state)

| Egress Path | What is sent | Gated? |
|-------------|-------------|--------|
| S3 / R2 / HF Hub / MinIO upload | Full recording archive | Yes — must be `reviewed` or `dismissed` |
| OpenAI Vision API (annotation) | Individual screenshots | Yes — must be `reviewed` or `dismissed` |
| Anthropic Claude API (annotation) | Individual screenshots | Yes — must be `reviewed` or `dismissed` |
| Google Gemini API (annotation) | Individual screenshots | Yes — must be `reviewed` or `dismissed` |
| Federated learning (gradient upload) | Model gradients (derived from data) | Yes — must be `reviewed` or `dismissed` |
| Magic Wormhole (P2P sharing) | Full recording | Yes — must be `reviewed` or `dismissed` |
| Error reporting / telemetry | Could contain screenshot fragments | Yes — stripped of all capture data |

**Implementation**: A single `check_egress_allowed(capture_id) -> bool` function that every outbound code path calls. If the recording is in `captured` or `scrubbed` state, the function raises `EgressBlockedError` with a user-facing message: "This recording hasn't been reviewed yet. Open the review panel to approve it for sharing."

### Persistent Pending State & UI

Every recording shows its review status in the capture browser:

```
Recent Recordings
─────────────────────────────────────────────
 ● 2026-03-02 14:30  (2h 15m, 1.2 GB)  [⚠ Pending Review]
 ● 2026-03-02 10:00  (45m, 0.3 GB)     [✓ Reviewed]
 ● 2026-03-01 09:00  (8h, 4.1 GB)      [⚠ Pending Review]
 ● 2026-02-28 13:00  (1h, 0.5 GB)      [✓ Dismissed]
─────────────────────────────────────────────
 3 recordings pending review (5.6 GB)
 [Review All]  [Dismiss All]
```

The tray icon can show a badge count of pending reviews. Periodic reminders (configurable) nudge the user to review accumulated recordings.

#### 5.1 Scrubbing and Review Flow

1. **Recording completes** → state is `captured` (pending review)
2. **User opens review panel** (from tray menu, capture browser, or reminder notification)
3. **User chooses action**:
   - **"Run Scrubbing"** → scrub worker creates parallel scrubbed copy → state becomes `scrubbed` → review UI shows before/after diff → user approves → state becomes `reviewed`
   - **"Dismiss (skip scrubbing)"** → warning: "Your raw recordings may contain passwords, personal information, or sensitive data. They will be uploadable as-is." → user confirms → state becomes `dismissed`
   - **"Delete"** → recording deleted from disk → state becomes `deleted`
4. **Once `reviewed` or `dismissed`** → recording is cleared for any egress path (upload, VLM annotation, sharing, etc.)

This means:
- The raw capture is never modified
- The user always has the original
- Scrubbing can be re-run with different settings without re-recording
- False positives (over-scrubbing) are caught before any data leaves
- VLM annotation can't accidentally send unreviewed screenshots to OpenAI/Anthropic/Google
- The pending state is visible and persistent — it doesn't go away until the user handles it

#### 5.2 Scrubbing Levels

| Method | Coverage | Speed | Size | False Positives |
|--------|----------|-------|------|----------------|
| **Regex only** | Credit cards, SSNs, emails, phone numbers, IPs | <1ms per text | 0 MB | Low |
| **Presidio + spaCy `en_core_web_sm`** | Above + person names, locations, dates | ~10-50ms per text | ~50 MB | Medium |
| **Presidio + spaCy `en_core_web_trf`** | Best NER accuracy | ~50-200ms per text | ~500 MB | Low |
| **On-device LLM (e.g., Phi-3-mini)** | Contextual understanding | ~500ms-2s | ~2-4 GB | Lowest |

**Recommendation**: Ship regex scrubbing by default (zero extra weight). Include spaCy models as optional downloadable enhancements:

```
Settings > Privacy > Scrubbing Level
  [x] Basic (regex patterns) -- 0 MB, fast
  [ ] Standard (spaCy small model) -- 50 MB download, fast
  [ ] Enhanced (spaCy transformer model) -- 500 MB download, slower
```

#### 5.3 PII Types and Detection

| PII Type | Detection Method | Scrub Level | Visual in Review |
|----------|-----------------|-------------|-----------------|
| Credit cards | Luhn-validated regex | Basic | Yellow highlight on text events |
| SSNs | Regex pattern | Basic | Yellow highlight |
| Email addresses | Regex | Basic | Yellow highlight |
| Phone numbers | Regex | Basic | Yellow highlight |
| Passwords | Password field detection (window metadata) | Basic | Red highlight (always redacted) |
| Person names | NER (spaCy) | Standard | Orange highlight |
| Addresses | NER (spaCy) | Standard | Orange highlight |
| On-screen text PII | OCR + NER (Presidio Image Redactor) | Standard | Red overlay boxes on screenshots |

#### 5.4 Scrubbed Copy Format

The scrub pass creates a parallel directory structure:

```
captures/
  2026-03-02_session/           # Raw (never modified)
    events.db
    video/chunk_0000.mp4
    screenshots/0001_click.png
  2026-03-02_session.scrubbed/  # Scrubbed copy (created on demand)
    events.db                   # Text events redacted
    screenshots/0001_click.png  # PII regions blurred/filled
    scrub_manifest.json         # What was scrubbed, where, why
    review_status.json          # User approvals/rejections
```

The `scrub_manifest.json` enables the review UI to show exactly what changed:

```json
{
  "scrub_level": "standard",
  "timestamp": "2026-03-02T14:30:00Z",
  "redactions": [
    {"type": "text", "event_id": 42, "field": "key_char", "original_hash": "sha256:...", "entity": "CREDIT_CARD", "confidence": 0.98},
    {"type": "image", "file": "screenshots/0001_click.png", "regions": [{"x": 100, "y": 200, "w": 300, "h": 20, "entity": "EMAIL_ADDRESS"}]}
  ]
}
```

#### 5.5 Scrubbing is Optional

Scrubbing is **recommended but not mandatory**. Users who are uploading their own personal recordings and don't care about PII can skip the scrub step entirely — they just confirm they've reviewed the raw data and consent to upload it as-is. The consent dialog makes this explicit (see Section 8).

---

## 6. Storage & Data Management

### 6.1 Estimated Data Rates

Assumptions: 1920x1080 resolution, single monitor.

| Component | Raw Rate | After Compression | Notes |
|-----------|----------|-------------------|-------|
| Video (10 FPS, CRF 0) | ~6 GB/hour | ~6 GB/hour (lossless) | Current default. Unsustainable for 24/7. |
| Video (10 FPS, CRF 23) | ~6 GB/hour | ~0.5-1 GB/hour | Recommended for 24/7. |
| Video (adaptive FPS, CRF 23) | Varies | ~0.1-0.5 GB/hour | With idle detection. |
| Screenshots (per action) | ~2-5 MB per PNG | ~0.5-1 MB per WebP | ~100-500 actions/hour typical. ~50-500 MB/hour. |
| Events (SQLite) | ~1-5 KB per event | Same (already compact) | ~10-50 MB/hour. Negligible. |
| Audio (FLAC) | ~50-100 MB/hour | Same (FLAC is lossless compressed) | Optional. |

**Conservative estimate for 24/7 recording (adaptive FPS, CRF 23, no audio):**
- Per hour: ~0.2 GB (video) + ~0.1 GB (screenshots) + ~0.01 GB (events) = ~0.3 GB/hour
- Per day: ~7 GB
- Per week: ~50 GB
- Per month: ~200 GB

**Aggressive estimate (10 FPS constant, CRF 23, with audio):**
- Per day: ~25 GB
- Per month: ~750 GB

### 6.2 Storage Tiers

| Tier | Age | Format | Location | Queryable? |
|------|-----|--------|----------|-----------|
| **Hot** | Last 24 hours | Raw captures (video chunks + screenshots + SQLite) | `captures/` directory | Yes (full query) |
| **Warm** | 1-7 days | Compressed archives (tar.zst per capture session) | `archive/` directory | Metadata only |
| **Cold** | 7+ days | Uploaded to cloud + local tombstone | Cloud (S3/R2) + `tombstones/` | Via cloud API |
| **Deleted** | Past retention period | Deleted locally and from cloud | Nowhere | No |

### 6.3 Automatic Cleanup Policies

```
Settings > Storage
  Maximum disk usage: [50] GB
  Retention period:   [30] days
  Archive after:      [24] hours
  Delete after upload: [x] (keep tombstone with metadata only)

  Current usage: 12.3 GB of 50 GB
  [====--------] 24.6%
```

**Cleanup algorithm** (runs every hour):
1. Calculate total disk usage of `captures/` + `archive/`.
2. If usage > max, delete oldest cold-tier archives first.
3. If still over, compress oldest hot-tier captures to warm tier.
4. If still over, delete oldest warm-tier archives.
5. Never delete hot-tier captures that are currently recording.
6. Always respect minimum retention period (user-configurable, default 24 hours).

### 6.4 Database for Metadata

| Option | Pros | Cons |
|--------|------|------|
| **SQLite** | Already used by openadapt-capture; proven; zero config; excellent Python support; WAL mode for concurrent access | Single-writer; no native analytics |
| **DuckDB** | Excellent analytics (columnar); SQL; Python-native | Overkill for metadata; adds dependency; not as battle-tested for concurrent write |
| **Plain JSON files** | Simplest; human-readable; no dependency | No query capability; no ACID; slow for large datasets |

**Recommendation**: SQLite for everything.

- Per-capture database: `events.db` (already exists in openadapt-capture's `CaptureStorage` class).
- Global index database: `~/.openadapt/index.db` containing metadata about all captures (ID, path, start time, duration, event count, size, upload status). This enables fast browsing without opening each capture's database.
- WAL mode for concurrent access (the recording process writes while the UI reads).

---

## 7. Storage Backends & Trust Architecture

### 7.1 Design Principles

1. **Storage backends are plugins** — a `StorageBackend` protocol with multiple implementations
2. **Multiple backends can be active simultaneously** (e.g., local + S3 + HF Hub)
3. **Build-time feature flags** physically exclude code for unused backends — verifiable, not just configurable
4. **Runtime config** controls which built-in backends are active
5. **User reviews and consents** before every upload (see Section 8)

### 7.2 All Storage Backends

| Backend | Operator | Data Visibility | Cost | Best For |
|---------|----------|----------------|------|----------|
| **Local only** | User | Private | Free | Air-gapped, maximum privacy |
| **S3 (BYO)** | Customer (e.g., Enterprise) | Customer-controlled | Customer pays | Enterprise, data sovereignty |
| **S3 (OpenAdapt-hosted)** | OpenAdapt | OpenAdapt-controlled | OpenAdapt pays | Managed collection |
| **Cloudflare R2** | OpenAdapt or BYO | Configurable | Free 10 GB, then $0.015/GB | Cost-optimized community collection |
| **HuggingFace Hub** | HF (public) | Public | Free unlimited (public) | Open-source dataset contribution |
| **MinIO** | Customer (on-prem) | Customer-controlled | Free (self-hosted) | Enterprise on-prem, no cloud |
| **IPFS** | Decentralized | Public | Free (pinning costs) | Censorship-resistant archival |
| **Magic Wormhole** | P2P | Ephemeral | Free | Ad-hoc sharing (already exists in openadapt-capture) |
| **Federated** | Aggregation server | Gradients only (no raw data) | Server cost | Model improvement without data sharing (see Section 9) |

### 7.3 Backend Plugin Protocol

```python
class StorageBackend(Protocol):
    """Every storage destination implements this."""
    name: str
    supports_delete: bool
    supports_list: bool

    def upload(self, archive_path: Path, metadata: dict) -> UploadResult: ...
    def delete(self, recording_id: str) -> bool: ...
    def list_uploads(self) -> list[UploadRecord]: ...
    def verify_credentials(self) -> bool: ...
    def estimate_cost(self, size_bytes: int) -> float | None: ...
```

All backends share the same upload pipeline:

```
[User approves in review UI] → Compress (tar.zst) → Queue → Upload Worker
                                                        |
                                                  Backend-specific:
                                                  - S3: multipart upload
                                                  - HF Hub: git lfs push
                                                  - R2: S3-compatible multipart
                                                  - Wormhole: P2P direct
```

### 7.4 Recommended Backend Combinations

| Profile | Backends | Target User |
|---------|----------|-------------|
| `air-gapped` | Local only | Security researchers, classified environments |
| `enterprise` | Local + BYO S3 | Enterprise, enterprises with data policies |
| `enterprise-fl` | Local + BYO S3 + Federated | Enterprise + model improvement without data sharing |
| `community` | Local + HF Hub | Open-source contributors |
| `community-r2` | Local + R2 → HF Hub | OpenAdapt-curated: R2 as staging, HF as public release |
| `community-fl` | Local + HF Hub + Federated | Contributor + model improvement |
| `full` | All enabled | Development and testing |

### 7.5 Build-Time Trust Guarantees

An `.env` variable is necessary but not sufficient for enterprise trust. **Code that doesn't exist can't run.** The strongest guarantee is compile-time exclusion via feature flags.

#### Tauri Feature Flags (Rust, compile-time)

```toml
# src-tauri/Cargo.toml
[features]
default = ["local-storage"]
local-storage = []
s3-upload = ["dep:aws-sdk-s3"]
r2-upload = ["s3-upload"]              # R2 is S3-compatible
hf-upload = ["dep:reqwest"]
federated = ["dep:tch"]
# Build profiles
enterprise = ["local-storage", "s3-upload"]
community = ["local-storage", "hf-upload", "r2-upload"]
full = ["enterprise", "community", "federated"]
```

#### Python Optional Dependencies (pip extras)

```toml
# pyproject.toml
[project.optional-dependencies]
enterprise = ["boto3"]
community = ["huggingface_hub"]
federated = ["flwr", "torch"]
full = ["openadapt-desktop[enterprise,community,federated]"]
```

#### How Enterprise Customers Verify

```bash
# Build enterprise-only binary
cargo build --features enterprise

# Verify: literally zero HuggingFace code in the binary
strings target/release/openadapt-desktop | grep -i huggingface   # → empty
strings target/release/openadapt-desktop | grep -i "hf_"          # → empty

# Verify: Python sidecar has no huggingface_hub
unzip openadapt_sidecar.zip && grep -r "huggingface" .            # → empty

# Verify: no outbound traffic to unexpected hosts
OPENADAPT_NETWORK_AUDIT_LOG=true openadapt-desktop &
# ... use the app ...
grep -v "s3.us-east-1.amazonaws.com" ~/.openadapt/audit.jsonl     # → only local entries
```

This is **provable** — not "we promise it's off" but "the code literally isn't there."

### 7.6 Runtime Configuration

```bash
# .env — only backends included at build time can be activated

# Master switch
OPENADAPT_STORAGE_MODE=enterprise       # air-gapped | enterprise | community | full

# S3 (BYO — enterprise customer's own bucket)
OPENADAPT_S3_BUCKET=company-openadapt-recordings
OPENADAPT_S3_REGION=us-east-1
OPENADAPT_S3_ACCESS_KEY_ID=AKIA...
OPENADAPT_S3_SECRET_ACCESS_KEY=...
OPENADAPT_S3_ENDPOINT=                  # Custom endpoint for S3-compatible (MinIO, R2)
OPENADAPT_S3_KMS_KEY_ID=               # Server-side encryption with customer-managed key

# R2 (community/managed builds)
OPENADAPT_R2_ACCOUNT_ID=...
OPENADAPT_R2_ACCESS_KEY_ID=...
OPENADAPT_R2_SECRET_ACCESS_KEY=...
OPENADAPT_R2_BUCKET=openadapt-recordings

# HuggingFace Hub (community builds)
OPENADAPT_HF_REPO=OpenAdaptAI/desktop-recordings
OPENADAPT_HF_TOKEN=hf_...
OPENADAPT_HF_PRIVATE=false              # Private dataset (paid HF tier)

# Federated learning
OPENADAPT_FL_SERVER=https://fl.openadapt.ai
OPENADAPT_FL_ROUNDS_PER_DAY=1
OPENADAPT_FL_MIN_LOCAL_SAMPLES=100

# Upload controls (apply to ALL backends)
OPENADAPT_UPLOAD_REQUIRE_REVIEW=true    # User must review before ANY upload
OPENADAPT_UPLOAD_BANDWIDTH_LIMIT=5      # MB/s
OPENADAPT_UPLOAD_SCHEDULE=idle          # idle | always | manual | cron:0 2 * * *

# Audit
OPENADAPT_NETWORK_AUDIT_LOG=true
OPENADAPT_AUDIT_LOG_PATH=~/.openadapt/audit.jsonl
```

#### Config Validation Chain (Startup)

1. **Build includes backend?** → If `STORAGE_MODE=community` but built with `--features enterprise`, hard error: "This build does not include HuggingFace support. Install the community edition or set STORAGE_MODE=enterprise."
2. **Credentials valid?** → Verify S3 bucket access, HF token, etc.
3. **Log the active configuration** → `audit.jsonl` records: `{"event": "startup", "storage_mode": "enterprise", "active_backends": ["s3"], "excluded_backends": ["hf", "r2", "ipfs", "federated"]}`

### 7.7 Upload Implementation Details

- **S3/R2**: Multipart upload (min 5 MB per part, max 10,000 parts). Each capture session uploaded as a `tar.zst` archive. Upload queue persisted to SQLite (`index.db`) to survive app restarts. Bandwidth limiter using token bucket algorithm.
- **HuggingFace Hub**: Uses `huggingface_hub` Python library. Uploads as dataset shards to a community dataset repo (e.g., `OpenAdaptAI/desktop-recordings`). Versioned via Git LFS. Built-in dataset viewer lets community browse without downloading.
- **Magic Wormhole**: Already integrated in `openadapt-capture`. P2P transfer, no storage needed, both parties must be online simultaneously. Good for ad-hoc sharing.
- **MinIO**: Same S3 API as AWS, but self-hosted. Enterprise customers deploy MinIO on-prem and point `OPENADAPT_S3_ENDPOINT` at it.

### 7.8 Encryption

| Level | What is Encrypted | Key Management | Default? |
|-------|-------------------|----------------|----------|
| **Transport** | Data in transit (HTTPS/TLS) | Standard TLS | Always on |
| **At rest (server-side)** | Data on cloud storage | Provider manages keys (SSE-S3) or customer KMS | Default for S3 |
| **At rest (client-side)** | Data encrypted before upload | AES-256-GCM key in OS keychain | Optional |
| **End-to-end** | Only authorized consumers can decrypt | Key exchange via account or manual | Future |

### 7.9 Network Audit Log

Every outbound network request is logged to an append-only JSONL file:

```jsonl
{"ts":"2026-03-02T10:00:01Z","event":"startup","storage_mode":"enterprise","backends":["s3"],"excluded":["hf","r2","ipfs"]}
{"ts":"2026-03-02T10:05:00Z","event":"upload_start","backend":"s3","dest":"s3://company-bucket/rec-001.tar.zst","size_mb":142}
{"ts":"2026-03-02T10:05:32Z","event":"upload_complete","backend":"s3","dest":"s3://company-bucket/rec-001.tar.zst","bytes_sent":148897280}
{"ts":"2026-03-02T10:05:32Z","event":"network_summary","total_outbound_bytes":148897280,"destinations":["s3.us-east-1.amazonaws.com"]}
```

Enterprise IT can: (a) grep the audit log for unexpected destinations, (b) set firewall rules allowing only their S3 endpoint, (c) use a network proxy to independently verify, (d) run `OPENADAPT_STORAGE_MODE=air-gapped` and confirm zero outbound traffic.

### 7.10 Three-Layer Trust Model

```
BUILD TIME                    RUNTIME                      USER ACTION
─────────                     ───────                      ───────────
Feature flags                 .env config                  Review + consent
(code inclusion)              (backend activation)         (per-upload approval)
     │                             │                            │
     ▼                             ▼                            ▼
┌──────────┐  validates    ┌──────────────┐  gates      ┌─────────────┐
│ Binary    │─────────────→│ Active       │────────────→│ Upload      │
│ contains: │              │ backends:    │             │ review UI:  │
│ □ S3      │              │ ☑ S3 (BYO)  │             │ "Upload X   │
│ □ R2      │              │ ☐ R2        │             │  to S3?"    │
│ □ HF Hub  │              │ ☐ HF Hub   │             │ [Approve]   │
│ □ Federated│             │ ☐ Federated │             │ [Reject]    │
└──────────┘              └──────────────┘             └─────────────┘
                                │                            │
                                ▼                            ▼
                          ┌──────────┐                ┌──────────┐
                          │ Audit    │                │ Audit    │
                          │ log      │                │ log      │
                          └──────────┘                └──────────┘
```

Three independent layers, each sufficient on its own, composable for defense in depth.

---

## 8. Upload Review & Consent UX

### 8.1 Review UI Architecture

The review UI is the gate between local recordings and any outbound data path. It reuses the existing `openadapt-capture` HTML viewer (`create_html()`) with a new `review_mode=True` parameter. Tauri loads this HTML in its WebView.

| Option | Approach | Pros | Cons |
|--------|----------|------|------|
| **A. Extend `create_html()` in Tauri WebView** | Generate review HTML, load in Tauri window, bridge actions via `window.__TAURI__.invoke()` | Reuses proven viewer code, native integration | Bridge complexity |
| **B. Tauri-native review panel** | Build review UI in JS/HTML from scratch in Tauri | Full control, no bridge | Duplicates viewer, more code |
| **C. Standalone HTML + local server** | Python serves review page on localhost | Simple, no Tauri coupling | Extra HTTP server, less integrated |

**Recommendation**: Option A — extend the existing viewer.

### 8.2 Review Mode Features

When `review_mode=True`, the viewer adds:

1. **Redaction highlights**: Red overlay boxes on scrubbed regions in screenshots. Yellow highlights on redacted text events.
2. **Before/after toggle**: Click any redacted region to see original vs. scrubbed side-by-side.
3. **Per-redaction controls**: Accept (keep scrubbed) or Reject (restore original for this region). Rejected items are listed as "user-approved PII" in the upload.
4. **Summary panel**: "This upload contains N captures (X GB). M regions were scrubbed (N accepted, K rejected)."
5. **Consent banner** at the bottom (see 8.3).
6. **Upload button** wired to `window.__TAURI__.invoke('upload_capture', {...})`.

### 8.3 Consent Language

Consent is shown when the user **reviews and clears** a recording for egress. Once cleared, the recording can be sent to any configured destination without re-prompting. The consent text adapts to show exactly which destinations are currently enabled.

```
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

You are clearing [N] recording sessions ([X.X] GB) for
sharing with external services.

This data includes screenshots of your desktop and records
of your mouse and keyboard actions. [If scrubbing was applied:
"PII scrubbing was applied — review the highlighted regions
above to verify nothing sensitive remains." / If dismissed:
"PII scrubbing was not applied. The raw recordings will be
shared as-is."]

Once cleared, this data may be sent to:

  Storage:
  [If S3 BYO: "• Your S3 bucket at s3://[bucket-name]
    (under your organization's control)"]
  [If HF Hub: "• HuggingFace Hub as part of the public dataset
    at huggingface.co/datasets/OpenAdaptAI/desktop-recordings"]
  [If R2: "• OpenAdapt's Cloudflare R2 (may be included
    in public training datasets)"]

  AI Services (for annotation and analysis):
  [If OpenAI configured: "• OpenAI API (screenshots sent
    for Vision analysis)"]
  [If Anthropic configured: "• Anthropic API (screenshots
    sent for Claude Vision analysis)"]
  [If Google configured: "• Google API (screenshots sent
    for Gemini analysis)"]

  [If federated enabled: "Model Training:
  • Federated learning server at [FL_SERVER]
    (only model gradients are shared, not raw data)"]

  • You can delete your uploads at any time from
    Settings → My Uploads
  • OpenAdapt is open-source (MIT) — models trained on
    this data will also be open-source

By clicking "Clear for Sharing", you confirm:
  1. You have reviewed the [scrubbed/raw] recordings above
  2. You consent to this data being sent to the services
     listed above
  3. [If any public destination: "You understand this data
     may become part of a public dataset"]
  4. You have the right to share this data (it was recorded
     on your own device and does not contain others' data
     without their consent)

                    [Cancel]  [Clear for Sharing]
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
```

Key design choices:
- **Shows ALL configured egress paths**: Not just storage, but VLM APIs (OpenAI, Anthropic, Google), federated servers — everything
- **Dynamic per-configuration**: Only shows destinations that are actually enabled in the current build + runtime config
- **Public vs. private distinction**: HF Hub says "public dataset" explicitly; BYO S3 says "your organization's control"; VLM APIs are listed with their company name
- **No dark patterns**: No pre-checked boxes, no "accept all" shortcut, cancel is equally prominent
- **One-time per recording**: Once cleared, the recording doesn't re-prompt for each individual egress. The user cleared it knowing all possible destinations.
- **Open-source reciprocity**: "Models trained on this data will also be open-source"
- **Right to share**: User attests they have the right to share (important for workplace recordings)

### 8.4 Dismiss Flow (Skip Scrubbing)

Users CAN skip scrubbing entirely via the "Dismiss" action in the review panel. This still shows the full consent dialog:

1. User clicks "Dismiss (skip scrubbing)" on a pending recording
2. Warning dialog: "Your recordings will be shared WITHOUT PII removal. Screenshots may contain passwords, personal information, or sensitive data visible on your screen."
3. The consent dialog (8.3) is shown with the full list of configured destinations, with the note "PII scrubbing was not applied. The raw recordings will be shared as-is."
4. User must explicitly confirm → state becomes `dismissed` → recording is cleared for egress

### 8.5 Batch Operations

For users with many accumulated pending recordings:

- **"Review All"**: Opens a batch review panel. User can scrub all, review summary of redactions across all recordings, and clear them in one action.
- **"Dismiss All"**: Shows the warning + consent dialog once, covering all pending recordings. Good for users who don't care about PII (e.g., recording on a dedicated test machine).
- **Per-app policies** (future): "Auto-dismiss recordings from [app name]" — for users who know certain apps never show PII. Requires explicit opt-in per app.

---

## 9. Federated Learning

### 9.1 The Value Proposition

Federated learning solves the fundamental tension between privacy and model improvement:

- **Enterprise users (Enterprise)**: Get model improvements from the community WITHOUT sharing any of their data
- **Community users**: Get model improvements from enterprise users WITHOUT seeing their screens
- **OpenAdapt**: Trains better models without centralized data collection

```
┌─────────────────────────────────────────────┐
│              User's Machine                  │
│                                              │
│  Recordings → Local Training → Gradients ────┼──→ Aggregation Server
│              (fine-tune base model)           │    (averages gradients
│                                              │     from all participants)
│  Updated Model ←─────────────────────────────┼──←
│              (better local predictions)       │    Updated Global Model
└─────────────────────────────────────────────┘
```

### 9.2 What Gets Shared

| Shared | NOT Shared |
|--------|------------|
| Model weight deltas (compressed, ~1-10 MB per round) | Raw screenshots |
| Participation metadata (sample count, training loss) | Keyboard/mouse events |
| Differentially private gradients (optionally) | Any recording content |

### 9.3 Framework Comparison

| Framework | Maturity | Privacy Features | Complexity | Python-Native | Fit |
|-----------|----------|-----------------|------------|---------------|-----|
| **Flower** (flwr.ai) | Production-ready | DP plugin, secure aggregation | Medium | Yes | Best fit |
| **PySyft** | Research-grade | Built-in DP, encrypted computation | High | Yes | Over-engineered |
| **Custom gradient API** | DIY | Whatever we build | Low initial | Yes | Simplest MVP |
| **Apple-style on-device** | N/A | Complete (no server) | Very high | No | Aspirational |

**Recommendation**: Flower for v2.0. Custom gradient API for MVP experimentation.

### 9.4 Differential Privacy Guarantee

With differential privacy (ε-DP), even the gradients are provably safe:

- An adversary with full access to the aggregation server cannot reconstruct any individual user's recording
- Formal mathematical guarantee, not just "we promise"
- Flower has a built-in DP plugin (`flwr` with `DPFedAvgFixed` or `DPFedAvgAdaptive`)
- Trade-off: stronger privacy (lower ε) = noisier gradients = slower convergence

### 9.5 What Gets Trained

The federated model is a GUI agent action predictor:
- **Input**: Screenshot + task instruction
- **Output**: Next action (click coordinates, type text, scroll, etc.)
- **Base model**: Distributed to all participants (e.g., fine-tuned VLM)
- **Local training**: Each participant fine-tunes on their recordings
- **Aggregation**: Server averages weight updates from all participants

### 9.6 Federated Configuration

```bash
# .env
OPENADAPT_FL_ENABLED=true
OPENADAPT_FL_SERVER=https://fl.openadapt.ai
OPENADAPT_FL_ROUNDS_PER_DAY=1           # How often to participate
OPENADAPT_FL_MIN_LOCAL_SAMPLES=100       # Min recordings before participating
OPENADAPT_FL_DIFFERENTIAL_PRIVACY=true   # Add DP noise to gradients
OPENADAPT_FL_EPSILON=1.0                 # Privacy budget (lower = more private)
OPENADAPT_FL_MAX_UPLOAD_MB=50            # Cap gradient upload size
```

### 9.7 Federated + Enterprise: The Enterprise Scenario

Enterprise's ideal setup:
1. Build with `--features enterprise` (no HF/R2 code in binary)
2. Set `OPENADAPT_FL_ENABLED=true` — only model gradients leave the machine
3. Set `OPENADAPT_FL_DIFFERENTIAL_PRIVACY=true` — formal privacy guarantee
4. Their data stays in their S3 bucket. Their model gets better from community contributions.
5. Community model gets better from Enterprise's gradient contributions (without seeing Enterprise's data).

This is the best of both worlds: **privacy AND collective intelligence.**

### 9.8 Phasing

| Phase | Scope | Timeline |
|-------|-------|----------|
| v0.1-v0.5 | No federated (data collection focus) | Months 1-6 |
| v1.0 | Custom gradient API: manual model update sharing | Month 8 |
| v2.0 | Flower integration: automated federated rounds | Month 12+ |
| v3.0 | Secure aggregation + DP: enterprise-grade privacy | Month 18+ |

---

## 10. Auto-Update Mechanism

### Options Compared

| Option | Platform Support | Delta Updates | Signature Verification | Integration |
|--------|-----------------|---------------|----------------------|-------------|
| **Tauri Updater (built-in)** | macOS, Windows, Linux | No (full download) | Yes (mandatory, Ed25519) | Native to Tauri |
| **Sparkle / WinSparkle** | macOS / Windows only | Yes (Sparkle) | Yes (EdDSA/RSA) | Requires custom integration |
| **electron-updater** | macOS, Windows, Linux | Yes (differential on macOS) | Yes (code signing) | Electron-only |
| **Custom (wget + replace)** | All | No | Manual | High maintenance |

**Recommendation**: Tauri Updater plugin.

**Rationale:**
- Since we are using Tauri as the app shell, the built-in updater is the natural choice.
- It requires Ed25519 signature verification (cannot be disabled), which is excellent for security.
- The update manifest (`latest.json`) can be hosted on GitHub Releases or a custom endpoint.
- The `tauri-action` GitHub Action generates signed update bundles automatically.

### Update Channels

| Channel | Audience | Frequency | Auto-Install |
|---------|----------|-----------|-------------|
| **Stable** | All users | Every 2-4 weeks | Yes (with user notification) |
| **Beta** | Opt-in testers | Weekly | Yes (with user notification) |
| **Nightly** | Developers | Daily (on main merge) | No (manual) |

### Update Flow

1. App checks for updates on startup and every 6 hours.
2. If update available, show notification: "OpenAdapt v1.2.3 is available. [Install Now] [Later]"
3. Download update in background.
4. Verify Ed25519 signature.
5. On user approval (or auto for stable), apply update on next app restart.
6. If the Python sidecar also needs updating, the new Tauri shell includes the updated sidecar binary.

### Python Sidecar Updates

Since the Python sidecar is bundled as a binary (PyInstaller), it is updated as part of the main app update. There is no separate update mechanism for the Python component. This simplifies the update flow but means that every Python dependency change requires a full app release.

---

## 11. CI/CD & Distribution

### 11.1 Build Matrix

| Platform | Architecture | Installer Type | Code Signing |
|----------|-------------|----------------|-------------|
| macOS | Intel (x86_64) | DMG + .app bundle | Apple Developer ID + Notarization |
| macOS | Apple Silicon (aarch64) | DMG + .app bundle | Apple Developer ID + Notarization |
| Windows | x64 | NSIS installer (.exe) + MSI | Windows Authenticode (EV cert recommended) |
| Linux | x64 | AppImage + .deb | GPG signature |

### 11.2 CI Pipeline (GitHub Actions)

```yaml
name: Build & Release

on:
  push:
    tags: ['v*']
  pull_request:
    branches: [main]

jobs:
  build-python-sidecar:
    # Build Python sidecar on each platform
    strategy:
      matrix:
        os: [macos-13, macos-14, windows-latest, ubuntu-22.04]
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install pyinstaller
      - run: pip install -e ".[all]"
      - run: pyinstaller --onefile --name openadapt-engine src/engine/main.py
      - uses: actions/upload-artifact@v4
        with:
          name: sidecar-${{ matrix.os }}
          path: dist/openadapt-engine*

  build-tauri:
    needs: build-python-sidecar
    strategy:
      matrix:
        include:
          - os: macos-13
            target: x86_64-apple-darwin
          - os: macos-14
            target: aarch64-apple-darwin
          - os: windows-latest
            target: x86_64-pc-windows-msvc
          - os: ubuntu-22.04
            target: x86_64-unknown-linux-gnu
    runs-on: ${{ matrix.os }}
    steps:
      - uses: actions/checkout@v4
      - uses: actions/download-artifact@v4
      - uses: tauri-apps/tauri-action@v0
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
          TAURI_SIGNING_PRIVATE_KEY: ${{ secrets.TAURI_SIGNING_PRIVATE_KEY }}
          # macOS signing
          APPLE_CERTIFICATE: ${{ secrets.APPLE_CERTIFICATE }}
          APPLE_CERTIFICATE_PASSWORD: ${{ secrets.APPLE_CERTIFICATE_PASSWORD }}
          APPLE_SIGNING_IDENTITY: ${{ secrets.APPLE_SIGNING_IDENTITY }}
          APPLE_ID: ${{ secrets.APPLE_ID }}
          APPLE_PASSWORD: ${{ secrets.APPLE_PASSWORD }}
          APPLE_TEAM_ID: ${{ secrets.APPLE_TEAM_ID }}
        with:
          tagName: v__VERSION__
          releaseName: 'OpenAdapt Desktop v__VERSION__'
          releaseBody: 'See the changelog for details.'
          releaseDraft: true
```

### 11.3 Code Signing

| Platform | Requirement | Certificate Type | Cost | Notes |
|----------|-------------|-----------------|------|-------|
| **macOS** | Apple Developer ID + Notarization | Developer ID Application ($99/year Apple Developer Program) | $99/year | Required for Gatekeeper. Without it, users see "unidentified developer" warning. Notarization sends binary to Apple for malware scan. |
| **Windows** | Authenticode code signing | Standard code sign cert ($70-200/year) or EV cert ($300-500/year) | $70-500/year | EV cert avoids SmartScreen warnings immediately. Standard cert builds reputation over time. |
| **Linux** | GPG signature | Free (self-generated GPG key) | Free | Not enforced by the OS, but expected by package managers. |

**Recommendation**: Start with standard code signing certificates. Upgrade to EV for Windows once download volume justifies it. Apple notarization is mandatory.

### 11.4 Download Distribution

| Channel | Purpose | Notes |
|---------|---------|-------|
| **GitHub Releases** | Primary distribution; auto-generated by tauri-action | Free; handles update manifest (`latest.json`); CDN-backed |
| **https://openadapt.ai/download** | Marketing website with download links | Points to GitHub Releases or S3 bucket |
| **Homebrew Cask (macOS)** | Developer-friendly installation | `brew install --cask openadapt` |
| **winget (Windows)** | Developer-friendly installation | `winget install OpenAdapt.Desktop` |

---

## 12. System Tray & UX

### 12.1 Current State of `openadapt-tray`

The existing tray implementation provides:

| Feature | Implementation | Status |
|---------|---------------|--------|
| System tray icon | pystray with state-based icons (idle, recording, training, error) | Working |
| Context menu | Start/Stop recording, Recent captures, Training, Dashboard, Settings, Quit | Working |
| Notifications | desktop-notifier (native) with AppleScript/PowerShell/notify-send fallbacks | Working (uncommitted) |
| Global hotkeys | pynput-based hotkey manager (toggle recording, open dashboard, triple-ctrl stop) | Working |
| IPC | Socket-based IPC client for communication with backend services | Working |
| State management | `StateManager` with `TrayState` enum and listener pattern | Working |
| Platform handlers | macOS (AppleScript dialogs, LaunchAgent autostart), Windows (PowerShell), Linux (notify-send) | Working |
| Configuration | JSON config file in OS-appropriate config directory | Working |

### 12.2 Tray Design for OpenAdapt Desktop

Since we are using Tauri, the tray implementation will use Tauri's native tray plugin instead of pystray. However, the UX design from openadapt-tray is preserved.

#### Tray Icon States

| State | Icon | Tooltip | Color |
|-------|------|---------|-------|
| Idle | Circle outline | "OpenAdapt - Idle" | Gray |
| Recording | Filled circle with pulse animation | "OpenAdapt - Recording (2h 15m)" | Red |
| Recording (paused) | Filled circle with pause bars | "OpenAdapt - Paused" | Yellow |
| Uploading | Circle with up arrow | "OpenAdapt - Uploading (45%)" | Blue |
| Error | Circle with exclamation | "OpenAdapt - Error: {message}" | Red |
| Updating | Circle with refresh | "OpenAdapt - Updating..." | Blue |

#### Context Menu

```
+------------------------------------------+
| [x] Recording (2h 15m, 1.2 GB)          |   <-- Toggle; shows duration + size
|     Pause Recording                      |
|     Stop Recording                       |
+------------------------------------------+
| Recent Captures                       >  |   <-- Submenu with last 10
| Storage: 12.3 GB / 50 GB             >  |   <-- Opens storage settings
| Upload Queue: 3 pending              >  |   <-- Shows upload status
+------------------------------------------+
| Preferences...                           |   <-- Opens Tauri settings window
| About OpenAdapt                          |
| Check for Updates                        |
+------------------------------------------+
| Quit OpenAdapt                           |
+------------------------------------------+
```

#### Recording Indicator (Legal/Ethical Requirement)

**The user must ALWAYS know when recording is active.**

- The tray icon changes to a conspicuous red filled circle.
- On macOS, the menu bar icon area shows a red dot.
- On Windows, a toast notification appears when recording starts.
- On Linux, a persistent notification stays in the notification area.
- The app cannot be configured to record without a visual indicator. This is a hard requirement for legal compliance (see Section 15).

#### Settings Window (Tauri WebView)

The Tauri WebView opens a proper settings window (not a browser tab) with the following pages:

1. **General**: Start on login, recording quality preset, default recording mode
2. **Privacy**: Scrubbing level (basic/standard/enhanced), password field detection, custom patterns
3. **Storage**: Max disk usage, retention period, archive settings, current usage visualization
4. **Cloud**: Enable/disable upload, cloud provider settings, bandwidth limit, encryption
5. **Hotkeys**: Configure keyboard shortcuts
6. **About**: Version, license, update channel, check for updates

#### First-Run Experience

1. Welcome screen explaining what OpenAdapt does.
2. Permission requests:
   - macOS: Screen Recording + Accessibility
   - Windows: (none needed)
   - Linux: Explain X11 requirement
3. Privacy settings: Choose scrubbing level.
4. Storage settings: Set max disk usage (default 50 GB).
5. Optional: Sign in to cloud account for uploads.
6. "Start Recording" button.

---

## 13. Security & Privacy

### 13.1 Threat Model

This application captures EVERYTHING on the user's screen, including:
- Passwords as they are typed
- Credit card numbers, SSNs, and other financial data
- Private messages, emails, and documents
- Medical records, legal documents
- Confidential business information
- Screen content of other applications

**Primary threats:**

| Threat | Severity | Mitigation |
|--------|----------|------------|
| Data breach (local) | Critical | Encryption at rest; OS-level file permissions; raw data stays local |
| Data breach (cloud) | Critical | Client-side encryption; access controls; audit logging |
| Unauthorized recording | High | Always-visible recording indicator; user must explicitly start |
| Insider access to cloud data | High | Client-side encryption; zero-knowledge architecture option |
| Malware accessing recordings | High | OS sandbox (macOS App Sandbox, Linux AppArmor); encrypted storage |
| Supply chain attack | Medium | Code signing; reproducible builds; dependency auditing |
| Network interception | Medium | TLS for all network communication; certificate pinning |

### 13.2 Local-First Architecture

**Principle**: Nothing leaves the machine without explicit user consent.

- The app operates fully offline by default.
- Cloud upload is opt-in and clearly labeled.
- All processing (recording, scrubbing, compression) happens locally.
- The app never phones home without user knowledge (update checks can be disabled).
- No telemetry or analytics without opt-in.

### 13.3 Encryption at Rest

| Component | Encryption | Key Storage |
|-----------|-----------|-------------|
| Event database (SQLite) | SQLCipher (AES-256-CBC) | OS keychain |
| Video chunks | AES-256-GCM per file | OS keychain |
| Screenshots | AES-256-GCM per file | OS keychain |
| Audio | AES-256-GCM per file | OS keychain |
| Configuration | Not encrypted (no sensitive data) | N/A |
| Upload queue | Metadata only (paths + status); not sensitive | N/A |

**Note**: Encryption at rest is optional (default OFF for v0.1) because it adds complexity and CPU overhead. Enable by default in a later release once performance is validated. When enabled, the key is derived from a master password or stored in the OS keychain.

### 13.4 Audit Logging

All significant actions are logged to an append-only JSONL audit log (`~/.openadapt/audit.jsonl`):
- Recording started/stopped
- Upload initiated/completed/failed (with destination URL)
- Settings changed
- Data deleted (local or cloud)
- Update installed
- Permissions granted/denied
- Every outbound network request (destination, size, response code)

See Section 7.9 for network audit log format details.

### 13.5 Compliance

| Regulation | Relevance | Requirements | Implementation |
|------------|-----------|-------------|----------------|
| **GDPR** (EU) | If users in EU upload data | Right to access, right to erasure, data portability, legitimate basis for processing | Data export endpoint; delete all data button; clear privacy policy |
| **CCPA** (California) | If users in California | Right to know, right to delete, right to opt out | Same as GDPR controls |
| **HIPAA** (US healthcare) | If healthcare workers use the app | BAA required; encryption at rest and in transit; access controls; audit trail | Optional HIPAA-compliant mode with mandatory encryption and enhanced scrubbing |
| **SOC 2** (enterprise) | If enterprise customers use cloud | Security, availability, processing integrity, confidentiality | Cloud infrastructure compliance (if/when cloud is built) |

---

## 14. Architecture Diagram

### 14.1 System Architecture (ASCII)

```
+============================================================================+
|                         OpenAdapt Desktop Application                       |
|                                                                             |
|  +------------------------------+    +----------------------------------+  |
|  |     Tauri Shell (Rust)        |    |     Python Engine (Sidecar)      |  |
|  |                               |    |                                  |  |
|  |  +--------+  +--------+      |    |  +---------------------------+   |  |
|  |  | System |  | WebView|      |    |  |   Recording Controller    |   |  |
|  |  | Tray   |  | (UI)   |      |    |  |   (Start/Stop/Pause)      |   |  |
|  |  +--------+  +--------+      |    |  +---------------------------+   |  |
|  |       |           |          |    |        |         |       |       |  |
|  |  +--------+  +--------+     |    |  +------+ +------+ +-----+      |  |
|  |  | Notif. |  | Auto-  |     |    |  |Screen| |Input | |Audio|      |  |
|  |  | Plugin |  | Update |     |    |  |Capture| |Events| |Rec. |      |  |
|  |  +--------+  +--------+     |    |  |(mss) | |(pynp)| |(sd) |      |  |
|  |       |                      |    |  +------+ +------+ +-----+      |  |
|  |  +--------+                  |    |        |         |       |       |  |
|  |  | Code   |                  |    |  +---------------------------+   |  |
|  |  | Sign   |                  |    |  |   Event Processing        |   |  |
|  |  +--------+                  |    |  |   (merge clicks, drags,   |   |  |
|  |                               |    |  |    type sequences)        |   |  |
|  +------------|------------------+    |  +---------------------------+   |  |
|               |                       |        |                        |  |
|      IPC (JSON stdin/stdout)          |  +---------------------------+   |  |
|               |                       |  |   PII Scrubber (on demand)|   |  |
|               v                       |  |   (regex + Presidio)      |   |  |
|  +-----------+----------+             |  +---------------------------+   |  |
|  | Commands / Responses |             |        |                        |  |
|  | - start_recording    |             |  +---------------------------+   |  |
|  | - stop_recording     |             |  |   Storage Manager         |   |  |
|  | - get_status         |             |  |   (SQLite + video chunks  |   |  |
|  | - get_captures       |             |  |    + screenshots)         |   |  |
|  | - get_storage_usage  |             |  +---------------------------+   |  |
|  | - set_config         |             |        |                        |  |
|  | - upload_capture     |             |  +---------------------------+   |  |
|  +-----------------------+             |  |   Upload Manager          |   |  |
|                                        |  |   (S3/HF/R2/Wormhole +  |   |  |
|                                        |  |    review UI bridge)     |   |  |
|                                        |  +---------------------------+   |  |
|                                        |                                  |  |
|                                        +----------------------------------+  |
+============================================================================+
                  |                                        |
                  v                                        v
         +----------------+                    +----------------------+
         | Local Storage   |                    | Cloud Storage(s)     |
         |                 |                    | +-- S3 (BYO/hosted)  |
         | ~/.openadapt/   |                    | +-- Cloudflare R2    |
         |   captures/     |                    | +-- HuggingFace Hub  |
         |   archive/      |                    | +-- MinIO (on-prem)  |
         |   index.db      |                    | +-- Federated (grad) |
         |   audit.jsonl   |                    +----------------------+
         +----------------+
```

### 14.2 Recording Pipeline (Raw-Then-Review)

```
User Action (mouse/keyboard)
       |
       v
  pynput Listener -----> Raw Event (unscrubbed)
       |                     |
       |                     v
       |              events.db (SQLite, raw)
       |
       v
  mss Screenshot -----> Frame
       |                   |
       |            Change Detection
       |            (skip if identical)
       |                   |
       |                   v
       |            +------+-------+
       |            |              |
       |        Video Chunk    Screenshot
       |        (H.264 MP4)    (.png / .webp)
       |            |              |
       |            v              v
       |        chunk_XXXX.mp4  XXXX_timestamp_type.png
       |
       v
  Optional: Audio Recording (FLAC)
       |
       v
  ALL DATA STORED RAW ON LOCAL DISK
       |
       |  [User clicks "Prepare for Upload"]
       v
  Scrub Worker (on demand, not real-time)
       |
       +---> Create .scrubbed/ copy
       |     (regex + optional Presidio)
       |
       +---> Generate scrub_manifest.json
       |     (what was scrubbed, where, confidence)
       |
       v
  Review UI (Tauri WebView, extends create_html viewer)
       |
       +---> User reviews before/after diff
       +---> Accepts/rejects individual redactions
       +---> Reads consent text (backend-specific)
       |
       v
  [User clicks "Upload"]
       |
       +---> Upload Queue (persisted SQLite)
              |
              v
         Backend-specific upload:
         - S3: multipart upload
         - HF Hub: git lfs push
         - R2: S3-compatible multipart
         - Wormhole: P2P direct
              |
              v
         Audit log entry (destination, size, timestamp)
```

---

## 15. Dependency Management

### 15.1 Python Dependencies

The Python sidecar bundles a complete Python runtime and all dependencies into a single executable using PyInstaller (or Nuitka). End users never interact with Python, pip, or virtual environments.

| Category | Dependencies | Size Impact |
|----------|-------------|-------------|
| **Core** | pynput, mss, Pillow, pydantic | ~10 MB |
| **Video** | PyAV (bundles ffmpeg/libx264) | ~30 MB |
| **Storage** | SQLAlchemy, alembic | ~5 MB |
| **Privacy (basic)** | regex (stdlib) | 0 MB |
| **Privacy (standard)** | spacy + `en_core_web_sm` | ~50 MB |
| **Privacy (enhanced)** | spacy + `en_core_web_trf` + spacy-transformers | ~500 MB |
| **Audio** | sounddevice, soundfile | ~5 MB |
| **Upload (S3)** | boto3 (S3 client) | ~10 MB |
| **Upload (HF)** | huggingface_hub | ~5 MB |
| **Federated** | flwr (Flower), torch | ~500 MB+ |
| **Misc** | loguru, psutil, tqdm, numpy | ~20 MB |
| **Python runtime** | Python 3.12 (bundled) | ~30 MB |
| **Total (without ML models)** | | ~160 MB |

### 15.2 PyInstaller vs Nuitka

| Criterion | PyInstaller | Nuitka |
|-----------|-------------|--------|
| **Startup time** | 2-5 seconds (unpacks to temp dir) | <1 second (compiled C) |
| **Binary size** | Larger (includes bytecode + runtime) | Smaller (compiled, stripped) |
| **Compatibility** | Excellent (widely used, well-documented) | Good (may have edge cases) |
| **Build time** | Fast (~1 min) | Slow (~10-30 min) |
| **Anti-virus false positives** | Common (packed executable pattern) | Rare (looks like normal binary) |

**Recommendation**: Start with PyInstaller for simplicity. Switch to Nuitka if anti-virus false positives become a problem or if startup time is unacceptable.

### 15.3 Native Dependencies per Platform

| Dependency | macOS | Windows | Linux |
|------------|-------|---------|-------|
| Screen capture | mss (uses CGWindowListCreateImage) | mss (uses BitBlt) | mss (uses X11 SHM / XGetImage) |
| Input events | pynput (uses Quartz Event Taps) | pynput (uses Win32 hooks) | pynput (uses X11 Record extension) |
| Video encoding | PyAV (bundled ffmpeg/libx264) | PyAV (bundled ffmpeg/libx264) | PyAV (bundled ffmpeg/libx264) |
| Audio | sounddevice (uses CoreAudio) | sounddevice (uses WASAPI/DirectSound) | sounddevice (uses PulseAudio/ALSA) |
| WebView (Tauri) | WKWebView (system) | WebView2 (system on Win10+) | WebKitGTK (must be installed) |

**Linux note**: Tauri requires `libwebkit2gtk-4.1` and `libappindicator3-1` on Linux. These must be listed as package dependencies in the `.deb` and documented for AppImage.

### 15.4 Virtual Environments for Development

Developers working on the Python engine use `uv` (consistent with all other OpenAdapt repos):

```bash
cd openadapt-desktop
uv sync                    # Install all Python deps
uv run python -m engine    # Run the Python engine
```

The Tauri shell is developed using standard Rust/Node.js tooling:

```bash
cd src-tauri
cargo build                # Build Rust backend
npm install                # Install frontend deps
npm run tauri dev          # Run in dev mode (Tauri + Python engine)
```

---

## 16. Testing Strategy

### 16.1 Test Pyramid

| Level | Scope | Tools | Runs In |
|-------|-------|-------|---------|
| **Unit tests** | Python engine functions (event processing, storage, scrubbing, compression) | pytest | CI (all platforms) |
| **Integration tests** | Recording pipeline (capture + process + store), scrubbing pipeline (detect + redact) | pytest + fixtures | CI (all platforms) |
| **E2E tests** | Full app lifecycle (install, first-run, record, stop, browse captures) | Tauri test framework + synthetic input | CI (headful; macOS + Windows) |
| **Performance tests** | CPU/RAM under 24/7 recording, storage growth rate, frame rate stability | pytest-benchmark + psutil monitoring | Nightly CI |
| **Platform tests** | macOS permissions, Windows UAC, Linux X11/Wayland detection | Platform-specific test suites | CI per platform |

### 16.2 Testing Screen Recording in CI

**Problem**: CI environments are typically headless, but screen recording requires a display.

**Solutions by platform:**

| Platform | Solution |
|----------|----------|
| **macOS (GitHub Actions)** | macOS runners have a display. Screen Recording permission can be granted programmatically in CI via `tccutil`. |
| **Windows (GitHub Actions)** | Windows runners have a virtual display. No special permissions needed. |
| **Linux (GitHub Actions)** | Use `Xvfb` (X Virtual Framebuffer): `xvfb-run -a pytest tests/`. Or use `xdummy` for a persistent virtual display. |

```yaml
# Example: Linux CI with virtual display
- name: Install Xvfb
  run: sudo apt-get install -y xvfb

- name: Run tests with virtual display
  run: xvfb-run -a --server-args="-screen 0 1920x1080x24" uv run pytest tests/ -v
```

### 16.3 Performance Benchmarks

| Metric | Target | Measurement |
|--------|--------|-------------|
| CPU usage (idle, recording active) | <5% of one core | psutil in test |
| CPU usage (active user, 10 FPS) | <15% of one core | psutil in test |
| RAM usage (steady state, 24h) | <200 MB | psutil in test (monitor over 1h, extrapolate) |
| RAM growth rate | <1 MB/hour (no leaks) | Memory snapshot comparison |
| Frame capture latency | <50ms per frame | Timestamp comparison |
| Event processing latency | <5ms per event | Timestamp comparison |
| Disk write throughput | >10 MB/s sustained | Storage benchmark |
| Startup time (app launch to tray icon visible) | <3 seconds | Timer in test |
| Recording start latency | <1 second | Timer in test |

---

## 17. Licensing & Legal

### 17.1 License Choice

| License | Pros | Cons | Used By |
|---------|------|------|---------|
| **MIT** | Maximum permissive; simple; industry standard | No copyleft protection; companies can close-source forks | All existing OpenAdapt repos |
| **Apache 2.0** | Permissive + patent grant; CLAs common | Slightly more complex than MIT | Kubernetes, TensorFlow |
| **AGPL 3.0** | Strong copyleft; protects against SaaS exploitation | Scares away enterprise users; viral license | MongoDB, Grafana |
| **BSL (Business Source License)** | Source-available; converts to open after time period | Not OSI-approved; confusing | HashiCorp, MariaDB |

**Recommendation**: MIT license.

**Rationale**: All existing OpenAdapt repos use MIT. Consistency matters. MIT is the simplest and most permissive choice, which maximizes adoption. The project's value comes from the data collection + AI training pipeline, not from the open-source code itself.

### 17.2 Third-Party Dependency Licenses

| Dependency | License | Compatible with MIT? |
|------------|---------|---------------------|
| pynput | LGPL-3.0 | Yes (dynamic linking) |
| mss (python-mss) | MIT | Yes |
| PyAV (ffmpeg bindings) | BSD + LGPL (ffmpeg) | Yes (dynamic linking). Note: ffmpeg with GPL codecs (x264) is LGPL when dynamically linked. |
| Pillow | HPND (Historical Permission Notice and Disclaimer) | Yes |
| pystray | MIT or LGPL-3.0 | Yes |
| spaCy | MIT | Yes |
| Presidio | MIT | Yes |
| Tauri | MIT or Apache 2.0 | Yes |
| SQLAlchemy | MIT | Yes |
| sounddevice | MIT | Yes |
| boto3 | Apache 2.0 | Yes |
| huggingface_hub | Apache 2.0 | Yes |
| flwr (Flower) | Apache 2.0 | Yes |

**Note on pynput and LGPL**: pynput is LGPL-3.0. When bundled via PyInstaller, it is included as a Python package (not statically linked C code). The LGPL's linking requirement is generally considered satisfied by Python's dynamic import mechanism. However, if there is any concern, we can provide the pynput source alongside the distribution as required by LGPL.

**Note on ffmpeg/x264**: PyAV bundles ffmpeg, which includes the x264 encoder (GPL). When PyAV is built with shared libraries (the default on pip), this is LGPL-compatible. If building from source, ensure `--enable-gpl` is NOT used, or use a different encoder (e.g., OpenH264, which is BSD).

### 17.3 Recording Consent

**This is the most critical legal consideration.**

| Jurisdiction | Consent Requirement | Notes |
|--------------|-------------------|-------|
| **United States (federal)** | One-party consent for audio (wiretap law) | Video recording of your own screen is generally not regulated. |
| **US states (California, Florida, etc.)** | Two-party consent for audio in some states | Affects audio recording only; screen recording of your own device is not covered by wiretap laws. |
| **EU (GDPR)** | Legitimate basis required for processing personal data | If the recording captures other people's data (e.g., shared screen, chat), GDPR applies. |
| **Workplace** | Varies by jurisdiction; many require employee notification | Employers must notify employees if their computers are monitored. |

**Recommendations:**
1. **Clear disclosure**: The app displays a permanent visual indicator when recording. This cannot be hidden.
2. **First-run consent**: The user explicitly agrees to record their screen activity.
3. **Audio recording opt-in**: Audio recording is OFF by default. If enabled, the app clearly indicates "Audio recording active" in the tray.
4. **Terms of Service**: Users agree that:
   - They are recording their own device activity.
   - They have the right to record this activity.
   - They are responsible for compliance with local laws.
   - If their recordings contain other people's data, they are responsible for obtaining consent.
5. **Enterprise mode**: An optional enterprise configuration that requires admin approval before recording starts, and logs all recording activity to a central audit server.

### 17.4 Terms of Service for Cloud Upload

If users upload recordings to the OpenAdapt cloud:
- OpenAdapt processes data only to provide the service (storage, AI training with user consent).
- Users retain ownership of their data.
- Users can delete all their data at any time.
- OpenAdapt will not sell or share user data with third parties.
- Data is stored encrypted (server-side at minimum, client-side optionally).
- Data processing location is disclosed (e.g., US-based Cloudflare R2).

---

## 18. MVP vs Full Vision

### v0.1 -- Minimum Viable Product (Target: 6-8 weeks)

**Goal**: A working desktop app that records screen activity, lets users browse recordings, and optionally upload (with review).

**Included:**
- [ ] New `openadapt-desktop` repository with Tauri 2.x shell
- [ ] Python sidecar bundling `openadapt-capture` for recording
- [ ] System tray with Start/Stop recording, recording indicator
- [ ] Raw recording to local disk (no scrubbing during capture)
- [ ] Local storage with automatic cleanup (max disk usage setting)
- [ ] Simple settings UI (recording quality, storage limit, start on login)
- [ ] Basic scrub-then-review flow (regex scrubbing + viewer-based review)
- [ ] S3 upload backend (BYO bucket — enterprise use case)
- [ ] HuggingFace Hub upload backend (public dataset contribution)
- [ ] Build profiles: `enterprise` (S3 only) and `community` (HF + R2)
- [ ] Consent dialog with backend-specific language
- [ ] Network audit logging
- [ ] macOS build (DMG, code signed + notarized)
- [ ] Windows build (NSIS installer, code signed)
- [ ] Auto-update via GitHub Releases
- [ ] CI pipeline (GitHub Actions, build + sign on tag push)

**Excluded (deferred):**
- Linux builds (AppImage/deb)
- Enhanced PII scrubbing (spaCy/Presidio)
- Audio recording
- Encryption at rest
- Federated learning
- R2, IPFS, MinIO backends

### v0.5 -- Feature Complete (Target: 3-4 months after v0.1)

**Adds:**
- [ ] Cloudflare R2 backend (OpenAdapt-hosted collection)
- [ ] Linux build (AppImage + deb)
- [ ] Enhanced PII scrubbing (spaCy small model, downloadable)
- [ ] Audio recording with Whisper transcription
- [ ] Storage tiers (hot/warm/cold)
- [ ] Upload queue with bandwidth limiting and resume
- [ ] Capture browser (view recent recordings in the settings UI)
- [ ] Adaptive frame rate (idle/active/burst)
- [ ] First-run onboarding wizard
- [ ] Update channels (stable/beta)
- [ ] MinIO backend support (on-prem enterprise)
- [ ] Build-time verification documentation for enterprise customers

### v1.0 -- Production Release (Target: 6-8 months after v0.1)

**Adds:**
- [ ] Encryption at rest (SQLCipher + file-level AES-GCM)
- [ ] Client-side encryption for cloud uploads
- [ ] Enhanced PII scrubbing (transformer model, downloadable)
- [ ] Enterprise mode (admin controls, central config, enhanced audit)
- [ ] Custom gradient API (manual federated model sharing — MVP of federated)
- [ ] Homebrew Cask + winget distribution
- [ ] Performance benchmarks in CI (regression detection)
- [ ] Wayland support (Linux, via PipeWire)
- [ ] Multi-monitor support
- [ ] IPFS backend support
- [ ] Public API documentation for cloud service

### v2.0 -- Federated Learning (Target: 12+ months after v0.1)

**Adds:**
- [ ] Flower-based automated federated learning
- [ ] Differential privacy for gradient sharing
- [ ] Secure aggregation
- [ ] Enterprise FL profile (`enterprise-fl`: data in S3, gradients to FL server)
- [ ] Community FL profile (`community-fl`: data to HF, gradients to FL server)
- [ ] Model distribution and local inference
- [ ] SOC 2 compliance for cloud + FL infrastructure

### What Can Be Deferred Indefinitely

- Mobile support (iOS/Android) -- different product
- Browser-only recording (already partially handled by Chrome extension in openadapt-capture)
- Real-time streaming to cloud (batch upload is sufficient)
- Collaborative features (multi-user annotations)
- On-device LLM for PII detection (overkill for this use case)

---

## Appendix A: Repo Structure

```
openadapt-desktop/
  .github/
    workflows/
      build.yml                # CI build + sign + release
      test.yml                 # Unit + integration tests
  src-tauri/                   # Tauri Rust backend
    src/
      main.rs                  # Entry point
      tray.rs                  # System tray setup
      commands.rs              # IPC commands (start_recording, etc.)
      sidecar.rs               # Python sidecar management
    tauri.conf.json            # Tauri configuration
    Cargo.toml                 # Features: enterprise, community, full
    icons/                     # App icons (all sizes)
  src/                         # Frontend (HTML/CSS/JS for WebView UI)
    index.html
    settings.html
    onboarding.html
    assets/
    styles/
    scripts/
  engine/                      # Python sidecar source
    __init__.py
    main.py                    # Entry point for sidecar
    controller.py              # Recording controller (start/stop/pause)
    ipc.py                     # IPC handler (JSON over stdin/stdout)
    storage_manager.py         # Storage tiers, cleanup, index
    scrubber.py                # PII scrubbing orchestration (raw → scrubbed copy)
    upload_manager.py          # Multi-backend upload with queue and resume
    backends/                  # Storage backend plugins
      __init__.py
      protocol.py              # StorageBackend protocol
      s3.py                    # S3/R2/MinIO (boto3)
      huggingface.py           # HuggingFace Hub (huggingface_hub)
      wormhole.py              # Magic Wormhole (P2P)
      federated.py             # Federated gradient upload (Flower)
    review.py                  # Upload review UI generation (extends create_html)
    config.py                  # Settings (pydantic-settings, build profile validation)
    monitor.py                 # Health monitoring (memory, disk, watchdog)
    audit.py                   # Network audit logging
  tests/
    test_engine/               # Python engine tests
    test_e2e/                  # End-to-end tests
    conftest.py
  pyproject.toml               # Python project (for engine development)
  package.json                 # Node.js (for frontend development)
  README.md
  LICENSE                      # MIT
```

## Appendix B: IPC Protocol (Tauri <-> Python Sidecar)

Messages are JSON objects, one per line, sent over stdin/stdout.

### Tauri -> Python (Commands)

```json
{"id": "uuid", "cmd": "start_recording", "params": {"quality": "standard"}}
{"id": "uuid", "cmd": "stop_recording", "params": {}}
{"id": "uuid", "cmd": "pause_recording", "params": {}}
{"id": "uuid", "cmd": "resume_recording", "params": {}}
{"id": "uuid", "cmd": "get_status", "params": {}}
{"id": "uuid", "cmd": "get_captures", "params": {"limit": 10}}
{"id": "uuid", "cmd": "get_storage_usage", "params": {}}
{"id": "uuid", "cmd": "set_config", "params": {"key": "max_storage_gb", "value": 50}}
{"id": "uuid", "cmd": "scrub_capture", "params": {"capture_id": "abc123", "level": "standard"}}
{"id": "uuid", "cmd": "get_scrub_manifest", "params": {"capture_id": "abc123"}}
{"id": "uuid", "cmd": "approve_review", "params": {"capture_id": "abc123"}}
{"id": "uuid", "cmd": "dismiss_review", "params": {"capture_id": "abc123"}}
{"id": "uuid", "cmd": "get_review_status", "params": {"capture_id": "abc123"}}
{"id": "uuid", "cmd": "get_pending_reviews", "params": {}}
{"id": "uuid", "cmd": "upload_capture", "params": {"capture_id": "abc123", "backend": "s3"}}
{"id": "uuid", "cmd": "delete_capture", "params": {"capture_id": "abc123"}}
{"id": "uuid", "cmd": "get_active_backends", "params": {}}
{"id": "uuid", "cmd": "get_egress_destinations", "params": {}}
```

### Python -> Tauri (Responses + Events)

```json
{"id": "uuid", "status": "ok", "data": {"recording": true, "duration": 3600}}
{"id": "uuid", "status": "error", "error": "Permission denied: Screen Recording"}
{"event": "recording_started", "data": {"capture_id": "abc123"}}
{"event": "recording_stopped", "data": {"capture_id": "abc123", "duration": 3600}}
{"event": "scrub_complete", "data": {"capture_id": "abc123", "redactions": 42, "manifest": "path/to/scrub_manifest.json"}}
{"event": "storage_warning", "data": {"usage_gb": 48.5, "max_gb": 50}}
{"event": "upload_progress", "data": {"capture_id": "abc123", "progress": 0.45}}
{"event": "health_warning", "data": {"type": "memory", "rss_mb": 450}}
```

## Appendix C: Research Sources

- [Tauri vs Electron 2025 comparison (Codeology)](https://codeology.co.nz/articles/tauri-vs-electron-2025-desktop-development.html)
- [Tauri vs Electron real-world comparison (levminer.com)](https://www.levminer.com/blog/tauri-vs-electron)
- [Tauri vs Electron deep technical comparison (Peerlist)](https://peerlist.io/jagss/articles/tauri-vs-electron-a-deep-technical-comparison)
- [Electron alternatives (Astrolytics)](https://www.astrolytics.io/blog/electron-alternatives)
- [Tauri updater plugin documentation](https://v2.tauri.app/plugin/updater/)
- [Tauri code signing for macOS and Windows](https://dev.to/tomtomdu73/ship-your-tauri-v2-app-like-a-pro-code-signing-for-macos-and-windows-part-12-3o9n)
- [Tauri Windows code signing](https://v2.tauri.app/distribute/sign/windows/)
- [Tauri GitHub Actions pipeline](https://v2.tauri.app/distribute/pipelines/github/)
