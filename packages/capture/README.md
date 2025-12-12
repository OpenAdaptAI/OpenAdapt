# openadapt-capture

GUI interaction capture - platform-agnostic event streams with time-aligned media.

> **Status:** Pre-alpha. See [docs/DESIGN.md](docs/DESIGN.md) for architecture discussion.

## Installation

```bash
pip install openadapt-capture
```

With privacy scrubbing support:

```bash
pip install openadapt-capture[privacy]
```

## Overview

`openadapt-capture` provides:

- **Event schemas** - Platform-agnostic representations of GUI interactions (mouse, keyboard, window events)
- **Media alignment** - Time-synchronized screenshots, video, and audio
- **Serialization** - JSON/binary formats for storage and transmission
- **Privacy integration** - Built-in support for scrubbing via `openadapt-privacy`

## License

MIT
