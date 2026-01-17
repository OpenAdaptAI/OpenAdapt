# Whisper Integration Plan for openadapt-capture

## 1. Current State Analysis

### 1.1 Package Search Results

**PyPI Search:**
- No package named "oa-whisper" or "openadapt-whisper" was found on PyPI
- Related packages found: `openai-whisper`, `faster-whisper`, `mlx-whisper`, `whisperx`, `pywhispercpp`

**OpenAdaptAI GitHub Organization:**
- The organization (https://github.com/OpenAdaptAI) has 35 repositories
- No dedicated whisper-related repository was found
- Main project is OpenAdapt for "Generative Process Automation"

### 1.2 Current Whisper Usage in openadapt-capture

The package already has whisper integration in two places:

**pyproject.toml:**
```toml
[project.optional-dependencies]
transcribe = [
    "openai-whisper>=20230314",
]
```

**openadapt_capture/audio.py:**
- Lazy imports whisper via `_import_whisper()`
- `AudioRecorder.transcribe()` method for batch transcription
- `ContinuousAudioCapture` class with streaming transcription support
- Uses `whisper.load_model()` and `model.transcribe()` APIs

**openadapt_capture/cli.py:**
- `transcribe` command with two modes:
  - `--api`: Uses OpenAI Whisper API (`whisper-1` model)
  - Local: Uses openai-whisper library (tiny, base, small, medium, large models)

### 1.3 Current Configuration

- Supports Python 3.10, 3.11, 3.12 (pyproject.toml: `requires-python = ">=3.10"`)
- OpenAI API already integrated for API-based transcription
- Whisper is an optional dependency via `[transcribe]` extra

---

## 2. Problem Description

### 2.1 Python Version Compatibility History

The user reported concerns about whisper requiring `numba -> llvmlite` which historically only supported Python 3.6-3.9, conflicting with the Python 3.12+ requirement.

**Historical Issue:**
- Older versions of `numba` blocked installation on Python 3.12+ with: "RuntimeError: Cannot install on Python version 3.12.1; only versions >=3.8,<3.12 are supported"
- This was due to `llvmlite` lacking Python 3.12 wheels

**Current Status (2026):**
- **openai-whisper v20250625** (latest) now officially supports Python 3.8-3.13
- **llvmlite 0.46.0+** includes wheels for Python 3.12 (cp312)
- **numba 0.59+** supports Python 3.12
- The compatibility issue appears to be **resolved** in recent versions

### 2.2 Remaining Concerns

Despite resolution, there are still valid reasons to consider alternatives:

1. **Large dependency footprint**: openai-whisper pulls in numba, torch, triton, and other heavy dependencies
2. **Memory usage**: Local whisper models require significant GPU/CPU memory
3. **Installation complexity**: PyTorch version mismatches can cause issues
4. **Cross-platform consistency**: Different behavior on macOS vs Linux vs Windows

---

## 3. Solution Options

### Option A: Use Latest openai-whisper (Current Approach)

**Implementation:**
Keep the current implementation but ensure the latest version is used.

```toml
[project.optional-dependencies]
transcribe = [
    "openai-whisper>=20250625",  # Updated to latest with Python 3.12+ support
]
```

**Pros:**
- Already implemented and working
- Official OpenAI implementation
- Supports word-level timestamps
- No code changes required

**Cons:**
- Large dependency tree (~2GB+ with PyTorch)
- May have compatibility issues on some systems
- Slower than alternatives

---

### Option B: Use faster-whisper (Recommended)

**Implementation:**
Replace openai-whisper with faster-whisper which uses CTranslate2.

```toml
[project.optional-dependencies]
transcribe = [
    "faster-whisper>=1.1.0",
]
```

**Code changes in audio.py:**
```python
# Replace whisper import
from faster_whisper import WhisperModel

# Replace model loading
model = WhisperModel(model_name, device="cpu", compute_type="int8")

# Replace transcribe call
segments, info = model.transcribe(audio, word_timestamps=True)
text = " ".join([segment.text for segment in segments])
```

**Pros:**
- 4x faster than openai-whisper
- Uses less memory (8-bit quantization)
- No FFmpeg system dependency required
- Supports Python 3.12
- Compatible with Distil-Whisper for even faster inference

**Cons:**
- Different API requires code changes
- Still has CUDA/cuDNN version considerations for GPU

---

### Option C: Use mlx-whisper (macOS Only)

**Implementation:**
For macOS with Apple Silicon, use mlx-whisper optimized for Metal.

```toml
[project.optional-dependencies]
transcribe-macos = [
    "mlx-whisper>=0.4.0",
]
```

**Pros:**
- Optimized for Apple Silicon (M1/M2/M3/M4)
- Up to 10x faster than whisper.cpp on Mac
- Uses Apple's Metal GPU acceleration
- Integrates with Hugging Face Hub

**Cons:**
- macOS-only (Apple Silicon)
- Not suitable for cross-platform use

---

### Option D: Use whisper.cpp via Python Bindings

**Implementation:**
Use pywhispercpp for C++ performance with Python API.

```toml
[project.optional-dependencies]
transcribe = [
    "pywhispercpp>=1.0.0",
]
```

**Pros:**
- Very fast C++ implementation
- Low memory footprint
- Cross-platform
- CUDA support available

**Cons:**
- Python bindings may be less mature
- Model files managed separately
- API differs significantly from openai-whisper

---

### Option E: OpenAI Whisper API Only

**Implementation:**
Remove local whisper, use only the cloud API.

```python
# In cli.py - make API the default
def transcribe(capture_dir: str, model: str = "whisper-1") -> None:
    """Transcribe audio using OpenAI Whisper API."""
    # Existing _transcribe_api implementation
```

**Pricing (2026):**
| Model | Price | Example Cost |
|-------|-------|--------------|
| whisper-1 | $0.006/min | $0.36/hour |
| gpt-4o-transcribe | $0.006/min | $0.36/hour |
| gpt-4o-mini-transcribe | $0.003/min | $0.18/hour |

**Pros:**
- No local dependencies
- Consistent cross-platform behavior
- No GPU/memory requirements
- Supports 99+ languages

**Cons:**
- Requires API key and internet
- Ongoing costs
- Privacy concerns (audio sent to cloud)
- No offline support

---

### Option F: Create Separate oa-whisper Package

**Implementation:**
Create a new PyPI package `oa-whisper` that:
1. Bundles optimized whisper implementation
2. Handles Python version detection and chooses best backend
3. Provides unified API across backends

```python
# oa-whisper package structure
oa_whisper/
    __init__.py          # Unified API
    backends/
        __init__.py
        openai_whisper.py
        faster_whisper.py
        mlx_whisper.py
        api.py
    utils.py
```

**Usage:**
```python
from oa_whisper import transcribe

result = transcribe(
    audio_path,
    model="base",
    backend="auto",  # Automatically choose best backend
)
```

**Pros:**
- Unified API across all backends
- Automatic backend selection based on platform
- Single dependency for openadapt-capture
- Version constraints managed centrally

**Cons:**
- Maintenance overhead
- Another package to maintain
- Initial development effort

---

### Option G: Microservice/Subprocess Approach

**Implementation:**
Run whisper in a separate process or container with different Python version.

```python
# Dockerfile.whisper
FROM python:3.11-slim
RUN pip install openai-whisper
COPY whisper_service.py /app/
CMD ["python", "/app/whisper_service.py"]

# In openadapt-capture
import subprocess
result = subprocess.run(
    ["docker", "run", "oa-whisper", "transcribe", audio_path],
    capture_output=True
)
```

**Pros:**
- Complete isolation from main package
- Can use any Python version for whisper
- Works with Docker/containerized deployments

**Cons:**
- Complex deployment
- Docker overhead
- Not suitable for all environments

---

## 4. Recommended Approach

### Primary Recommendation: Option B (faster-whisper) + Option E (API fallback)

**Rationale:**
1. faster-whisper is production-proven (4x faster, less memory)
2. Fully supports Python 3.12
3. API fallback for users without GPU or who prefer cloud
4. Maintains backward compatibility

### Implementation Strategy:

**Phase 1: Update Dependencies (Immediate)**
```toml
[project.optional-dependencies]
# Local transcription with faster-whisper (recommended)
transcribe = [
    "faster-whisper>=1.1.0",
]

# Legacy openai-whisper support (for compatibility)
transcribe-legacy = [
    "openai-whisper>=20250625",
]

# macOS Apple Silicon optimization
transcribe-macos = [
    "mlx-whisper>=0.4.0",
]
```

**Phase 2: Unified Backend Selection (Medium-term)**
```python
# openadapt_capture/transcription.py

def get_transcription_backend():
    """Auto-detect best available backend."""
    import sys
    import platform

    # Check for Apple Silicon
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        try:
            import mlx_whisper
            return "mlx"
        except ImportError:
            pass

    # Try faster-whisper (recommended)
    try:
        from faster_whisper import WhisperModel
        return "faster-whisper"
    except ImportError:
        pass

    # Fall back to openai-whisper
    try:
        import whisper
        return "openai-whisper"
    except ImportError:
        pass

    # No local backend available
    return "api"
```

**Phase 3: Consider oa-whisper Package (Long-term)**

If transcription becomes a core feature used across multiple OpenAdapt packages, create `oa-whisper` as a unified abstraction layer.

---

## 5. Implementation Steps

### Immediate Actions (Week 1)

1. **Verify current compatibility**
   ```bash
   # Test that openai-whisper works with Python 3.12
   python -c "import whisper; print(whisper.__version__)"
   ```

2. **Update pyproject.toml version constraints**
   ```toml
   transcribe = [
       "openai-whisper>=20250625",  # Ensure Python 3.12 support
   ]
   ```

3. **Add faster-whisper as alternative**
   ```toml
   transcribe-fast = [
       "faster-whisper>=1.1.0",
   ]
   ```

### Short-term Actions (Week 2-3)

4. **Implement faster-whisper backend in audio.py**
   - Create `_transcribe_faster_whisper()` function
   - Update `transcribe` CLI command to support `--backend` flag

5. **Update documentation**
   - Document all transcription options
   - Provide installation instructions for each backend

### Medium-term Actions (Month 2)

6. **Implement auto-detection**
   - Create `get_best_backend()` function
   - Add platform-specific optimizations

7. **Add mlx-whisper support for macOS**
   - Conditional import and usage
   - Performance benchmarks

### Long-term Considerations

8. **Evaluate oa-whisper package**
   - If multiple OpenAdapt packages need transcription
   - Create unified abstraction layer

9. **Consider real-time transcription**
   - WebSocket streaming support
   - Integration with live audio capture

---

## 6. Summary Table

| Option | Python 3.12 | Speed | Memory | Cross-Platform | Offline | Complexity |
|--------|-------------|-------|--------|----------------|---------|------------|
| openai-whisper | Yes (v20250625) | Baseline | High | Yes | Yes | Low |
| faster-whisper | Yes | 4x faster | Lower | Yes | Yes | Low |
| mlx-whisper | Yes | 10x faster | Low | macOS only | Yes | Low |
| pywhispercpp | Yes | 3-5x faster | Very Low | Yes | Yes | Medium |
| OpenAI API | N/A | Variable | N/A | Yes | No | Low |
| oa-whisper pkg | Yes | Variable | Variable | Yes | Yes | High |
| Microservice | Yes | Variable | Isolated | Yes | Yes | High |

---

## 7. References

### PyPI Packages
- [openai-whisper](https://pypi.org/project/openai-whisper/)
- [faster-whisper](https://pypi.org/project/faster-whisper/)
- [mlx-whisper](https://pypi.org/project/mlx-whisper/)
- [pywhispercpp](https://pypi.org/project/pywhispercpp/)

### GitHub Repositories
- [OpenAI Whisper](https://github.com/openai/whisper)
- [SYSTRAN/faster-whisper](https://github.com/SYSTRAN/faster-whisper)
- [ggml-org/whisper.cpp](https://github.com/ggml-org/whisper.cpp)
- [ml-explore/mlx](https://github.com/ml-explore/mlx)
- [OpenAdaptAI](https://github.com/OpenAdaptAI)

### Documentation
- [OpenAI Whisper API Pricing](https://openai.com/api/pricing/)
- [Numba Installation](https://numba.readthedocs.io/en/stable/user/installing.html)

---

*Document created: 2026-01-16*
*Author: Research analysis for openadapt-capture whisper integration*
