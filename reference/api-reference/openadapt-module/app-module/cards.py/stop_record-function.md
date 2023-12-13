---
description: Stop the current recording session.
---

# stop\_record (function)

## Syntax

```python
def stop_record() -> None
```

## Remarks

This function sends `SIGINT` to `record_proc`.
