---
description: Implementation of the system tray icon for OpenAdapt.
---

# 🤖 tray.py

## Remarks

The dock icon is hidden on MacOS, as the tray icon is sufficient to show that OpenAdapt is running. This tray sends notifications using [**notify.py**](https://pypi.org/project/notify-py/)**,** to let the user know the state of the application (ex. OpenAdapt is running in the background -- on startup).
