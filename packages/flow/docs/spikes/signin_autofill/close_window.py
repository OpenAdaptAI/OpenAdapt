"""Close the Chrome window whose page is on a given origin (test setup only)."""

import sys

import signin_replay as s

origin = sys.argv[1]
app = s.AXUIElementCreateApplication(s._chrome_pid())
for win in s._attr(app, "AXWindows") or []:
    try:
        web = s._web_area(win)
    except s.Refused:
        continue
    if s._origin(s._attr(web, "AXURL") or "") == origin:
        button = s._attr(win, "AXCloseButton")
        print("closed", origin, s.AXUIElementPerformAction(button, "AXPress"))
        break
