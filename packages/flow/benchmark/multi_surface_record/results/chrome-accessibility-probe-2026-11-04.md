# Can the recorder read a Chrome the person opened themselves? (2026-11-04)

A local page with a heading, a labelled field ("Referral note") and a button
("Mark referral sent"), opened in Chrome as a person would (no automation, no
command-line flags). Read from outside through the OS accessibility API the
recorder already uses for each action: a hit test at the click point.

| OS | Chrome | As launched | What turned the page tree on at runtime | Not enough |
| --- | --- | --- | --- | --- |
| Windows 11 Pro (zenbook) | Google Chrome (winget, 2026-11-04) | UIA `ElementFromPoint` returns the page's `ButtonControl "Mark referral sent"`, `EditControl "Referral note"`, `DocumentControl "Probe page"` and the address bar on the **first** request | nothing extra: the point query itself | a full `TreeWalker` walk from the window shows only Chrome's toolbar (234 nodes), with or without `--force-renderer-accessibility` |
| macOS 15.7.7 (oa-macvm guest) | Google Chrome 157.0.8103.39 | toolbar only; the page is one empty `AXScrollArea` | `AXEnhancedUserInterface = true` on Chrome's application element (the call returns -25208, yet 3 s later `AXButton "Mark referral sent"`, `AXTextField "Referral note"`, `AXHeading`, `AXWebArea "Probe page"` are hit-testable) | `AXManualAccessibility` is unsupported (-25205) |
| Linux, X11 (Xvfb + openbox, WSL Ubuntu 24.04) | Chrome for Testing (Playwright chromium-1228) | only Chrome's frame; nothing inside it | none found | the a11y bus `IsEnabled`/`ScreenReaderEnabled`, `ACCESSIBILITY_ENABLED=1`, GNOME `toolkit-accessibility` and `screen-reader-enabled` (confirmed `true` through dconf), a second read, AT-SPI hit tests. Only Chrome started with `--force-renderer-accessibility` exposes `push button "Mark referral sent"` and `entry "Referral note"` and the URL |

Probes: `probe_chromium_uia.py` (Windows), `probe_chrome_ax.py` (macOS),
`probe_chrome_atspi.py` (Linux), in this folder.
