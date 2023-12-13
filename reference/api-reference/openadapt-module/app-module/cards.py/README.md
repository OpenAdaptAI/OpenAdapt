---
description: Provides functions for managing UI cards in the OpenAdapt application.
---

# ☀ cards.py

## Note

There are some functions contained within cards.py for recording, such as:

* stop\_record (function)
* quick\_record (function)

This is because the app contains recording functionality and executes these from cards. A global variable, `record_proc` is used throughout this file, to represent an existing recording process -- if it exists.
