---
description: Display the recording prompt dialog (nicegui.ui.card).
---

# recording\_prompt (function)

<figure><img src="../../../../../.gitbook/assets/image (7).png" alt=""><figcaption></figcaption></figure>

## Syntax

```python
def recording_prompt(options: list[str], record_button: ui.button) -> None:
```

## Parameters

`options`

A `list` of strings for autocompletion.

`record_button`

The button used to start recordings, to modify states (record / stop recording).

## Remarks

This function includes a few nested functions for organizing control flow:

* `terminate() -> None`
* `begin() -> None`
* `on_record() -> None`

