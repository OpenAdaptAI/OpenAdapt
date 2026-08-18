# macOS permissions

This historical path now points to the maintained
[recording guide](https://docs.openadapt.ai/guides/record-your-app/).

Install the macOS recording and replay capabilities:

```bash
python -m pip install 'openadapt[capture,macos]'
openadapt doctor --backend macos
```

The local host typically needs Screen Recording and Input Monitoring for
capture. It needs Accessibility for actuation. Grant access to the exact
terminal, Desktop application, or signed executable that runs OpenAdapt.

OpenAdapt must return a non-success result when a required permission is
absent. Do not treat process exit alone as proof of a business effect.
