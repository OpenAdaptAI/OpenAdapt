# Windows desktop parity runbook (Parallels + in-guest agent)

How to bring the **desktop** path to parity with the **web** path — record →
compile → replay with the structural (UIA) + vision ladder, identity, and
effect verification — and run the **snapshot-safe live proof** in one pass on a
machine with the Parallels Windows 11 VM.

This is the operator runbook. The design/rationale lives in
`openadapt_flow/backends/win_agent/README.md` (the agent) and
`docs/desktop/PHASE2.md` (the benchmark).

---

## 0. What "parity with web" means here

| Capability | Web (Playwright) | Desktop (Windows) |
|---|---|---|
| Screenshot + input | `PlaywrightBackend` | `WindowsBackend` over the in-guest agent |
| Structural top rung | DOM `#id` / role+name (`dom_arm`) | **UIA `AutomationId` / role+name** (`WindowsBackend.structural_locator_at` / `locate_structural`) |
| Identity verification | DOM `structured_text_at` + OCR band | **UIA `structured_text_at`** + OCR band |
| Visual fallback ladder | template / ocr / geometry | same (backend-agnostic resolver) |
| Effect verification | postconditions + effects | same IR; system-of-record effects need an app API (unchanged) |

The resolver, replayer, identity, and risk gates are **backend-agnostic** — the
same code drives both. Desktop parity is achieved by (a) a backend that
implements the structural + identity capabilities, and (b) getting those
locators onto the bundle at record time.

**Proven in code (no VM):** the `WindowsBackend` structural/identity contract,
the hardened in-guest agent, record→compile→replay conformance over a mock WAA
server, and the desktop-recording arming path.
**Pending a live-VM run:** the end-to-end proof that the UIA structural rung
fires against a real Windows app — that is exactly what step 4 runs.

---

## 1. Provision the in-guest agent (session 1)

`prlctl exec` runs as SYSTEM in **session 0**, which cannot screenshot or drive
the interactive desktop. The agent must run in **session 1**. Two ways:

### Automated (what the e2e uses)
`ParallelsVM.launch_agent()` pushes `openadapt_flow/backends/win_agent/server.py`
into the guest and starts it in session 1 via `scripts/desktop/session1_launch.py`
(`WTSQueryUserToken` → `CreateProcessAsUserW`, `lpDesktop=winsta0\default`), then
polls `/health`. Nothing to do by hand.

### Manual / unattended (logon scheduled task)
1. Copy `openadapt_flow/backends/win_agent/` into the guest, e.g.
   `C:\oa\win_agent\`.
2. As the interactive user, persist config and register a **logon** task that
   runs **as that user** (not SYSTEM):
   ```bat
   setx OAFLOW_AGENT_HOST 0.0.0.0
   setx OAFLOW_AGENT_TOKEN <paste-a-secret>
   schtasks /Create /TN OAFlowWinAgent /SC ONLOGON /RL HIGHEST ^
     /TR "C:\oa\win_agent\run_agent.bat" /F
   ```
3. Log on (or `schtasks /Run /TN OAFlowWinAgent`) and verify **in the guest**:
   ```
   curl http://127.0.0.1:5000/health
   ```
   `active_console_session` must be a real session id (not `-1` or `0`). If it is
   `0`, the agent landed in session 0 — relaunch it from the interactive session.

**Security:** default bind is loopback. Whenever you set `--host 0.0.0.0` (needed
for a host→guest `WindowsBackend`), **also set a bearer token** and pass the same
token to `WindowsBackend(auth_token=...)`. `/execute_windows` is remote code
execution by contract.

---

## 2. Record a desktop task → a bundle (same shape as web)

Preferred (reaches parity — arms UIA locators):

```python
from openadapt_flow.adapters.desktop_recorder import record_desktop_demo
from openadapt_flow.backends import WindowsBackend
from openadapt_flow.compiler import compile_recording

backend = WindowsBackend("http://<guest-ip>:5000", auth_token="<secret>")

def driver(rec):
    rec.click(x0, y0)                 # a UIA AutomationId is armed at each click
    rec.type_text("value", param="p")
    rec.press("Enter")

recording = record_desktop_demo(backend, "out/recording", driver)
bundle = compile_recording(recording, "out/bundle", name="my-desktop-task")
```

Check parity of the compiled bundle:

```python
from openadapt_flow.adapters.desktop_recorder import structural_armed_coverage
from openadapt_flow.ir import Workflow
print(structural_armed_coverage(Workflow.load("out/bundle")))  # armed_coverage == 1.0
```

Offline alternative (`openadapt_flow.adapters.capture.convert_capture`): converts
an `openadapt-capture` session into the identical bundle shape, but **cannot** arm
the structural locator (no live UIA tree at conversion time), so replay uses the
visual ladder only. Closing that is a documented follow-up (a `uia_arm` re-arming
pass); see the `desktop_recorder` module docstring.

---

## 3. Replay

```python
from openadapt_flow.ir import Workflow
from openadapt_flow.runtime import Replayer

report = Replayer(backend, use_structural=True).run(
    Workflow.load("out/bundle"), params={"p": "value"},
    bundle_dir="out/bundle", run_dir="out/run",
)
print(report.rung_counts)   # {'structural': N} when the UIA rung drives it
assert report.success
```

The structurally-resolved point flows through the **same** click path as any
visual rung, so the pre-click identity gate and the irreversible risk gate still
fire.

---

## 4. Run the snapshot-safe live proof (one pass)

The opt-in e2e (`tests/e2e/test_parallels_desktop_e2e.py`) does the whole loop
against the built-in Windows **Calculator** (deterministic, no PHI): snapshot →
ensure VM up → launch the agent in session 1 → record→compile→replay via
`WindowsBackend` → assert the UIA structural rung fires and the run completes →
**revert to the snapshot**.

```bash
# On the Mac with the Parallels VM:
OAFLOW_PARALLELS_E2E=1 pytest -q tests/e2e/test_parallels_desktop_e2e.py
# Optional: OAFLOW_PARALLELS_VM_UUID='{...}' to target a different VM.
```

**Snapshot safety, guaranteed:** the test takes a **fresh** snapshot before it
touches the guest and reverts to it in a `finally` block. It **never deletes**
the VM or any snapshot, and it is **skipped entirely** unless
`OAFLOW_PARALLELS_E2E=1`. Always confirm your VM has a known-good snapshot before
running anything against it.

---

## 5. Verify without a VM (what CI does)

```bash
pytest -q --ignore=tests/e2e         # macOS/Linux: Windows-only bits mocked/skipped
pytest -q tests/test_win_agent_server.py \
         tests/test_windows_backend.py \
         tests/test_desktop_recorder.py \
         tests/test_parallels_vm.py
ruff check openadapt_flow/backends openadapt_flow/adapters
```

The e2e file is **collected but skipped** without the env var, so a plain
`pytest` never touches the VM.
