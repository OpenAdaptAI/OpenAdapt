# Native desktop permissions

The maintained recording guide is at
[docs.openadapt.ai](https://docs.openadapt.ai/guides/record-your-app/).

Use `openadapt doctor --backend <surface>` to inspect the installed capability.
The check does not grant access or certify the target application.

Native recording and replay use operating system controls:

| Surface | Typical local requirements |
| --- | --- |
| macOS | Screen Recording and Input Monitoring for capture; Accessibility for actuation |
| Windows | An interactive desktop; UI Automation and input rights at the target integrity level |
| Linux | An interactive X11 or approved portal session plus AT-SPI for structural evidence |
| RDP / Citrix | Access to the exact visible local client window and its input path |

OpenAdapt must refuse an action when a required permission or target boundary
is absent. A permission failure must not become a silent success.

Install the applicable capability before the check:

```bash
python -m pip install 'openadapt[capture,macos]'
openadapt doctor --backend macos
```

Run `openadapt flow record --help` and `openadapt flow replay --help` for the
exact installed target options.
