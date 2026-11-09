# Records docs/quickstart/quickstart.gif with VHS (https://github.com/charmbracelet/vhs, MIT).
#
#   PATH="$PWD/.venv/bin:$PATH" vhs docs/quickstart/quickstart.tape
#
# The browser stays hidden (no display in the recording) and the side-by-side
# page is not opened, so nothing appears outside the terminal. Everything else
# is the real command, start to finish, at real speed.

Output docs/quickstart/quickstart.gif

Set Shell bash
Set FontSize 15
Set Width 1240
Set Height 1060
Set Padding 24
Set Framerate 24
Set TypingSpeed 45ms
Set Theme "Builtin Dark"

Hide
Type "unset DISPLAY WAYLAND_DISPLAY; export SSH_CONNECTION=recording BROWSER=true OPENADAPT_SEAL_HOME=$(mktemp -d) PS1='$ '; cd $(mktemp -d) && clear"
Enter
Show

Sleep 500ms
Type "openadapt quickstart"
Sleep 400ms
Enter
Wait+Screen@180s /Press Enter/
Sleep 2s
Enter
Wait+Screen@180s /Give your agent/
Sleep 6s
