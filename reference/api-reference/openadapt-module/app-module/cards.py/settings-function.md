---
description: Display the settings dialog (nicegui.ui.card).
---

# settings (function)



<figure><img src="../../../../../.gitbook/assets/image (6).png" alt=""><figcaption></figcaption></figure>

## Syntax

<pre class="language-python"><code class="lang-python"><strong>def settings(dark_mode: bool) -> None:
</strong></code></pre>

## Parameters

`dark_mode`

The current dark mode preference (true for dark, false otherwise)

## Remarks

Interacting with the toggles in this window will directly modify environment variables for `SCRUB_ENABLED` and `APP_DARK_MODE` using `openadapt.config.persist_env`
