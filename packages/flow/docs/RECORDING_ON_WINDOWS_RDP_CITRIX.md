# Recording on Windows, RDP and Citrix

OpenAdapt learns a task from one or more demonstrations. A person or an agent
shows the task in the real app, OpenAdapt records each step, and a person
accepts the recording before anything runs from it. This page covers native
Windows apps and remote sessions over RDP or Citrix.

There are two ways to show the task:

- **A person** does the task while `openadapt-flow record` or OpenAdapt
  Desktop records it. See [desktop/RECORDING.md](desktop/RECORDING.md).
- **An AI agent** does the task through the recording tools in
  [openadapt-agent](https://github.com/OpenAdaptAI/openadapt-agent#record-a-first-workflow)
  (`openadapt-agent serve --authoring`). A person stays at the computer to sign
  in, type secrets, and accept the result.

Every step in the recording says who did it: `actor` in `events.jsonl` is
`agent` or `person`, and `demonstrated_by` in `meta.json` is `agent`,
`person`, or `agent_and_person`. An agent never accepts its own recording.

ChatGPT and Claude.ai sessions that reach the computer through OpenAdapt
Desktop can't drive these surfaces. On native Windows, RDP, and Citrix they
suggest the next step and the person clicks.

## Native Windows apps: the Windows agent server

### What it is

The Windows agent server is a small HTTP server that ships with openadapt-flow
(`openadapt_flow.backends.win_agent`). It runs on the Windows computer and
does three things there: it takes screenshots of the desktop, sends clicks
and key presses, and reads the UI Automation details of the control that was
clicked. Flow's replay uses the same server, so an agent's input during
recording goes through the same path that later runs use.

### Why you need it

Flow and the agent don't have to run on the Windows computer. They can run on
another computer, such as a Mac. That computer can't click in a Windows app or
read its controls, so something on the Windows side has to do it. The Windows
agent server is that piece. When Flow and the agent do run on the Windows
computer, they still go through the server, at `localhost`.

The server must run in the desktop session of the person signed in to
Windows. A Windows service, a scheduled task running as SYSTEM, or a command
started over SSH runs in a different session, where screenshots come back
blank and input goes nowhere.

### Start it

On the Windows computer, sign in, open a terminal on the desktop, and run:

```bat
pip install openadapt-flow mss pyautogui uiautomation
set OAFLOW_AGENT_TOKEN=<a long random secret>
python -m openadapt_flow.backends.win_agent.server --port 5001
```

Check it from the same computer:

```bat
curl http://127.0.0.1:5001/health
```

`active_console_session` must be a session number other than `-1` or `0`. If
it's `0`, the server started in the wrong session; start it again from a
terminal on the desktop. [Its README](../openadapt_flow/backends/win_agent/README.md)
covers starting it at sign-in and the security details.

### The token

With `OAFLOW_AGENT_TOKEN` set (or `--token`), the server answers screenshot
and input requests only from callers that send the same token. `/health`
doesn't need it. openadapt-agent reads `OAFLOW_AGENT_TOKEN` from its own
environment and sends it with every request, so set the same value on the
computer that runs the agent. Treat it like a password: anyone with the token
who can reach the server can see the screen and control the computer.

### Connect the agent

If the agent runs on the Windows computer:

```bash
openadapt-agent serve --authoring --windows-agent-url http://localhost:5001
```

If the agent runs on another computer, keep in mind that the server listens
on `127.0.0.1` by default, so only programs on the Windows computer can reach
it. Pick one of these:

- **SSH tunnel.** Turn on OpenSSH Server on the Windows computer. On the Mac,
  forward the port and point the agent at the local end:

  ```bash
  ssh -N -L 5001:127.0.0.1:5001 you@windows-pc
  export OAFLOW_AGENT_TOKEN=<the same secret>
  openadapt-agent serve --authoring --windows-agent-url http://localhost:5001
  ```

  The server stays on `127.0.0.1`, and SSH encrypts the traffic. The tunnel
  only carries traffic; the server itself still has to be started from the
  Windows desktop.

- **Direct HTTPS.** Start the server with `--host 0.0.0.0 --certfile cert.pem
  --keyfile key.pem` and a token, and pass
  `--windows-agent-url https://windows-pc:5001`. Flow refuses plain `http://`
  to any host other than `localhost`, because screenshots can show patient
  data. openadapt-agent has no option for pinning a certificate fingerprint,
  so the certificate must be one the agent's computer already trusts for that
  host name.

With an MCP client, set the token in the environment the client starts the
server with. In Claude Code:

```bash
claude mcp add openadapt-authoring -e OAFLOW_AGENT_TOKEN=<the same secret> -- \
  uvx --python 3.12 --from 'openadapt-agent[tutorial]' \
  openadapt-agent serve --authoring --windows-agent-url http://localhost:5001
```

If the agent reports that the Windows backend is missing, install
`openadapt-flow[windows]` next to it.

The recording keeps each clicked control's UI Automation id when Windows
reports one.

## RDP and Citrix: the pixel path

An RDP or Citrix session needs no server on the remote computer. The agent
drives the client window on the local computer, such as Microsoft Remote
Desktop or Citrix Workspace, the same way a person at that computer would:
it sees the window's pixels and sends clicks and key presses. The local
computer can be a Mac or a Windows PC.

```bash
# RDP: name the client app, and the window title if the app has more than one window
openadapt-agent serve --authoring --remote-window "Microsoft Remote Desktop" \
  --remote-window-title "Clinic PC" --authoring-screens scrubbed

# Citrix Workspace
openadapt-agent serve --authoring --citrix --authoring-screens scrubbed
```

A remote session has no DOM or UI Automation tree on the local side, so
`observe` returns no nodes. The agent clicks x,y positions in the client
window. On a Mac, the process that runs the agent needs the Screen Recording
and Accessibility permissions in System Settings.

## Screens are off by default

The agent can't see the screen unless the operator turns it on with
`--authoring-screens`:

| Value | What the agent gets |
| --- | --- |
| `off` (default) | No `screen` tool. |
| `scrubbed` | A `screen` tool that returns the pinned window with identifiers scrubbed. If scrubbing fails, it returns nothing. |
| `unscrubbed` | A `screen` tool that returns the pinned window as it is. |

`observe` returns no nodes on RDP, Citrix, or native Windows yet, so an agent
driving one of them needs `scrubbed` or `unscrubbed` to see where to click.

## What has been tested

The Windows and RDP/Citrix recording paths are covered by tests against a mock
Windows agent server and stand-in client windows. They haven't been run
against a real Windows, RDP, or Citrix session yet.
