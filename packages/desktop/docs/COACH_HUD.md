# Agent-guided demonstration HUD

OpenAdapt Desktop already owns a native control overlay. This document extends that overlay into a coach for a first demonstration: a person does the clicks on the live system, and the agent they already use suggests the next move.

The website isn't the runner. Citrix, RDP, a Windows EMR, and a local browser all use this overlay.

## Decision: who actuates

The person actuates. The agent suggests.

For a novel GUI path, the calling agent isn't a production demonstration. Unsigned success is failure. A person admits later. The overlay must never look like `VERIFIED` or a Seal because a model proposed a click.

Agent-with-confirm is a later governed-replay mode, after a certified bundle exists. Agent-solo is out of scope for this HUD.

## Decision: where the hint lives

The hint has more than one home, and they don't share a wire.

The compact native overlay shows a closed-vocab status (`LOCAL CAPTURE`, `PAUSED`) plus, during recording, a short local hint and an optional "Your turn" badge. No overlay buttons while observing, recording, executing, verifying, pausing, resuming, or stopping. The window stays pointer-transparent, non-focusable, and capture-excluded.

`overlay://coach` is the local-only coach channel. Hint text, turn, pause reason, operator response, and an optional bound target rect travel there. That payload is never written into evidence, `report.json`, a Seal, Cloud ingest, PostHog, or `overlay://frame`. Capture exclusion stays on even in presentation mode; a compositor that wants a demo derivative already subscribes to `overlay://frame`.

The main Desktop window can show the pack playbook (step list, QR, pack URL) if the chat agent is weak. Hotkeys bind there, not on the click-through overlay. Pack presence on openadapt.ai stays claims-only. Desktop may fetch the pack URL locally. It must not POST live GUI state to that site.

Chat stays the agent's own UI. OpenAdapt doesn't grow a second chat product.

## Decision: how the existing agent drives it

Coding agents on the machine (Claude Code, Cursor, a local MCP client) already sit next to Desktop. They drive the coach through the existing loopback IPC, not a new daemon.

1. Desktop writes `~/.openadapt/desktop_ipc.json` = `{host, port, token, protocol_version}` when the engine starts. That file already exists for the tray.
2. The agent connects to `127.0.0.1` with the session token and sends newline JSON `{type, data, token}`.
3. `set_coach` / `get_coach` / `clear_coach` are engine commands on that socket (and on the Tauri sidecar JSON-lines wire). `set_coach` emits `engine://coach`. The overlay reduces it and emits `overlay://coach` for local subscribers.
4. The tray allowlist still drops `coach` events. A tray client must never receive hint text.

ChatGPT / Claude in a browser can't run local MCP. They already fetched the pack. Desktop can load that pack URL itself and step the playbook in the main window. MCP in `openadapt-agent` is the later wrapper around these same commands; this slice leaves the payload stable so that wrapper can call `set_coach` without a Desktop redesign.

A coach hint is a short instruction from the playbook or the agent, such as "Open the claim screen". It must not include a person name, a record id, chart text, a URL, or a typed value.

## Decision: auth pause

The person types secrets in the real application. OpenAdapt, the HUD, and the agent do not ask them to paste a password into chat.

When the agent (or the person) marks a secret field, the overlay goes to `paused` and may become interactive. The card copy is exactly:

> Type in the application. Continue here when done.

Continue is an overlay control at that pause boundary. Capture pause/resume is still unimplemented in the recorder (`pause_recording` reports current status; Flow record sessions advertise `pause: false`). v1 coach pause is an overlay interaction boundary. Typing in the app still reaches the app because the overlay shrinks to a compact card and the rest of the screen is the live UI. If lossless capture pause lands later, this same `pause_reason: "auth"` path should request it.

## Decision: feedback

Also a pause boundary, never a click-through intercept.

| Operator action | Coach field | Overlay |
|---|---|---|
| Continue | `operator_response: "continue"` | Leaves pause, back to recording visuals |
| That was wrong | `operator_response: "wrong"` | Stays paused until the agent sends the next hint |
| Skip | `operator_response: "skip"` | Stays paused; agent decides whether skip is legal |
| Done | `operator_response: "done"` | Stays paused; agent or the person then stops from Desktop |
| Secret field | `pause_reason: "secret_field"` | Same card as auth |

Wrong-step, skip, and done do not compile, admit, or Seal anything. They are notes for the agent and for a later human admission.

## Decision: what the agent may see

v1: nothing from the live GUI.

The agent already has the pack. It can `get_coach` to read the last hint and the last operator response. It does not receive screenshots, UIA/AX/AT-SPI text, OCR, URLs, or field values. Structural observers still persist beside capture events for compile, on the machine. They are not a coach feed.

A later local-only snapshot of control *roles* (not values) could help an on-machine coding agent. That is a separate change and still must not leave the host.

## Ghost target ring

If `set_coach` includes a rect *and* an exact observation binding (`observation_hmac_sha256` of 64 hex chars, or a `media_frame` hash plus `frame_index`), the overlay may grow to the current monitor, stay click-through, and draw a dashed ring. Coordinate space is `top_level_viewport_normalized`. Omit the ring when the binding is missing. Do not invent a rect from a screenshot, a selector, or interpolation.

While paused or terminal, the overlay shrinks back to a compact card so the person can type in the app. The ring is visual-only and capture-excluded.

## Native window contract (unchanged, plus coach)

During `observing`, `recording`, `executing`, `verifying`, `pausing`, `resuming`, `stopping`: pointer-transparent, non-focusable, capture-excluded, no overlay controls. Status, hint, badge, and ring may paint. Interactive controls only at `paused` or terminal.

`overlay://frame` stays the Types v2 closed vocabulary. Coach fields do not appear in `state_id`, `status`, `workflow_label`, or `target_tracking` of that frame. Flow's runtime emitter is unchanged.

Same overlay path for native, RDP, and Citrix. RDP/Citrix stay black-box: OCR, relational anchors, and fresh-frame verification on the runner, not fake UIA across the remote boundary.

## Rejected shapes

DOM or accessibility injection puts OpenAdapt inside the target app's tree. It intercepts the physical click we are trying to record, and it contaminates capture. The overlay already exists to avoid that.

A website HUD cannot see Citrix or a local EMR. The pack page is claims. The runner is Desktop.

WebRTC (or any pixel pipe) to Cloud or openadapt.ai is a PHI export. Do not add it.

Phonetic coaching still needs a HUD, and it is a poor fit for a dense form.

Screenshot-to-ChatGPT is the same export with extra steps. The person and the agent look at the real UI; they do not upload it.

## `set_coach` payload

Local only. Schema `openadapt.control-overlay-coach/v1`. Not an `openadapt-types` frame.

```json
{
  "schema_version": "openadapt.control-overlay-coach/v1",
  "hint": "Open the claim screen",
  "turn": "your_turn",
  "pause_reason": null,
  "target": null,
  "operator_response": null
}
```

`turn` is `your_turn`, `wait`, `auth`, or `feedback`. `pause_reason` is `auth`, `secret_field`, `wrong_step`, `skip`, `done`, or `next`. Hint max 80 characters after whitespace collapse. The engine drops a hint that contains a URL, `@`, or a run of six or more digits rather than display a maybe-identifying string.

## Agent recipe (coding agent on the same machine)

```python
import json, socket
from pathlib import Path

disc = json.loads(Path.home().joinpath(".openadapt/desktop_ipc.json").read_text())
sock = socket.create_connection((disc["host"], disc["port"]))
sock.sendall((json.dumps({
    "type": "set_coach",
    "token": disc["token"],
    "data": {"hint": "Open the claim screen", "turn": "your_turn"},
}) + "\n").encode())
print(sock.makefile().readline())
```

`get_coach` on the same socket returns the current payload to that connection only. Poll it after you ask the person to continue.

MCP in `openadapt-agent` should wrap these three commands when that repo is extended. Until then, this IPC is the interface.

## v1 gaps

Lossless capture pause/resume is still a recorder no-op. Coach pause doesn't pretend otherwise.

The main window doesn't fetch a pack URL or bind hotkeys yet. The overlay is the part a person can feel during recording.

`openadapt-agent` doesn't expose `set_coach` as an MCP tool yet. The payload above is what it should send.

A bound target ring needs an observation HMAC the first-run agent usually does not have. Most first demonstrations show the hint without a ring, which is the correct omission.
