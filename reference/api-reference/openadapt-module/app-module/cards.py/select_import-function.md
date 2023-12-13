---
description: Display the import file selection dialog (nicegui.ui.card).
---

# select\_import (function)

<figure><img src="../../../../../.gitbook/assets/image (5).png" alt=""><figcaption></figcaption></figure>

## Syntax

```python
def select_import(f: callable) -> None:
```

## Parameters

`f`

Function to call when the import button is clicked. This function will be called as&#x20;

```python
f(selected_file.text, delete.value)
```

## Remarks

This function may be used to import new recordings or databases, currently unused.

