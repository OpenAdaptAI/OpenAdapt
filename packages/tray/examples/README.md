# OpenAdapt Tray Examples

This directory contains example scripts demonstrating various features of openadapt-tray.

## Notification Examples

### notification_examples.py

Comprehensive examples showing all notification features:

```bash
python examples/notification_examples.py
```

Examples include:
1. Basic notification
2. Recording started notification
3. Critical error notification
4. Notification with click callback
5. Notification with custom icon
6. Notification with action buttons
7. Notification with reply field (macOS)
8. Sequence of notifications
9. Conditional notifications based on config
10. State-based notifications

### Quick Test Scripts

Located in the root directory for easy access:

**test_notification_simple.py** - Quick test of basic notification features:
```bash
python test_notification_simple.py
```

Shows:
- Backend detection
- Basic notification
- Critical notification
- Callback notification

**test_notification.py** - Interactive tests with user interaction:
```bash
python test_notification.py
```

Includes wait times for user to click notifications and test callbacks.

## Running Examples

All examples can be run directly:

```bash
# From the openadapt-tray root directory
cd /path/to/openadapt-tray

# Run the comprehensive examples
python examples/notification_examples.py

# Run quick tests
python test_notification_simple.py
```

## Adding New Examples

To add a new example:

1. Create a new Python file in this directory
2. Import from `openadapt_tray` modules
3. Add clear comments and docstrings
4. Update this README with a description

Example template:

```python
#!/usr/bin/env python3
"""Description of what this example demonstrates."""

import sys
from pathlib import Path

# Add parent src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from openadapt_tray.notifications import NotificationManager

def main():
    """Run the example."""
    # Your example code here
    pass

if __name__ == "__main__":
    main()
```

## Documentation

For detailed notification system documentation, see:
- [NOTIFICATIONS.md](../NOTIFICATIONS.md) - Complete notification system guide
- [README.md](../README.md) - Main project documentation
