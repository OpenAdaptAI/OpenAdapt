# Real-RDP readiness diagnosis

This is a derived, non-counted diagnostic record for the rejected candidate
`a1b152640e36ccd8722e2df33252403078a87653`. Raw screenshots and guest command
output remain local under `/private/tmp`; they are not product evidence and
were not committed because they expose the qualification VM's desktop.

The first input-free diagnostic reproduced the 90-second pre-input halt. A real
Aardwolf session painted Windows' "Another user is signed in" interstitial,
while `query user` showed only the preserved base's console user active and
Explorer existed only in that console session. The temporary RDP connection
never advanced beyond a connected, username-less session. Diagnostic JSON
SHA-256: `a2491d72adfbdf1d64dfe0420841893fcb8dffbf8a7207e19b04159a1013d4f5`.

A fresh owned-snapshot diagnostic then logged off the pre-existing console
session before connection, without sending any RDP input. The temporary account
became the one exact active RDP session and Explorer's independently queried
`SessionId` matched it. A 60-second frame sequence proved the remaining race:

| Elapsed | Observed state | Frame SHA-256 |
| ---: | --- | --- |
| 0 s | Windows Welcome/login transition | `d2bb78c8a6a82ade6a5b477cd5d270663080774ac19aea983264ffb3cd570a39` |
| 10 s | Painted Windows desktop | `c718b644e986d8609a8dbcda327f0dae3cea0e3ac3dca70d115bbe98d40c1fdb` |
| 30 s | Painted desktop; partial update observed | `819a9b5c62f48b20bfbc8e75d5d6ddf972d0192be946198787166769feb59bbf` |
| 60 s | Painted Windows desktop | `00e4ae703f796d542e19e964262c1a30c5f28fc6d798a7e29a9b0e429c7f9c19` |

The final diagnostic JSON SHA-256 is
`8e53c7c088eaa912991b03248397bbccdf91e9f5be00d2b3d14644e8243ae766`.
It records zero RDP input and successful cleanup: exact suspended base current,
owned snapshot deleted, and all seven pre-existing snapshot IDs restored.

The successor candidate therefore refuses input until it proves all of:

1. Pre-existing console/RDP users were logged off inside the harness-owned
   snapshot.
2. The exact qualification account has one active RDP session.
3. Explorer exists in that exact session ID.
4. The framebuffer materially transitions from the login frame and multiple
   stable frames match this fixed VM's Windows desktop/taskbar predicate.
5. Aardwolf framebuffer copies occur on its decoder event-loop thread and any
   embedded `(value, error)` input receipt raises instead of looking successful.

This diagnosis does not count as a qualification trial and makes no Citrix
ICA/HDX claim.

## Successor threshold measurement

A later input-free owned-snapshot probe used the detached exact-ID logoff plus
exact-ID reset fallback. It independently proved the original console session
gone, reached the temporary account's exact RDP session, and observed Welcome
through 20 seconds followed by the real desktop/taskbar from 25 through 75
seconds. The real Welcome-to-desktop changed `0.124912` of pixels; the desktop
predicate was true, and frames became byte-stable from 35 seconds onward. The
earlier `0.50` transition floor was therefore unsupported by the fixed VM.

The successor uses an evidence-bound `0.10` floor only in conjunction with the
fixed-VM taskbar predicate, one exact active target-account RDP session,
Explorer in that same session ID, and three stable frames. This threshold is a
qualification-environment readiness guard, not a generic Windows, RDP, or
application-identity claim. A unit test proves taskbar-like pixels below the
transition floor still refuse.

## Retained clean base and pre-freeze proof

The original base snapshot retained an interactive console session, so it was
preserved unchanged and a separate qualification base was prepared from it.
Preparation receipt `rdp-base-prep-6aab3e2d2b6c39cb` proved three consecutive,
parseable checks with no `query user` rows and an explicit successful
`OAFLOW_EXPLORER_IDS=` sentinel. It sent no RDP input, suspended the VM, and
created retained snapshot `{8f43c385-0566-44d3-9e50-f646777d315b}`. The
preparation JSON SHA-256 is
`aa0f1badc1ab9abdfdca1932c0e137ac1f5bf96b61a182b178c16c770323f033`.
The original base and both preserved footage branches remained present.

One input-free proof then ran from that retained base. It found no pre-existing
interactive session, proved the temporary account owned exact active RDP
session `3` with Explorer in the same session, observed the desktop predicate
at 35 seconds, and required three stable frames through 45 seconds. The
Welcome-to-desktop change was again `0.124912`. Cleanup deleted only the owned
diagnostic snapshot and restored the exact eight-snapshot inventory with the
qualification base suspended and current. Diagnostic JSON SHA-256:
`4b834b41fc913b954fd12e3926a708fcee248180ee6619e1b73646d2e23aed88`.

This was a non-counted, pre-freeze proof from the dirty successor working tree
based on `b97af63dfb69059914c3e1cd7104997bb082e8a8`; the exact pre-documentation
working-tree patch SHA-256 was
`9631d9cb4db2d4a794aad66437f9ebb10cf070ca83b59fa957901826c16e383a`.
It is only the gate for freezing a new candidate and is not accepted
qualification evidence by itself.

The first frozen successor, `309fa24152ee8ef22ecf3020614fdff46be53ebe`,
was rejected before counted input because the counted path still inherited a
30-second desktop-readiness timeout. That could not cover the proof's first
recognized desktop at 35 seconds or its third stable frame at 45 seconds. The
replacement candidate uses and reports an explicit 75-second counted timeout,
matching the complete diagnostic observation window. A focused unit test binds
the counted wrapper to that exact value.
