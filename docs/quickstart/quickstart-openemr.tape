# Records docs/quickstart/quickstart-openemr.gif with VHS (https://github.com/charmbracelet/vhs, MIT).
#
#   PATH="$PWD/.venv/bin:$PATH" vhs docs/quickstart/quickstart-openemr.tape
#
# Run it from a clone with Docker running (with Podman, export DOCKER_HOST
# first; the recording's shell inherits it). The fixture from an earlier run
# is reused, so this records a warm run. The browsers stay hidden (no display
# in the recording). Everything else is the real command, start to finish, at
# real speed.

Output docs/quickstart/quickstart-openemr.gif

Set Shell bash
Set FontSize 15
Set Width 1240
Set Height 1100
Set Padding 24
Set Framerate 12
Set TypingSpeed 45ms
Set Theme "Builtin Dark"

Hide
Type "unset DISPLAY WAYLAND_DISPLAY; export SSH_CONNECTION=recording BROWSER=true OPENADAPT_FLOW_SCRUB=off OPENADAPT_SEAL_HOME=$(mktemp -d) PS1='$ '; cd $(mktemp -d) && clear"
Enter
Show

Sleep 500ms
Type "openadapt quickstart --app openemr"
Sleep 400ms
Enter
Wait+Screen@600s /Press Enter/
Sleep 2s
Enter
Wait+Screen@300s /Give your agent/
Sleep 8s
