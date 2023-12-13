---
description: This module provides functionality for building the OpenAdapt application.
---

# 🔨 build.py

## Usage

`python -m openadapt.app.build`

### Remarks

This script creates a `spec` file for PyInstaller, and then modifies this file at runtime to add the following line:\


```python
import sys ; sys.setrecursionlimit(sys.getrecursionlimit() * 5)
```

This is necessary because of the large number of imports used by OpenAdapt.
