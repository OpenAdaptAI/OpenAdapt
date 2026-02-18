# CHANGELOG


## v0.3.0 (2026-02-18)

### Bug Fixes

- Resolve all ruff lint errors ([#7](https://github.com/OpenAdaptAI/openadapt-capture/pull/7),
  [`de00dab`](https://github.com/OpenAdaptAI/openadapt-capture/commit/de00dab66724eb786dafb80ddfc860c51be13877))

* fix: resolve all ruff lint errors

- Remove unused variable assignments (share.py, browser_bridge.py, windows.py, test_highlevel.py) -
  Add noqa comment for Quartz import needed by ApplicationServices (darwin.py) - Remove unused
  TYPE_CHECKING import (storage/__init__.py) - Add proper TYPE_CHECKING import for CaptureStats
  annotation (generate_real_capture_plot.py) - Auto-fix import sorting across multiple files

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

* docs: update README with share command and ecosystem links

- Uncomment PyPI badges (package now published as 0.3.0) - Add "Sharing Recordings" section with
  Magic Wormhole usage - Update openadapt-privacy from "Coming soon" to GitHub link - Add share
  extra to optional extras table - Add openadapt-privacy and openadapt-evals to Related Projects

* docs: remove redundant openadapt-ml training section

The detailed training workflow belongs in openadapt-ml's README. This keeps openadapt-capture
  focused on its core functionality. Users can find training info via the Related Projects link.

---------

Co-authored-by: Claude Opus 4.5 <noreply@anthropic.com>

- Throttle screen capture to reduce system lag during recording
  ([#11](https://github.com/OpenAdaptAI/openadapt-capture/pull/11),
  [`d7134a8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d7134a83921a551cd8e91ae127f63f0759a146fe))

The screen reader thread was capturing screenshots in a tight loop with no frame rate limit, causing
  high CPU and memory pressure. With action-gated video, only the most recent screenshot matters
  when an action occurs, so capturing at 100+ fps was pure waste.

Add SCREEN_CAPTURE_FPS config (default: 10 fps). The throttle sleeps for the remainder of the frame
  interval after each capture. Set to 0 for unlimited (legacy behavior). Also available as
  screen_capture_fps param on Recorder constructor and RecordingConfig.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- **ci**: Fix release automation — use ADMIN_TOKEN for protected branches
  ([#8](https://github.com/OpenAdaptAI/openadapt-capture/pull/8),
  [`4205cd5`](https://github.com/OpenAdaptAI/openadapt-capture/commit/4205cd5d2c6fd86eff4fd1e3bcd93a59aae03416))

* fix: resolve all ruff lint errors

- Remove unused variable assignments (share.py, browser_bridge.py, windows.py, test_highlevel.py) -
  Add noqa comment for Quartz import needed by ApplicationServices (darwin.py) - Remove unused
  TYPE_CHECKING import (storage/__init__.py) - Add proper TYPE_CHECKING import for CaptureStats
  annotation (generate_real_capture_plot.py) - Auto-fix import sorting across multiple files

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

* docs: update README with share command and ecosystem links

- Uncomment PyPI badges (package now published as 0.3.0) - Add "Sharing Recordings" section with
  Magic Wormhole usage - Update openadapt-privacy from "Coming soon" to GitHub link - Add share
  extra to optional extras table - Add openadapt-privacy and openadapt-evals to Related Projects

* docs: remove redundant openadapt-ml training section

The detailed training workflow belongs in openadapt-ml's README. This keeps openadapt-capture
  focused on its core functionality. Users can find training info via the Related Projects link.

* fix(ci): fix release automation — use ADMIN_TOKEN to push to protected branches

Root cause: GITHUB_TOKEN cannot push commits to protected branches. Semantic-release created the
  v0.3.0 tag (tags bypass protection) but the "chore: release 0.3.0" commit that bumps
  pyproject.toml was orphaned.

- Use ADMIN_TOKEN for checkout and semantic-release (can push to main) - Add skip-check to prevent
  infinite loops on release commits - Sync pyproject.toml version to 0.3.0 (matches latest tag)

Prerequisite: Add ADMIN_TOKEN secret (GitHub PAT with repo scope) to

repository settings.

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

---------

Co-authored-by: Claude Opus 4.5 <noreply@anthropic.com>

- **ci**: Use v9 branch config for python-semantic-release
  ([#13](https://github.com/OpenAdaptAI/openadapt-capture/pull/13),
  [`11eafca`](https://github.com/OpenAdaptAI/openadapt-capture/commit/11eafcaac1cab88dde4e861e93553fdef4b4ac1b))

* feat: disable window capture by default, add recording profiling with auto-wormhole

- Make window reader/writer conditional on RECORD_WINDOW_DATA (defaults to False), eliminating
  unnecessary thread + process + expensive platform API calls - Add throttle to read_window_events
  (0.1s) and memory_writer (1s) loops - Add profiling summary at end of record() with duration,
  event counts/rates, config flags, main thread check, and thread count - Auto-send profiling.json
  via Magic Wormhole after recording stops

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* fix: skip window requirement when RECORD_WINDOW_DATA=False, set log level to WARNING

- When window capture is disabled, skip the window timestamp requirement in process_events instead
  of discarding all action events - Set loguru log level to WARNING by default (was DEBUG) to reduce
  noise during recording

* fix: set log level to INFO not WARNING

* fix: handle missing video frames on early Ctrl+C

video_post_callback crashes with KeyError 'last_frame' when recording stops before any action
  triggers a video frame write. Guard against missing state keys and close the container gracefully.

* fix: guard window event save when capture disabled, fix PyAV pict_type compat

- Second reference to prev_window_event in process_events was unguarded, causing AttributeError when
  RECORD_WINDOW_DATA=False - PyAV pict_type="I" raises TypeError on newer versions; fall back to
  integer constant

* fix: use PictureType.I enum for PyAV pict_type, add video tests

- Use av.video.frame.PictureType.I instead of string "I" which is unsupported in current PyAV
  versions - Add test_video.py with tests for frame writing, key frames, and PictureType enum
  compatibility

* fix: use Agg backend for matplotlib, improve wormhole-not-found message

- Set matplotlib to non-interactive Agg backend so plotting works from background threads (fixes
  RuntimeError when Recorder runs record() in a non-main thread) - Improve wormhole-not-found
  message with install instructions

* fix: reset stop sequence index after match to prevent IndexError

When the stop sequence was fully matched, the index wasn't reset. Extra keypresses after the match
  would index past the end of the sequence list, causing IndexError.

* feat: add per-screenshot timing to profiling, fix stop sequence IndexError

- Track screenshot duration (avg/max/min ms) and total iteration duration per screen reader loop
  iteration in profiling.json - Reset stop sequence index after match to prevent IndexError on extra
  keypresses

* feat: make send_profile opt-in CLI flag, add magic-wormhole as regular dep

Profiling data is no longer auto-sent via wormhole after every recording. Use --send_profile flag to
  opt in. Also promotes magic-wormhole from optional [share] extra to a regular dependency since
  sharing is core functionality.

* fix: add pixel_ratio and audio_start_time to CaptureSession

HTML visualizer referenced these attributes which didn't exist on CaptureSession. Added properties
  with safe fallbacks and updated html.py to use getattr with defaults.

* fix(ci): use v9 branch config for python-semantic-release

The `branch = "main"` key is from v7/v8 and is silently ignored by v9, causing "No release will be
  made, 0.3.0 has already been released!" on every push. Use the v9 `[branches.main]` table.

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

### Features

- Copy legacy OpenAdapt recording system into openadapt-capture
  ([#9](https://github.com/OpenAdaptAI/openadapt-capture/pull/9),
  [`d359011`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d3590118edad60171c8c2ca47cee181f05d590e2))

* fix: match legacy OpenAdapt recording architecture

- Action-gated video capture: only encode frames when actions occur (~1-5 fps) instead of every
  screenshot (24fps). This is the core reason legacy OpenAdapt was smooth — not just separate
  processes. Matches legacy RECORD_FULL_VIDEO=False default behavior. - Video encoding in separate
  multiprocessing.Process (avoids GIL) - Screenshots via mss (2-4x faster than PIL.ImageGrab on
  Windows) - SIGINT ignored in worker process (main handles Ctrl+C) - Non-daemon process ensures
  video finalization on shutdown - First frame forced as key frame for seekability - Fix wormhole
  FileNotFoundError on Windows (searches Scripts/ dir)

Legacy patterns matched: - prev_screen_event buffering → _prev_screen_frame -
  prev_saved_screen_timestamp dedup → _prev_saved_screen_timestamp - RECORD_FULL_VIDEO option →
  record_full_video parameter - SIG_IGN in worker processes - mss with CAPTUREBLT=0 on Windows

Co-Authored-By: Claude Opus 4.6 <noreply@anthropic.com>

* feat: copy legacy OpenAdapt recording system into openadapt-capture

Replace vibe-coded recording internals with proven legacy OpenAdapt code, adapted only for
  per-capture databases and import paths.

New modules (copied from legacy): - db/models.py: SQLAlchemy models (Recording, ActionEvent,
  Screenshot, WindowEvent, PerformanceStat, MemoryStat) - db/crud.py: batch insert functions,
  post_process_events - extensions/synchronized_queue.py: multiprocessing queue wrapper - utils.py:
  timestamps, screenshots, monitor dims - window/: platform-specific active window capture -
  plotting.py: performance stat visualization

Updated modules: - recorder.py: full legacy record() with multi-process writers, action-gated video,
  stop sequences, SIGINT handling - capture.py: reads from SQLAlchemy DB, fixes session leak,
  mouse_pressed=None handling, disabled event filtering, adds dx/dy/button properties to Action -
  config.py: all legacy recording config values - video.py: legacy functional API wrappers - cli.py:
  wired to new recorder - pyproject.toml: added sqlalchemy, loguru, psutil, tqdm deps

Bug fixes: - Reset stop_sequence_detected on re-entry (Recorder reuse) - Close session on error in
  CaptureSession.load() - Skip click events with mouse_pressed=None - Filter disabled events in
  raw_events()

Tests: 118 passed + 6 performance tests (Windows-only)

Docs: updated README.md and CLAUDE.md to match new architecture

* fix: make pynput import conditional for headless CI

- Wrap Recorder import in try/except in __init__.py and test files - Skip Recorder tests when pynput
  unavailable (no display server) - Fix all ruff I001 import sorting violations - Remove unused
  imports and variables

* fix(ci): exclude browser bridge tests and add timeout

Browser bridge tests hang indefinitely on headless CI due to async websocket fixtures. Add
  pytest-timeout and a 10-minute job timeout.

---------

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- Unified Recorder API with config overrides and runtime properties
  ([#10](https://github.com/OpenAdaptAI/openadapt-capture/pull/10),
  [`d840e81`](https://github.com/OpenAdaptAI/openadapt-capture/commit/d840e8107d7d45347201f04958c53293f08902e6))

Restore the clean Python API from the pre-legacy design on top of the proven legacy multi-process
  recording internals.

Recorder constructor now accepts capture_video, capture_audio, and other recording options as
  keyword params that override config defaults. Adds event_count, is_recording, stats,
  wait_for_ready(), and capture properties for runtime introspection.

Changes: - config.py: Add RecordingConfig dataclass + config_override() context manager for
  temporary config patching - recorder.py: Add shared counter params to record(), fix module-level
  config reads (STOP_SEQUENCES, PLOT_PERFORMANCE), rewrite Recorder class with full API (~120 lines
  replacing ~36) - cli.py: Forward --video/--audio/--images flags to Recorder - __init__.py: Export
  RecordingConfig - tests: 11 new tests (Recorder API + config_override)

Fixes compatibility with record_waa_demos.py which passes capture_video/capture_audio and reads
  recorder.event_count.

Co-authored-by: Claude Opus 4.6 <noreply@anthropic.com>

- **share**: Add Magic Wormhole sharing for recordings
  ([`e7cfb1e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/e7cfb1ec84999184a96682ebf74c08929485fe63))

- Add share.py module with send/receive via wormhole - Add 'capture share send/receive' CLI commands
  - Add magic-wormhole as optional [share] dependency - Auto-installs wormhole if missing

Usage: capture share send ./my_recording capture share receive 7-guitarist-revenge

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>


## v0.2.0 (2026-01-29)

### Bug Fixes

- Comment out PyPI badges until package is published
  ([#3](https://github.com/OpenAdaptAI/openadapt-capture/pull/3),
  [`5aedd99`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5aedd99f6329368514cfb6340741241c3f71813a))

The PyPI version and downloads badges show "package not found" since openadapt-capture is not yet
  published to PyPI. Commenting them out until the package is released.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- Move openai-whisper to optional [transcribe] extra
  ([`9dca9e5`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9dca9e5e394b015664d982a3581c11801217d50b))

The openai-whisper package requires numba → llvmlite which only supports Python 3.6-3.9, causing
  installation failures on Python 3.12+.

Moving whisper to an optional dependency allows the meta-package (openadapt) to install successfully
  while users who need transcription can explicitly opt-in with `pip install
  openadapt-capture[transcribe]`.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Update author email to richard@openadapt.ai
  ([`1987bee`](https://github.com/OpenAdaptAI/openadapt-capture/commit/1987beeb22eed52d98b67516217d8c486ab7c37d))

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Use filename-based GitHub Actions badge URL
  ([#4](https://github.com/OpenAdaptAI/openadapt-capture/pull/4),
  [`957ca48`](https://github.com/OpenAdaptAI/openadapt-capture/commit/957ca480baf06b1b328b9f9cf65b1a483d948ea2))

The workflow-name-based badge URL was showing "no status" because GitHub requires workflow runs on
  the specified branch. Using the filename-based URL format (actions/workflows/test.yml/badge.svg)
  is more reliable and works regardless of when the workflow last ran.

Co-authored-by: Claude Sonnet 4.5 <noreply@anthropic.com>

- **ci**: Remove build_command from semantic-release config
  ([`93cdbb8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/93cdbb8ff6f87a6ad96dc74d8092bbad58a34d51))

The python-semantic-release action runs in a Docker container where uv is not available. Let the
  workflow handle building instead.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Chores

- Gitignore turn-off-nightshift test capture
  ([`62b25be`](https://github.com/OpenAdaptAI/openadapt-capture/commit/62b25be430ca2e1e5f69803c3c4db9568fbcf72f))

Test capture data (video, screenshots, database) should not be committed.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Continuous Integration

- Add auto-release workflow
  ([`c3b3eb8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/c3b3eb806ac060f3d8a98d8b5e048a0f2acfa2b2))

Automatically bumps version and creates tags on PR merge: - feat: minor version bump - fix/perf:
  patch version bump - docs/style/refactor/test/chore/ci/build: patch version bump

Triggers publish.yml which deploys to PyPI.

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Switch to python-semantic-release for automated versioning
  ([`b9246a6`](https://github.com/OpenAdaptAI/openadapt-capture/commit/b9246a60de8f83fa7d5ff32749fd0df9d0e22163))

Replaces manual commit parsing with python-semantic-release: - Automatic version bumping based on
  conventional commits - feat: -> minor, fix:/perf: -> patch - Creates GitHub releases automatically
  - Publishes to PyPI on release

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

### Documentation

- Add CLAUDE.md with development guidelines
  ([#1](https://github.com/OpenAdaptAI/openadapt-capture/pull/1),
  [`5f8fafc`](https://github.com/OpenAdaptAI/openadapt-capture/commit/5f8fafcd77c613b3c74f46546e605f75a7b1c675))

- Add overview of package purpose - Add quick commands for installation, testing, and usage - Add
  architecture overview and key components - Add links to related projects

- Add viewer screenshot to README
  ([`a22c789`](https://github.com/OpenAdaptAI/openadapt-capture/commit/a22c78952cc0e12f2a6cd742a2218a93a55a146d))

Add screenshot of the Capture Viewer HTML interface to improve documentation and show users what the
  viewer looks like.

### Features

- Add browser event capture via Chrome extension
  ([`553bb0a`](https://github.com/OpenAdaptAI/openadapt-capture/commit/553bb0ac783e7e0535b772c3840aeccf74815b20))

- Add BrowserBridge WebSocket server for Chrome extension communication - Add browser_events.py with
  Pydantic models for click, key, scroll events - Add Chrome extension with manifest v3 for DOM
  event capture - Export browser bridge API from __init__.py - Add step navigation controls to HTML
  visualizer - Comprehensive test suite (800+ lines)

Also includes: - docs/whisper-integration-plan.md: Whisper strategy analysis - README improvements
  with ecosystem documentation

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>

- Add faster-whisper backend for 4x faster transcription
  ([`6a8e30e`](https://github.com/OpenAdaptAI/openadapt-capture/commit/6a8e30ec71ed4513bb643bfb58558911d2fe9584))

Add support for faster-whisper as an alternative transcription backend: - New transcribe-fast
  optional dependency in pyproject.toml - Backend auto-detection (tries faster-whisper first, falls
  back to openai-whisper) - New --backend CLI option: auto, faster-whisper, openai-whisper, api -
  Maintain backward compatibility with existing --api flag

Co-Authored-By: Claude Opus 4.5 <noreply@anthropic.com>


## v0.1.0 (2025-12-12)

### Bug Fixes

- Add contents read permission for publish workflow
  ([`eda29a4`](https://github.com/OpenAdaptAI/openadapt-capture/commit/eda29a49124d6db70369db518ae734ff0b994cec))

### Features

- Complete GUI capture with transcription, visualization, processing, and CI/CD
  ([`365dff8`](https://github.com/OpenAdaptAI/openadapt-capture/commit/365dff8689378afce4013a50997b0fe02e650730))

- Initial repo with design doc
  ([`9e34077`](https://github.com/OpenAdaptAI/openadapt-capture/commit/9e34077504e76e7449d185142caa4de2744059b9))
