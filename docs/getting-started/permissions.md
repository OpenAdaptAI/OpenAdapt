# Native desktop permissions

The maintained recording guide is at
[docs.openadapt.ai](https://docs.openadapt.ai/guides/record-your-app/).

Use `openadapt doctor --backend <surface>` to inspect the installed capability.
The check does not grant access or certify the target application.

Native recording and replay use operating system controls:

| Surface | Typical local requirements |
| --- | --- |
| macOS before 27.3 | Screen Recording and Input Monitoring for capture; Accessibility for app control |
| macOS 27.3 or later | Screen Recording and Input Monitoring for capture; System & App Protection Override for app control, with the selected target included |
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

## macOS 27.3 setup

Apple introduced the new app-control settings in **macOS 27.3 beta 2** on
November 16, 2026. Existing grants remain in effect. Apple notes that some known apps keep
their existing behavior without a new entitlement. See [Apple's announcement](https://developer.apple.com/news/?id=oqng0zjk).

1. Open **System Settings > Privacy & Security > System & App Protection Override**.
2. Click **+** and add the app that launches OpenAdapt, such as the installed
   desktop app or your terminal host.
3. Select **Choose Apps**, add the intended target apps, and choose **Ask First**
   or **At Any Time** for each target. An Ask First grant can require another
   confirmation during use.
4. For unrestricted control, use the system-protection override option, review
   its five action confirmations and authenticate when asked. This is broader
   than selecting target apps; it is not required by this guide.
5. Restart the launching app and re-check the intended target. For earlier macOS,
   use **Privacy & Security > Accessibility** instead.

[Apple's permission guide](https://support.apple.com/en-us/149699) describes the
scope controls. Screen Recording and Input Monitoring remain separate checks;
this change does not make Full Disk Access a recording requirement. The new-pane
deep-link identifier is not verified, so Desktop opens Privacy & Security and
provides the manual path.

## What the checks establish

`AXIsProcessTrusted` reports trust for the current process. It does not document
which target apps are allowed. On 27.3 or later, doctor and Desktop describe that
result as a process preflight, rather than claiming every target is accessible.
An unavailable API is unknown and must not be reported as granted. Runtime input
continues to refuse when the existing process/target checks cannot establish the
required boundary; a version number never grants access.

No 27.3 machine was available for this change. The relationship between an
override grant and `AXIsProcessTrusted`, read versus posted-input permissions,
and replay under **Choose Apps** still need beta-machine verification. No
replacement trust API or blanket bypass is assumed. No general-availability date
is established here.

Apple's [27.3 beta release notes](https://developer.apple.com/documentation/macos-release-notes/macos-27_3-release-notes)
list interaction issues with Safari and other WebKit apps under the new grant
(192510863). They also list a missing activity indicator for some CoreGraphics
input (192728601). The indicator is not proof that input succeeded or failed;
verify the application's actual effect.
