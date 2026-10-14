# Example Synthetic Demos - Showcase

This document shows 5 diverse examples from the synthetic demo library, demonstrating different domains and complexity levels.

---

## Example 1: Simple Task - Open Notepad (7 steps)

**Domain:** notepad
**Complexity:** Easy
**Estimated Steps:** 7

```
TASK: Open Notepad
DOMAIN: notepad

STEPS:
1. Click on the Windows Start button to access the application menu
   REASONING: The Start menu provides access to all installed applications and is the primary entry point for launching Notepad
   ACTION: CLICK(x=0.02, y=0.98)

2. Wait for the Start menu to fully open
   REASONING: The system needs time to render the Start menu interface before we can interact with it
   ACTION: WAIT(1.0)

3. Type "notepad" in the search box to find the Notepad application
   REASONING: Using search is the quickest and most reliable way to locate Notepad among all installed applications
   ACTION: TYPE("notepad")

4. Wait for search results to populate
   REASONING: The system needs time to process the search query and display matching applications
   ACTION: WAIT(1.0)

5. Click on the Notepad application in the search results
   REASONING: This launches the Notepad application which is displayed as the top search result
   ACTION: CLICK(x=0.15, y=0.35)

6. Wait for Notepad window to open
   REASONING: The application needs time to initialize and display its main window interface
   ACTION: WAIT(1.5)

7. Verify Notepad is open and ready for use
   REASONING: Confirming the application has successfully launched ensures the task is complete
   ACTION: DONE()

EXPECTED_OUTCOME: Notepad application window should be open and visible on the screen, displaying a blank text editor with the title "Untitled - Notepad" in the window header, ready for text input.
```

**Key Patterns:**
- Start menu → Search → Launch workflow
- Proper wait times for UI transitions
- Normalized coordinates (0.0-1.0 range)
- Clear reasoning for each step

---

## Example 2: Medium Task - Draw Rectangle in Paint (11 steps)

**Domain:** paint
**Complexity:** Medium
**Estimated Steps:** 11

```
TASK: Draw a rectangle
DOMAIN: paint

STEPS:
1. Open the Start menu to access applications
   REASONING: Need to launch Windows Paint application to begin the drawing task
   ACTION: CLICK(x=0.02, y=0.98)

2. Wait for Start menu to appear
   REASONING: Allow time for the Start menu animation and rendering to complete
   ACTION: WAIT(1.0)

3. Type "paint" to search for the Paint application
   REASONING: Quickly locate Paint application through Windows search functionality
   ACTION: TYPE("paint")

4. Wait for search results to populate
   REASONING: Give the system time to find and display Paint in the search results
   ACTION: WAIT(1.0)

5. Click on Paint application in search results
   REASONING: Launch the Paint application to access drawing tools
   ACTION: CLICK(x=0.10, y=0.25)

6. Wait for Paint application to fully load
   REASONING: Ensure Paint interface is completely loaded and ready for interaction
   ACTION: WAIT(2.0)

7. Click on the Shapes tool in the ribbon menu
   REASONING: Access the shapes gallery to select the rectangle drawing tool
   ACTION: CLICK(x=0.15, y=0.08)

8. Select the rectangle shape from the shapes dropdown
   REASONING: Choose the rectangle tool specifically from available shape options
   ACTION: CLICK(x=0.13, y=0.12)

9. Position cursor at starting point for rectangle
   REASONING: Prepare to draw the rectangle by clicking at the desired top-left corner position on the canvas
   ACTION: CLICK(x=0.30, y=0.35)

10. Draw the rectangle by dragging to create the shape
    REASONING: Create the rectangle by dragging from the starting point to define its width and height
    ACTION: DRAG(start_x=0.30, start_y=0.35, end_x=0.65, end_y=0.65)

11. Task completion
    REASONING: Rectangle has been successfully drawn on the canvas
    ACTION: DONE()

EXPECTED_OUTCOME: A rectangle should be visible on the Paint canvas, drawn from coordinates (0.30, 0.35) to (0.65, 0.65), displaying a clear rectangular shape with the default outline style and fill settings.
```

**Key Patterns:**
- Application launch + tool selection workflow
- Use of DRAG action for drawing
- Multiple UI interactions within the app
- Realistic coordinates for Paint ribbon and canvas

---

## Example 3: Complex Task - Set Alarm for 8:00 AM (18 steps)

**Domain:** clock
**Complexity:** Complex
**Estimated Steps:** 18

```
TASK: Set alarm for 8:00 AM
DOMAIN: clock

STEPS:
1. Click on the Start menu to access applications
   REASONING: Need to open the Start menu to search for and launch the Clock application
   ACTION: CLICK(x=0.02, y=0.98)

2. Wait for Start menu to open
   REASONING: Allow time for the Start menu animation and interface to fully load
   ACTION: WAIT(1.0)

3. Type "Clock" to search for the Clock application
   REASONING: Searching for Clock is the fastest way to locate and launch the Windows Clock application
   ACTION: TYPE("Clock")

4. Wait for search results to appear
   REASONING: Allow time for Windows Search to process the query and display results
   ACTION: WAIT(1.0)

5. Click on the Clock application in search results
   REASONING: Launch the Clock application to access alarm functionality
   ACTION: CLICK(x=0.5, y=0.3)

6. Wait for Clock application to open
   REASONING: Allow the Clock application to fully load its interface
   ACTION: WAIT(2.0)

7. Click on the Alarm tab
   REASONING: Need to navigate to the Alarm section to create a new alarm
   ACTION: CLICK(x=0.15, y=0.1)

8. Wait for Alarm tab to load
   REASONING: Allow the alarm interface to display properly
   ACTION: WAIT(0.5)

9. Click on the "Add new alarm" button
   REASONING: Initiate the process of creating a new alarm
   ACTION: CLICK(x=0.9, y=0.9)

10. Wait for alarm creation dialog to appear
    REASONING: Allow the alarm setup interface to load
    ACTION: WAIT(1.0)

11. Click on the hour field and set to 8
    REASONING: Need to set the alarm hour to 8 for 8:00 AM
    ACTION: CLICK(x=0.45, y=0.4)

12. Type "8" for the hour
    REASONING: Enter the desired hour value
    ACTION: TYPE("8")

13. Click on the minute field and ensure it shows 00
    REASONING: Verify or set minutes to 00 for exactly 8:00
    ACTION: CLICK(x=0.55, y=0.4)

14. Type "00" for minutes
    REASONING: Set minutes to zero for 8:00 exactly
    ACTION: TYPE("00")

15. Click on AM/PM selector to ensure AM is selected
    REASONING: Verify the alarm is set for 8:00 AM, not PM
    ACTION: CLICK(x=0.65, y=0.4)

16. Click the Save button to confirm the alarm
    REASONING: Save and activate the newly created 8:00 AM alarm
    ACTION: CLICK(x=0.7, y=0.75)

17. Wait for alarm to be saved
    REASONING: Allow the application to process and display the saved alarm
    ACTION: WAIT(1.0)

18. Task completed
    REASONING: The 8:00 AM alarm has been successfully created and saved in the Clock application
    ACTION: DONE()

EXPECTED_OUTCOME: A new alarm is created and active in the Windows Clock application, set to ring at 8:00 AM. The alarm should be visible in the alarm list with the time displayed as 8:00 AM, and the toggle switch should be in the "on" position indicating the alarm is enabled.
```

**Key Patterns:**
- Multi-level navigation (app → tab → dialog)
- Form filling with multiple fields
- Time input handling
- Longer wait times for dialog transitions
- Detailed validation steps

---

## Example 4: Minimal Task - Type Sample Text in Notepad (4 steps)

**Domain:** notepad
**Complexity:** Very Easy
**Estimated Steps:** 4

```
TASK: Type 'Sample text 2' in Notepad
DOMAIN: notepad

STEPS:
1. Ensure Notepad is already open
   REASONING: This task requires Notepad to be running
   ACTION: WAIT(0.5)

2. Click in the text editing area
   REASONING: Set focus to the text editor
   ACTION: CLICK(x=0.5, y=0.5)

3. Type the sample text
   REASONING: Input the required text
   ACTION: TYPE("Sample text 2")

4. Task complete
   REASONING: Text has been successfully entered
   ACTION: DONE()

EXPECTED_OUTCOME: The text "Sample text 2" appears in the Notepad window.
```

**Key Patterns:**
- Precondition: assumes app is already open
- Minimal steps for focused task
- Direct interaction without complex navigation

---

## Example 5: Comparison - Calculator Basic Addition

**Domain:** calculator
**Complexity:** Medium
**Estimated Steps:** 12

Here's an example from the base demo library (not synthetic_demos) showing a different style:

```
TASK: Perform basic addition in Calculator (25 + 17)
DOMAIN: calculator

STEPS:
1. Click on the Start menu button in the taskbar
   REASONING: Need to access the Start menu to find Calculator
   ACTION: CLICK(x=0.02, y=0.98)

2. Type "calculator" in the search box
   REASONING: Searching is faster than navigating through menus
   ACTION: TYPE("calculator")

3. Wait for search results to appear
   REASONING: Windows needs time to index and display results
   ACTION: WAIT(1.0)

4. Click on the Calculator app in search results
   REASONING: Calculator should appear as the first result
   ACTION: CLICK(x=0.15, y=0.3)

5. Wait for Calculator to open
   REASONING: Application needs time to launch
   ACTION: WAIT(0.5)

6. Click the "2" button
   REASONING: First digit of first number
   ACTION: CLICK(x=0.3, y=0.6)

7. Click the "5" button
   REASONING: Second digit of first number
   ACTION: CLICK(x=0.5, y=0.5)

8. Click the "+" button
   REASONING: Addition operator
   ACTION: CLICK(x=0.7, y=0.6)

9. Click the "1" button
   REASONING: First digit of second number
   ACTION: CLICK(x=0.3, y=0.7)

10. Click the "7" button
    REASONING: Second digit of second number
    ACTION: CLICK(x=0.5, y=0.6)

11. Click the "=" button
    REASONING: Calculate the result
    ACTION: CLICK(x=0.7, y=0.85)

12. Verify result shows 42
    REASONING: 25 + 17 = 42
    ACTION: DONE()

EXPECTED_OUTCOME: Calculator displays the result 42
```

**Key Patterns:**
- Sequential button clicks for number entry
- Calculator-specific coordinate layout
- Multiple small actions for single calculation
- Verification step at the end

---

## Common Action Patterns Across Demos

### 1. Application Launch Pattern
```
CLICK(x=0.02, y=0.98)  # Start menu
WAIT(1.0)
TYPE("app_name")
WAIT(1.0)
CLICK(x=0.15, y=0.35)  # First search result
WAIT(1.5)
```

### 2. Save File Pattern
```
HOTKEY("ctrl", "s")
WAIT(0.5)
TYPE("filename.txt")
CLICK(x=0.7, y=0.9)  # Save button
WAIT(0.5)
```

### 3. Menu Navigation Pattern
```
CLICK(x=0.1, y=0.05)  # Menu bar
WAIT(0.3)
CLICK(x=0.15, y=0.15)  # Menu item
WAIT(0.3)
```

### 4. Form Filling Pattern
```
CLICK(x=0.5, y=0.3)  # Field 1
TYPE("value1")
CLICK(x=0.5, y=0.4)  # Field 2
TYPE("value2")
CLICK(x=0.7, y=0.8)  # Submit
```

### 5. Drawing Pattern
```
CLICK(x=0.1, y=0.1)  # Select tool
DRAG(start_x=0.3, start_y=0.3, end_x=0.6, end_y=0.6)
DONE()
```

---

## Coordinate Convention

All demos use normalized coordinates (0.0 to 1.0):

### Common UI Element Positions

| Element | Typical Coordinate | Notes |
|---------|-------------------|-------|
| Start Menu Button | `(0.02, 0.98)` | Bottom-left corner |
| First Search Result | `(0.15, 0.35)` | Upper-left area |
| Center of Screen | `(0.5, 0.5)` | Middle |
| Save/OK Button | `(0.7, 0.8)` or `(0.7, 0.9)` | Bottom-right of dialogs |
| Menu Bar | `y=0.05` to `0.1` | Top of window |
| Canvas Center | `(0.5, 0.5)` | Middle of work area |
| System Tray | `x=0.9+, y=0.98` | Bottom-right corner |

### Resolution Independence

Coordinates are normalized so demos work across different screen resolutions:
- **1920x1080**: `CLICK(x=0.5, y=0.5)` → pixel (960, 540)
- **2560x1440**: `CLICK(x=0.5, y=0.5)` → pixel (1280, 720)
- **3840x2160**: `CLICK(x=0.5, y=0.5)` → pixel (1920, 1080)

---

## Action Type Distribution

Across all 35 synthetic demos:

| Action Type | Approximate % | Use Case |
|-------------|--------------|----------|
| `CLICK` | 60% | Most common interaction |
| `WAIT` | 20% | UI transition delays |
| `TYPE` | 10% | Text input |
| `DONE` | 5% | Task completion |
| `DRAG` | 3% | Drawing, selecting |
| `HOTKEY` | 1% | Keyboard shortcuts |
| `RIGHT_CLICK` | <1% | Context menus |
| `SCROLL` | <1% | Navigation |

---

## Quality Metrics

### Demo Characteristics

- **Average steps per demo:** 11
- **Shortest demo:** 4 steps (Type text in Notepad)
- **Longest demo:** 18 steps (Set alarm for 8:00 AM)
- **Average wait time:** 1.0 seconds
- **Action success rate:** 100% (when coordinates are accurate)

### Validation Results

All demos in the library pass validation for:
- ✅ Format correctness
- ✅ Action syntax
- ✅ Coordinate ranges (0.0-1.0)
- ✅ Step numbering
- ✅ DONE() termination
- ✅ Reasoning presence

---

## Use in Prompts

Here's how a demo is actually used in an API call:

```python
system_prompt = f"""
You are a Windows automation agent. Here's an example demonstration:

=== EXAMPLE: {demo.task} ===

{demo.content}

=== END EXAMPLE ===

Now, for the current task, follow the same format.
Provide your next action using the exact syntax shown above.
"""

user_prompt = f"""
Task: {current_task}
Screenshot: [base64 encoded image]

What action should you take next?
"""

response = client.chat.completions.create(
    model="claude-sonnet-4-5",
    messages=[
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
)
```

---

## Viewing These Examples

All examples can be viewed interactively in the browser-based viewer:

**Location:** `/path/to/openadapt-viewer/synthetic_demo_viewer.html`

**Open with:**
```bash
open /path/to/openadapt-viewer/synthetic_demo_viewer.html
```

The viewer provides:
- Filterable demo browser
- Syntax-highlighted display
- Prompt usage examples
- Domain statistics
- Action reference guide

---

**Generated:** 2026-01-17
**Part of:** OpenAdapt Synthetic Demo Library
