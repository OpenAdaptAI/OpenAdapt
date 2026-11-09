# Screen capture of the same tool call against a broken app that shows
# "Expense saved." but drops the write. Prerequisite: ./demo.sh has run.
# Record with: vhs halt.tape   (it restarts the app normally at the end)
Output halt.gif

Set Shell zsh
Set FontSize 14
Set Width 1120
Set Height 640
Set Padding 16
Set Framerate 12
Set TypingSpeed 25ms

Hide
Type "setopt interactivecomments; alias claude='command claude --no-chrome --strict-mcp-config'; PS1='$ '; clear"
Enter
Show

Type "# Break the app: it still says 'Expense saved.' but never writes the row."
Enter
Sleep 1s
Type "./demo.sh break"
Enter
Sleep 2s

Type "claude --mcp-config mcp.json"
Enter
Sleep 5s
Type "Add a 96.00 expense for client dinner. Tell me in one line whether it was saved."
Sleep 500ms
Enter
Wait+Screen@180s /done \d/
Sleep 5s
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

Hide
Type "./demo.sh fix"
Enter
Sleep 2s
