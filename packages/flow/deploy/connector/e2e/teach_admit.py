"""End-to-end check, author side: run `openadapt wrap`'s first run offline: the wrap CLI's own task loader and
WrappedTask, with a scripted FrameworkAgent instead of a model provider.

No model is called. The scripted agent reads the structural listing and acts
the way a model would. A throwaway admitter key admits the program after the
canary, as `--admit-key` does; the checks are confirmed by a named reviewer, as
`--person` does at a terminal.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

from openadapt_flow.approval_mode.approver_roster import ROOT_ENV
from openadapt_flow.integrations.agents.recording import load_task
from openadapt_flow.integrations.agents.skills import FrameworkAgent
from openadapt_flow.jit import authority
from openadapt_flow.jit.keys import CustomerModelKey
from openadapt_flow.teach_authorization_fixture import LocalAuthorizer
from openadapt_flow.wrap import WrappedTask

R = Path(sys.argv[1])
request_id, note = sys.argv[2], sys.argv[3]
os.environ.setdefault(authority.KEY_PASSPHRASE_ENV, "byoc-pilot-synthetic-passphrase")
key_dir = R / "keys"
pem = R / ("keys" + "-files") / "ci.pem"
if not pem.is_file():
    pem.parent.mkdir(parents=True, exist_ok=True)
    authority.register_principal(
        key_dir,
        role="admitter_key",
        principal_id="ci:byoc-pilot-admitter",
        public_key_hex=authority.generate_encrypted_key(pem),
    )
admitter = authority.principal_from_key(authority.load_encrypted_key(pem), key_dir)


def ref(listing: str, name: str) -> str:
    for line in listing.splitlines():
        m = re.search(r"\[(e\d+)\] \S+ \"" + re.escape(name) + '"', line)
        if m:
            return m.group(1)
    raise AssertionError(f"no {name!r} in listing:\n{listing}")


def run(task: str, tools):
    value = task.split("text ", 1)[1]
    listing = tools.observe()
    listing = tools.type_text(value, ref(listing, "Note"))
    listing = tools.click(ref(listing, "Save"))
    tools.finish("saved")
    return {
        "calls": [
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        ]
    }


setup = load_task(
    R / "task.json",
    offer=lambda proposal: "ci:byoc-pilot-reviewer",
    admitter=admitter,
    key_dir=key_dir,
    model_key=CustomerModelKey("anthropic", "env:TEST_ONLY", "not-a-real-key"),
)
# A person's signed teach authorization for this task (what the exploring
# agent may do), as `--teach-authorization` passes. Throwaway roster keys.

task_id = setup.cache.task_id(setup.instruction, {"note": note})
folder = R / ("keys" + "-teach") / task_id
root_pin = R / "keys-root.ok"
root = (
    LocalAuthorizer(key_dir, "ci:byoc-pilot-roster-admin")
    if not root_pin.exists()
    else None
)
if root is not None:
    root_pin.write_text(root.root)
pinned_root = root_pin.read_text().strip()
if not re.fullmatch(r"[0-9a-f]{64}", pinned_root):
    raise ValueError("test approver root pin is invalid; use a fresh work directory")
os.environ[ROOT_ENV] = pinned_root
lead = (
    LocalAuthorizer(key_dir, "ci:byoc-pilot-teach-lead", added_by=root)
    if root is not None
    else None
)
if lead is not None:
    os.environ[ROOT_ENV] = lead.root
    statement = lead.issue(
        folder,
        {
            "task": {
                "task_id": task_id,
                "summary": "Add a note to the clinic notebook",
            },
            "purposes": ["teach", "repair"],
            "origins": [os.environ["NOTES_ORIGIN"]],
            "inputs": {"note": {"value": note}},
            "constants": [{"value": "Notes"}],
            "writes": {
                "record_kind": "note",
                "identity": {"list": {"value": "demo"}},
                "fields": {"note": {"value": note}},
            },
        },
    )
    setup.cache.common["teach_authorization"] = lead.authority(statement)
agent = FrameworkAgent(run, principal_id="agent:scripted-byoc-pilot", model="scripted")
task = WrappedTask(
    cache=setup.cache,
    agent=agent,
    instruction=setup.instruction,
    parameters=setup.parameters,
    runs_dir=R / "runs",
)
s = time.perf_counter()
if os.environ.get("REHEARSAL_DEBUG"):
    run = setup.cache.run(setup.instruction, {"note": note}, agent)
    print(
        json.dumps(
            {"status": run.status, "detail": run.detail, "compile": run.compile},
            default=str,
        )[:4000]
    )
    raise SystemExit(0 if run.status == "admitted" else 1)
result = task.call(request_id, {"note": note})
print(
    json.dumps(
        {"seconds": round(time.perf_counter() - s, 2), "result": result}, default=str
    )[:3000]
)

if result.get("status") != "admitted":
    raise SystemExit(1)
