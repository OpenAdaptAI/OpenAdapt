---
description: Populates menus with actions for the tray icon.
---

# populate\_menu (function)

## Syntax

```python
def populate_menu(self, menu: QMenu, action: QAction, action_type: str) -> None:
```

## Parameters

`menu`

The `menu` parameter is expected to be a `PySide6.QtWidget.Qmenu` object, for either the `visualize_menu` or `replay_menu`

`action`

The `action` parameter is expected to be a `PySide6.QtGui.QAction`, which acts as an action to trigger when a menu item is clicked.

`action_type`

Can either be `"visualize"` or `"replay"`. The recording actions need this distinction or else they will be triggered multiple times (visualization will trigger replay as well).

