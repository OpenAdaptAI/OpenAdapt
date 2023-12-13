---
description: Updates the tray icon.
---

# update\_tray\_icon (function)

## Syntax

```python
def update_tray_icon(self) -> None:
```

## Remarks

This function is called automatically through a **QTimer**, every **1000ms**. This timer is created upon initialization of the [SystemTrayIcon](./).
