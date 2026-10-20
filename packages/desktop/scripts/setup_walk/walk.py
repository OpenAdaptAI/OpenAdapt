#!/usr/bin/env python3
"""Walk Desktop from "I recorded something" to the first checked run.

Counts every input a person gives on the way and screenshots each one. It
runs the real Desktop engine (``bridge.py``) over a fresh data folder, the
real Flow underneath it, and MockMed's transactional server, with the UI from
``npm run dev`` in a headless browser. Nothing is mocked: every answer the
screens show comes from the engine, and the practice item, the drills and the
first item are real Flow runs against MockMed. All data is synthetic.

Usage (from packages/desktop, with ``npm run dev`` running)::

    OPENADAPT_DESKTOP_FLOW_BIN=<a wrapper for python -m openadapt_flow> \\
    PYTHONPATH=<flow>:<types>:<desktop> \\
        python scripts/setup_walk/walk.py --variant after --out docs/setup-walk/after

``--variant`` picks the step list for the screens on this branch (``after``)
or for the screens before it (``before``, run on the earlier commit). The
output folder gets one PNG per input and ``inputs.json``.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import socket
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from bridge import Bridge, serve  # noqa: E402
from seed import record, seed  # noqa: E402

INIT_SCRIPT = """
(({ token, bridge }) => {
  const callbacks = new Map();
  const listeners = new Map();
  let nextId = 1;
  try { localStorage.setItem('openadapt.desktop.local-session.v1', 'enabled'); } catch {}
  // Tauri's IPC has no connection limit; a browser allows six per host. The
  // commands a waiting run needs (approvals) and the event poll go to a
  // second host name for the same bridge, so calls parked behind a long run
  // can't starve them, as they can't in the app.
  const side = bridge.replace('127.0.0.1', 'localhost');
  const quick = new Set(['get_item_approval', 'answer_item_approval']);
  const post = (path, body) => fetch(
    (path === '/events' || quick.has(body && body.cmd) ? side : bridge) + path, {
    method: 'POST',
    headers: { 'content-type': 'application/json', 'x-walk-token': token },
    body: JSON.stringify(body || {}),
  }).then((r) => r.json());
  window.__TAURI_INTERNALS__ = {
    transformCallback(callback) { const id = nextId++; callbacks.set(id, callback); return id; },
    unregisterCallback(id) { callbacks.delete(id); },
    convertFileSrc(p) { return p; },
    async invoke(cmd, args) {
      if (cmd === 'plugin:event|listen') {
        const list = listeners.get(args.event) || [];
        list.push(args.handler);
        listeners.set(args.event, list);
        return args.handler;
      }
      if (cmd === 'plugin:event|unlisten') {
        for (const [name, list] of listeners) {
          listeners.set(name, list.filter((id) => id !== args.eventId));
        }
        return undefined;
      }
      if (cmd === 'engine_invoke') {
        const reply = await post('/invoke', { cmd: args.cmd, params: args.params || {} });
        if (!reply.ok) throw new Error(reply.error);
        return reply.result;
      }
      return undefined;
    },
  };
  window.__TAURI_EVENT_PLUGIN_INTERNALS__ = { unregisterListener() {} };
  setInterval(async () => {
    let events = [];
    try { events = await post('/events'); } catch { return; }
    for (const item of events) {
      const name = 'engine://' + item.event;
      for (const id of listeners.get(name) || []) {
        const callback = callbacks.get(id);
        if (callback) callback({ event: name, id, payload: item.payload });
      }
    }
  }, 250);
})
"""


#: Inputs that set a workflow up. ``item_*`` inputs belong to running an
#: item (its own values and its save approval), ``navigation`` to moving
#: between screens.
SETUP_KINDS = frozenset({"free_text", "choice", "confirmation", "decision"})


@dataclass
class Input:
    """One thing a person gives: what kind, where, and what it was."""

    screen: str
    kind: str  # SETUP_KINDS | item_value | item_run | item_approval | navigation
    label: str
    shot: str = ""
    note: str = ""


@dataclass
class Walk:
    page: Any
    out: Path
    inputs: list[Input] = field(default_factory=list)
    shots: int = 0

    def shot(self, name: str, *, full: bool = True) -> str:
        self.shots += 1
        file = f"{self.shots:02d}-{name}.png"
        self.page.wait_for_timeout(400)
        if full:
            # The app scrolls inside its own panes, so a full-page shot of the
            # window misses the rest. Grow the window to the tallest pane,
            # shoot, and put it back.
            height = self.page.evaluate(
                "() => Math.max(...[document.documentElement,"
                " ...document.querySelectorAll('main, .content')]"
                ".map((el) => el.scrollHeight + el.getBoundingClientRect().top))"
            )
            size = self.page.viewport_size
            self.page.set_viewport_size(
                {"width": size["width"], "height": max(size["height"], int(height) + 40)}
            )
            self.page.wait_for_timeout(200)
            self.page.screenshot(path=str(self.out / file))
            self.page.set_viewport_size(size)
        else:
            self.page.screenshot(path=str(self.out / file))
        return file

    def give(
        self,
        screen: str,
        kind: str,
        label: str,
        act: Callable[[], None],
        *,
        name: str,
        note: str = "",
    ) -> None:
        """Screenshot the input as the person sees it, then give it."""

        file = self.shot(name)
        self.inputs.append(Input(screen, kind, label, file, note))
        act()
        self.page.wait_for_timeout(600)

    def wait_text(self, text: str, timeout: float = 240_000) -> None:
        self.page.get_by_text(text, exact=False).first.wait_for(timeout=timeout)


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def button(page: Any, name: str) -> Any:
    return page.get_by_role("button", name=name, exact=True).first


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--variant", choices=["before", "after", "agent"], required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--base", default="http://localhost:1420")
    parser.add_argument("--work", type=Path, default=None)
    parser.add_argument("--headed", action="store_true")
    args = parser.parse_args()

    from openadapt_flow.mockmed.fault_server import serve as serve_mockmed
    from playwright.sync_api import sync_playwright

    out = args.out.resolve()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    work = Path(args.work or tempfile.mkdtemp(prefix="setup-walk-")).resolve()
    work.mkdir(parents=True, exist_ok=True)

    # A fixed port per run: the recording, the program and the runs must agree.
    mockmed_url, store, stop_mockmed = serve_mockmed(port=free_port())
    recording = record(mockmed_url, work / "captures" / "triage")
    token = secrets.token_hex(16)
    bridge = Bridge(work, token)
    server = serve(bridge, 0)
    bridge_url = f"http://127.0.0.1:{server.server_address[1]}"
    workflow_id = seed(bridge, recording, mockmed_url)
    # The engine's local socket, as the app runs it: the agent variant's MCP
    # calls reach the same dispatcher, under the same lock, through it.
    from openadapt_desktop.socket_server import DesktopSocketServer

    discovery = work / "desktop_ipc.json"
    socket_server = DesktopSocketServer(
        bridge.dispatcher.config,
        discovery_path=discovery,
        dispatcher=bridge.dispatcher,
        dispatch_lock=bridge._dispatch_lock,
    )
    socket_server.start()
    os.environ["OPENADAPT_DESKTOP_IPC"] = str(discovery)
    agent_log: list[dict] = []

    steps = __import__(f"steps_{args.variant}")
    started = time.monotonic()
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900}, device_scale_factor=1, color_scheme="light"
        )
        context.add_init_script(
            f"({INIT_SCRIPT})({json.dumps({'token': token, 'bridge': bridge_url})})"
        )
        page = context.new_page()
        page.on("pageerror", lambda error: print(f"  ! page error: {error}", flush=True))
        page.goto(args.base, wait_until="domcontentloaded")
        page.wait_for_timeout(1500)
        walk = Walk(page, out)
        try:
            steps.run(
                walk,
                workflow_id=workflow_id,
                mockmed_url=mockmed_url,
                work=work,
                reset_store=store.reset,
                agent_log=agent_log,
            )
        finally:
            walk.shot("end")
            browser.close()
    stop_mockmed()
    socket_server.stop()
    server.shutdown()

    counts: dict[str, int] = {}
    for item in walk.inputs:
        counts[item.kind] = counts.get(item.kind, 0) + 1
    summary = {
        "variant": args.variant,
        "app": "MockMed (openadapt_flow.mockmed.fault_server, ?fault=ok)",
        "workflow": "Add a triage note",
        "seconds": round(time.monotonic() - started),
        "counts": counts,
        "total": len(walk.inputs),
        # Setup: everything a person gives from "recorded" to "approved".
        "setup_inputs": sum(v for k, v in counts.items() if k in SETUP_KINDS),
        "first_item_inputs": sum(v for k, v in counts.items() if k.startswith("item_")),
        "navigation": counts.get("navigation", 0),
        "reached_checked_run": getattr(steps, "BLOCKED", None) is None,
        "blocked": getattr(steps, "BLOCKED", None),
        "inputs": [item.__dict__ for item in walk.inputs],
        "engine_commands": [call["cmd"] for call in bridge.calls],
        "agent_calls": [
            {"tool": call["tool"], "arguments": call["arguments"]} for call in agent_log
        ],
    }
    (out / "inputs.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        json.dumps(
            {k: summary[k] for k in ("counts", "setup_inputs", "first_item_inputs", "navigation")}
        )
    )


if __name__ == "__main__":
    main()
