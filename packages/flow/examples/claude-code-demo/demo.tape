# Screen capture of Claude Code calling the compiled workflow.
# Prerequisite: ./demo.sh has run (app up, bundle built, mcp.json written).
# Record with: vhs demo.tape
Output demo.gif

Set Shell zsh
Set FontSize 14
Set Width 1120
Set Height 640
Set Padding 16
Set Framerate 12
Set TypingSpeed 25ms

# Hidden setup. The alias keeps MCP servers and browser-extension prompts
# from the recording machine's own Claude Code settings out of the capture.
Hide
Type "setopt interactivecomments; alias claude='command claude --no-chrome --strict-mcp-config'; PS1='$ '; clear"
Enter
Show

Type "# An expenses app on SQLite. Adding an expense was recorded once and compiled."
Enter
Sleep 1s
Type "./demo.sh rows"
Enter
Sleep 3s

Type "claude --mcp-config mcp.json"
Enter
Sleep 5s
Type "Add an 18.75 expense for parking at the client site. Then check the run report and tell me in one line what OpenAdapt verified."
Sleep 500ms
Enter
Wait+Screen@180s /done \d/
Sleep 3s

# Show the raw tool results, then go back.
Ctrl+O
Sleep 5s
Ctrl+O
Sleep 1s
Hide
Ctrl+C
Sleep 500ms
Ctrl+C
Sleep 1s
Type "clear"
Enter
Show

Type "./demo.sh rows"
Enter
Sleep 4s
