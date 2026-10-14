# Platform-Specific Code Refactoring Analysis

## Executive Summary

This document analyzes the platform-specific accessibility and UI tree reading code in OpenAdapt and related projects, and provides recommendations for creating a dedicated `openadapt-platform` package.

**Recommendation**: Yes, create `openadapt-platform` as a standalone package to consolidate platform-specific functionality for accessibility tree reading and input simulation across macOS, Windows, and Linux.

## Background

### Code Provenance

The platform-specific code has traveled through several projects:

1. **Original Source**: OpenAdapt repository (`openadapt/window/`)
2. **Adopted by**: OpenCUA/AgentNetTool (`agentnet-annotator/api/core/a11y/`)
3. **Current locations**:
   - OpenAdapt: `openadapt/window/_macos.py`, `_windows.py`, `_linux.py`
   - AgentNetTool: `api/core/a11y/_darwin.py`, `_windows.py`

### Current Implementation Analysis

#### OpenAdapt `window` Module Structure

```
openadapt/window/
    __init__.py      # Platform detection and unified interface
    _macos.py        # macOS implementation (AppKit, Quartz, ApplicationServices)
    _windows.py      # Windows implementation (pywinauto)
    _linux.py        # Linux implementation (xcffib)
```

The `__init__.py` provides a clean abstraction:

```python
if sys.platform == "darwin":
    from . import _macos as impl
elif sys.platform == "win32":
    from . import _windows as impl
elif sys.platform.startswith("linux"):
    from . import _linux as impl
```

#### Key Functions (Unified Interface)

| Function | Purpose |
|----------|---------|
| `get_active_window_state(read_window_data)` | Get window metadata and optional accessibility tree |
| `get_active_element_state(x, y)` | Get element properties at coordinates |
| `get_active_window_data(include_window_data)` | High-level wrapper for window data |

#### Platform-Specific Dependencies

| Platform | Dependencies | Notes |
|----------|--------------|-------|
| macOS | AppKit, ApplicationServices, Quartz, Foundation, oa_atomacos | PyObjC bindings, custom atomacos fork |
| Windows | pywinauto, pyautogui | UIA backend |
| Linux | xcffib | X11 only, limited a11y support |

### AgentNetTool Enhancements

The AgentNetTool fork added several improvements to the macOS implementation:

1. **Concurrent tree traversal**: Uses `ThreadPoolExecutor` for faster accessibility tree building
2. **Bounding box filtering**: Filters elements outside visible window bounds
3. **Element description**: `DarwinElementDescriber` class for structured element analysis
4. **Window switch detection**: Tracks if window changed during tree capture
5. **Dock support**: Can retrieve Dock element state

Key additions in `_darwin.py`:

```python
def _create_axui_node(node, nodes, depth, bbox, switched):
    """Concurrent node creation with bounding box filtering"""
    with concurrent.futures.ThreadPoolExecutor() as executor:
        # Parallel child traversal
        ...

def get_accessibility_tree():
    """Returns tree with completion status"""
    tree_status = {
        "complete": True,
        "switched": False,
        "closed": False,
    }
    ...
```

## Proposed `openadapt-platform` Package

### Package Scope

The package would consolidate:

1. **Accessibility Tree Reading** (read operations)
   - Window state retrieval
   - Element tree traversal
   - Element-at-coordinate lookup

2. **Input Simulation** (write operations)
   - Mouse events (click, move, drag, scroll)
   - Keyboard events (key press, type text)
   - Window manipulation (focus, resize, move)

### Proposed Structure

```
openadapt-platform/
    src/
        openadapt_platform/
            __init__.py              # Public API exports

            # Accessibility (reading)
            accessibility/
                __init__.py          # Unified interface
                base.py              # Abstract base classes
                darwin.py            # macOS implementation
                windows.py           # Windows implementation
                linux.py             # Linux implementation (X11/Wayland)
                types.py             # Common data structures

            # Input simulation (writing)
            input/
                __init__.py          # Unified interface
                base.py              # Abstract base classes
                darwin.py            # macOS input (CGEvent, etc.)
                windows.py           # Windows input (SendInput, etc.)
                linux.py             # Linux input (Xlib/uinput)
                types.py             # Common data structures

            # Shared utilities
            utils/
                __init__.py
                converters.py        # ObjC/Win32 type conversion
                geometry.py          # Bounding box, rectangle operations

    tests/
        test_accessibility/
        test_input/

    pyproject.toml
```

### API Design

#### Accessibility API

```python
from openadapt_platform.accessibility import (
    get_active_window,
    get_window_tree,
    get_element_at_point,
    WindowState,
    ElementTree,
)

# Get active window metadata
window: WindowState = get_active_window()
# WindowState(title="...", bounds=Rect(...), window_id=..., pid=...)

# Get full accessibility tree
tree: ElementTree = get_window_tree(window, max_depth=10, timeout=5.0)

# Get element at coordinates
element = get_element_at_point(x=100, y=200)
```

#### Input API

```python
from openadapt_platform.input import (
    click,
    double_click,
    move_to,
    drag,
    scroll,
    type_text,
    press_key,
    key_down,
    key_up,
)

# Mouse operations
click(x=100, y=200, button="left")
double_click(x=100, y=200)
move_to(x=300, y=400)
drag(from_x=100, from_y=200, to_x=300, to_y=400)
scroll(x=100, y=200, delta_y=-3)  # scroll down

# Keyboard operations
type_text("Hello, World!")
press_key("enter")
press_key("cmd+c")  # platform-aware modifier handling
```

### Consumer Packages

| Package | Usage |
|---------|-------|
| `openadapt-capture` | Uses accessibility reading for recording UI state during demonstrations |
| `openadapt` (core) | Uses input simulation for replaying actions; may use reading for verification |
| `openadapt-grounding` | May use accessibility data to enhance visual grounding |

### Dependency Graph

```
                    openadapt (core)
                         |
            +------------+------------+
            |                         |
    openadapt-capture          openadapt-grounding
            |                         |
            +------------+------------+
                         |
                 openadapt-platform
                    (read + write)
```

## Implementation Recommendations

### Phase 1: Extract and Unify

1. Create new `openadapt-platform` repository
2. Port existing `openadapt/window/` code as the accessibility module
3. Incorporate improvements from AgentNetTool:
   - Concurrent tree traversal
   - Bounding box filtering
   - Window switch detection
4. Add comprehensive type hints and dataclasses

### Phase 2: Add Input Simulation

1. Extract input code from current `openadapt` repo (if any)
2. Implement cross-platform input simulation:
   - macOS: `CGEvent`, `CGEventPost`
   - Windows: `SendInput`, `pyautogui`
   - Linux: `python-xlib`, `pynput`

### Phase 3: Integration

1. Update `openadapt-capture` to depend on `openadapt-platform`
2. Update `openadapt` core to use `openadapt-platform` for input
3. Remove duplicated platform code from other packages

### Key Decisions to Make

1. **Linux support priority**: Current Linux implementation is minimal. Decide between:
   - X11-only support (simpler, covers most cases)
   - Wayland support (future-proof, more complex)
   - Use AT-SPI for accessibility on Linux

2. **Atomacos fork**: Continue using `oa_atomacos` or inline the necessary functionality?

3. **Async support**: Should the API support async/await for tree traversal?

4. **Caching**: Implement caching for repeated element lookups?

## Benefits of Refactoring

1. **Single source of truth**: One package for all platform-specific code
2. **Easier maintenance**: Platform bugs fixed in one place
3. **Better testing**: Dedicated test suite for platform abstractions
4. **Clear ownership**: Explicit package for platform expertise
5. **Reusability**: Other projects (like AgentNetTool) could depend on this
6. **Reduced duplication**: No need to sync changes across multiple repos

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Breaking changes during extraction | Maintain API compatibility; extensive testing |
| Platform-specific bugs | CI matrix with macOS, Windows, Linux runners |
| Performance regression | Benchmark before/after; keep concurrent traversal |
| Increased dependency complexity | Keep dependencies minimal; optional extras for each platform |

## Conclusion

Creating `openadapt-platform` is recommended. The platform-specific code is:

1. **Already duplicated** across OpenAdapt and AgentNetTool
2. **Complex enough** to warrant dedicated maintenance
3. **Foundational** to multiple packages in the ecosystem
4. **Improvable** with enhancements from AgentNetTool

The refactoring aligns with OpenAdapt's modular package architecture and would provide a clean separation between platform abstractions and higher-level automation logic.

## References

- OpenAdapt window module: https://github.com/OpenAdaptAI/openadapt/tree/main/openadapt/window
- AgentNetTool a11y module: https://github.com/xlang-ai/AgentNetTool/tree/main/agentnet-annotator/api/core/a11y
- Apple Accessibility API: https://developer.apple.com/documentation/applicationservices/axuielement
- Windows UI Automation: https://docs.microsoft.com/en-us/windows/win32/winauto/entry-uiauto-win32
- Linux AT-SPI: https://gitlab.gnome.org/GNOME/at-spi2-core
