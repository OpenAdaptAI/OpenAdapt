---
description: >-
  Import data from a selected file. The file is assumed to be a bz2 compressed
  file.
---

# on\_import (function)

## Syntax

```python
def on_import(
    selected_file: str,
    delete: bool = False,
    src: str = "openadapt.db",
) -> None:
```

## Parameters

`selected_file`

The path of the selected file.

`delete`

Whether to delete the file after import (false by default).

`src`

The file name of the imported data, `openadapt.db` by default.

## Remarks

This function is to be replaced with magic-wormhole.
