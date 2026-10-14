# Task Navigational Ambiguity Analysis

**Date**: 2026-03-23
**Purpose**: Identify tasks where demo guidance would most improve agent performance.

## Rating Scale

- **High navigational ambiguity**: The task requires multi-level menu navigation, the
  correct UI path is not obvious from the instruction, and a frontier model would likely
  fail without guidance. Demo guidance should help significantly.
- **Medium**: The task involves some non-obvious UI choices. A demo might help but a
  frontier model could potentially figure it out.
- **Low**: The task is straightforward enough that a frontier model can solve it from the
  instruction alone. Demo guidance adds minimal value.

## Custom Tasks (example_tasks/)

### clear-browsing-data-chrome.yaml
- **Rating**: HIGH
- **Instruction**: "Clear browsing data in Google Chrome"
- **UI path**: Chrome -> Settings (3-dot menu or chrome://settings) -> Privacy and security
  -> Clear browsing data -> Select time range -> Click "Clear data"
- **Milestones**: 4 (Chrome open, Settings open, Clear dialog open, Data cleared)
- **Why high**: Multiple valid but different paths (keyboard shortcut Ctrl+Shift+Del,
  menu navigation, address bar). The Settings UI has nested pages. Agent needs to navigate
  Settings -> Privacy and Security -> Clear browsing data, which is 3+ clicks deep.
- **Flywheel value**: EXCELLENT candidate. A demo showing the exact click path through
  menus would prevent exploration. Baseline agents commonly get lost in Chrome Settings.

### chrome-auto-delete-site-data.yaml
- **Rating**: HIGH
- **Instruction**: "Set Chrome to auto-delete on-device site data when closing"
- **UI path**: Chrome -> Settings -> Privacy and security -> Third-party cookies ->
  "Clear on-device site data when you close all windows" toggle
- **Milestones**: 5 (Chrome open, Settings, Privacy section, Site data settings, Toggle on)
- **Why high**: Deeply nested setting that has moved between Chrome versions. The setting
  name does not exactly match what the user sees. Even humans often struggle to find this.
- **Flywheel value**: EXCELLENT. This is a poster case for demo guidance -- the exact click
  path is non-obvious and version-dependent.

### clear-browsing-data-edge.yaml
- **Rating**: HIGH
- **Instruction**: "Clear browsing data in Microsoft Edge"
- **UI path**: Edge -> Settings -> Privacy, search, and services -> Clear browsing data
- **Milestones**: 4 (Edge open, Settings, Clear dialog, Data cleared)
- **Why high**: Similar to Chrome but Edge's Settings layout is different. The Privacy
  section has a different name ("Privacy, search, and services") that agents may not
  recognize as the correct target.
- **Flywheel value**: HIGH. Demo shows the Edge-specific navigation path.

### clear-youtube-history.yaml
- **Rating**: HIGH
- **Instruction**: "Remove all YouTube browsing history from Chrome"
- **UI path**: Chrome -> History (chrome://history) -> Search for YouTube entries ->
  Delete individual entries or use "Clear browsing data" filtered to YouTube
- **Milestones**: 4 (Chrome open, History page, YouTube found, History removed)
- **Why high**: Ambiguous instruction -- unclear whether to use Chrome history page,
  YouTube's own history (youtube.com/feed/history), or Chrome settings. Multiple valid
  approaches with different UI paths.
- **Flywheel value**: HIGH. Demo disambiguates which of several approaches to take.

### calc-formula.yaml
- **Rating**: MEDIUM
- **Instruction**: "Enter values in A1:A3 and a SUM formula in A4 in LibreOffice Calc"
- **UI path**: Click cells A1-A3, type values, click A4, type =SUM(A1:A3)
- **Milestones**: 3 (Calc open, Values entered, Formula entered)
- **Why medium**: Cell navigation in a spreadsheet is mostly straightforward (click cell,
  type). However, the agent needs to know the SUM formula syntax and cell reference format.
- **Flywheel value**: MODERATE. Demo helps with exact cell clicking coordinates but a
  frontier model likely knows spreadsheet formulas.

### create-desktop-folder.yaml
- **Rating**: LOW
- **Instruction**: "Create a folder named TestFolder on the Desktop"
- **UI path**: Right-click desktop -> New -> Folder -> Type name
- **Milestones**: 2 (Desktop visible, Folder exists)
- **Why low**: Very simple task, single right-click context menu. A frontier model
  can reliably figure this out.
- **Flywheel value**: LOW. Demo adds almost no value.

### notepad-hello.yaml
- **Rating**: LOW
- **Instruction**: "Open Notepad and type Hello World"
- **UI path**: Start menu -> search "notepad" -> open -> type text
- **Milestones**: 2 (Notepad open, Text typed)
- **Why low**: Trivially simple. GPT-4o-mini scored 1.0 without any guidance.
- **Flywheel value**: NONE. This was the task that caused the regression because
  the baseline already succeeds perfectly.

## WAA Benchmark Tasks (sampled)

### Chrome: Enable Do Not Track (030eeff7)
- **Rating**: HIGH
- **Instruction**: "Can you enable the 'Do Not Track' feature in Chrome?"
- **UI path**: Chrome -> Settings -> Privacy and security -> Third-party cookies ->
  Send a "Do Not Track" request toggle
- **Why high**: Setting is buried 3 levels deep in Chrome Settings. The UI label
  for the section containing this toggle is not intuitively "Do Not Track."

### Chrome: Reopen closed tab (06fe7178)
- **Rating**: LOW
- **Instruction**: "Can you make my computer bring back the last tab I shut down?"
- **Why low**: Ctrl+Shift+T is well-known. A frontier model knows this shortcut.

### Chrome: Add Dota 2 soundtrack to Steam cart (121ba48f)
- **Rating**: MEDIUM
- **Instruction**: "Find the Dota 2 official soundtrack and add it to my cart on Steam"
- **Why medium**: Requires web navigation (search on Steam, find DLC, add to cart).
  Not a settings navigation task but involves multi-step web interaction.

### Settings: Turn off notifications (37e10fc4)
- **Rating**: HIGH
- **Instruction**: "Turn off notifications in settings"
- **UI path**: Settings -> System -> Notifications -> Toggle off
- **Why high**: Windows 11 Settings has been reorganized. The Notifications toggle is
  under System, which is not where many agents look first.

### Settings: Night light schedule (46adf721)
- **Rating**: HIGH
- **Instruction**: "Enable Night light and set 7:00 PM on / 7:00 AM off"
- **UI path**: Settings -> System -> Display -> Night light -> Schedule -> Set hours
- **Why high**: 4+ clicks deep. Agent must enable Night light AND set specific times.
  Time input fields are finicky to interact with programmatically.

### Settings: Change timezone (9504989a)
- **Rating**: MEDIUM
- **Instruction**: "Change timezone to Pacific (US & Canada)"
- **UI path**: Settings -> Time & language -> Date & time -> Time zone dropdown
- **Why medium**: Relatively standard path but the dropdown selection for the specific
  timezone value requires scrolling and exact matching.

### Edge: Enable Do Not Track (004587f8)
- **Rating**: HIGH
- **Instruction**: "Enable 'Do Not Track' in Edge"
- **UI path**: Edge -> Settings -> Privacy, search, and services -> Send "Do Not Track"
- **Why high**: Edge's settings layout differs from Chrome. The agent needs to navigate
  Edge-specific UI.

### LibreOffice Writer: Line spacing (0810415c)
- **Rating**: HIGH
- **Instruction**: "Make line spacing of first two paragraphs double"
- **UI path**: Select text -> Format -> Paragraph -> Indents & Spacing tab -> Line
  spacing dropdown -> Double
- **Why high**: Multi-level menu (Format -> Paragraph), then a tab within the dialog,
  then a dropdown. Many agents get lost in LibreOffice's menu structure.

### LibreOffice Writer: Tabstops (0a0faba3)
- **Rating**: HIGH
- **Instruction**: "Split sentences using tabstops (3 words left, rest right-aligned)"
- **UI path**: Format -> Tabs -> Set right-aligned tab stop -> Insert tab characters
- **Why high**: Very specialized LibreOffice feature. Even human users often struggle
  with tabstops. A demo is essentially required.

### LibreOffice Calc: Fill blank cells (01b269ae)
- **Rating**: MEDIUM
- **Instruction**: "Fill all blank cells with the value in the cell above"
- **Why medium**: Can be done with Find & Replace or manually. Multiple approaches,
  none obvious from the instruction.

### LibreOffice Calc: Gross profit + new sheet (035f41ba)
- **Rating**: MEDIUM
- **Instruction**: "Fill Gross profit column, then create Year_Profit in new sheet"
- **Why medium**: Requires formula knowledge plus sheet creation. Multi-step but
  individual steps are findable.

### VLC: Disable background cone (215dfd39)
- **Rating**: HIGH
- **Instruction**: "Disable the cone icon in the splash screen"
- **UI path**: Tools -> Preferences -> Show settings: All -> Interface -> Main interfaces
  -> Qt -> Uncheck "Display background cone or art"
- **Why high**: Requires switching to "All" settings mode (not "Simple"), then navigating
  a deep tree. This is one of the hardest navigational tasks in the WAA benchmark.

### Notepad: Create and save draft.txt (366de66e)
- **Rating**: LOW
- **Instruction**: "Open Notepad, create draft.txt, type text, save to Documents"
- **Why low**: Straightforward file operations. The Save As dialog requires selecting
  Documents folder but frontier models handle this well.

### Paint: Draw red circle (15f8de6e)
- **Rating**: MEDIUM
- **Instruction**: "Open Paint and draw a red circle"
- **UI path**: Open Paint -> Select color red -> Select circle/ellipse tool -> Draw
- **Why medium**: Requires knowing Paint's toolbar layout to find the correct tool and
  color. Moderate navigational ambiguity.

### File Explorer: List PNG files (016c9a9d)
- **Rating**: LOW
- **Instruction**: "Search for .png files in Pictures, list names in png_files.txt"
- **Why low**: Can be done via PowerShell command or File Explorer search. The task
  instruction is clear enough for a frontier model.

### VS Code: Find and replace (0ed39f63)
- **Rating**: LOW
- **Instruction**: "Change all 'text' to 'test'"
- **Why low**: Ctrl+H find-and-replace is well-known. Very low ambiguity.

## Summary: Best Flywheel Validation Candidates

### Tier 1: Excellent (HIGH ambiguity, demo guidance should transform 0.0 -> 0.5+)

| Task | App | Why |
|------|-----|-----|
| clear-browsing-data-chrome | Chrome | 3+ clicks deep in Settings, multiple paths |
| chrome-auto-delete-site-data | Chrome | Deeply nested, non-obvious setting name |
| clear-browsing-data-edge | Edge | Edge-specific Settings layout |
| clear-youtube-history | Chrome | Ambiguous approach (which history page?) |
| VLC disable cone (215dfd39) | VLC | Requires "All" settings + deep tree navigation |
| Night light schedule (46adf721) | Settings | 4+ clicks deep + time input fields |
| Writer line spacing (0810415c) | LibreOffice | Format -> Paragraph -> Tab -> Dropdown |
| Writer tabstops (0a0faba3) | LibreOffice | Specialized feature, demo essentially required |
| Edge Do Not Track (004587f8) | Edge | Edge-specific 3-level Settings navigation |
| Chrome Do Not Track (030eeff7) | Chrome | Buried in Privacy section |

### Tier 2: Good (MEDIUM ambiguity, demo might help)

| Task | App | Why |
|------|-----|-----|
| calc-formula | LibreOffice | Cell navigation + formula syntax |
| Timezone change (9504989a) | Settings | Dropdown selection requires scrolling |
| Calc fill blanks (01b269ae) | LibreOffice | Multiple approaches, none obvious |
| Paint red circle (15f8de6e) | Paint | Tool/color selection in unfamiliar UI |
| Steam cart (121ba48f) | Chrome/Web | Multi-step web navigation |

### Tier 3: Skip (LOW ambiguity, demo adds no value)

| Task | App | Why |
|------|-----|-----|
| notepad-hello | Notepad | Baseline already scores 1.0 |
| create-desktop-folder | Desktop | Single right-click menu |
| Reopen tab (06fe7178) | Chrome | Well-known keyboard shortcut |
| Notepad save (366de66e) | Notepad | Standard file operations |
| VS Code replace (0ed39f63) | VS Code | Well-known Ctrl+H |
| File Explorer PNG list (016c9a9d) | File Explorer | Can use PowerShell |

## Recommendation for Flywheel Re-run

Use **clear-browsing-data-chrome** as the primary validation task because:
1. It has 4 milestones giving granular scoring signal
2. The UI path is 3+ clicks deep with non-obvious choices
3. It is already defined as a custom task with setup/evaluate config
4. Chrome's Settings UI produces visually distinct screenshots at each step
   (unlike the notepad task where desktop screenshots were indistinguishable)
5. A frontier model (GPT-5.4-mini) may still fail the deep navigation without guidance

Secondary candidate: **chrome-auto-delete-site-data** (5 milestones, very deep navigation).

For tasks where the baseline agent scores 0.0-0.3, the flywheel thesis
(fail -> demo -> succeed) can be clearly demonstrated.
