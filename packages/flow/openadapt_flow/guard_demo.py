"""Guarded-save demo: a fake agent saves a referral to MockMed through the guard.

Run it with ``python -m openadapt_flow.guard_demo``. It starts the MockMed
fault server on localhost, and for each case a fake agent opens a chart page,
asks the guard, and posts the referral to MockMed's API the way a
computer-use agent's click on Save would. The guard reads ``GET /api/db``
(the system of record) to judge the write. All data is synthetic, nothing
leaves localhost, and no model is called.

Cases:

* ``correct``: the right chart is open and the save lands. ``VERIFIED``.
* ``wrong_patient``: the agent opened another patient's chart. The guard
  refuses at ``begin`` and nothing is posted.
* ``dropped``: the server answers 200 and stores nothing. The guard returns
  ``RECONCILIATION_REQUIRED`` and refuses a second attempt at the same task.
"""

from __future__ import annotations

import http.cookiejar
import json
import struct
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zlib
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from openadapt_flow.guard import GuardedSave, GuardRefused, GuardTask
from openadapt_flow.mockmed.chart_pages import READER_PASSWORD, READER_USERNAME
from openadapt_flow.verify_run import parse_verify_config

if TYPE_CHECKING:
    from openadapt_flow.approval_mode.human_attestation import ApproverSigner

NOTE = "Referral to orthopedics for knee pain"


def verify_yaml(base_url: str) -> dict[str, Any]:
    """The ``verify.yaml`` for the demo, as data."""

    return {
        "schema_version": "openadapt.verify/v1",
        "verifier": {"kind": "rest", "base_url": base_url, "records_path": "/api/db"},
        "effects": [
            {
                "kind": "record_written",
                "match": {
                    "patient_id": {"param": "patient_id"},
                    "type": "Referral",
                    "note": {"param": "note"},
                },
                "expected_count": 1,
                "count_new_only": True,
                # How long the read-back waits for a late write.
                "timeout_s": 1.0,
            }
        ],
    }


def referral_task(task_id: str, patient_id: str = "p1") -> GuardTask:
    return GuardTask(
        task_id=task_id,
        expected_record={"patient_id": patient_id},
        intended_fields={"note": NOTE},
        authorization_ref="ticket-demo-0001",
    )


class _Text(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in ("script", "style"):
            self._skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._skip:
            self._skip -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)


def tiny_png(rgb: tuple[int, int, int] = (240, 240, 240)) -> bytes:
    """A valid 1x1 PNG: the 'screenshot' a person is shown in the demo."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    raw = b"\x00" + bytes(rgb)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


class FakeAgent:
    """Stands in for a computer-use agent: opens a chart, clicks Save.

    Uses only the standard library, so the demo runs on a plain install.
    """

    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        self.http = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        form = urllib.parse.urlencode(
            {"username": READER_USERNAME, "password": READER_PASSWORD}
        ).encode()
        self._send(
            f"{self.base}/chart/signin", form, "application/x-www-form-urlencoded"
        )

    def _send(
        self, url: str, body: Optional[bytes] = None, content_type: str = ""
    ) -> str:
        request = urllib.request.Request(url, data=body)
        if content_type:
            request.add_header("Content-Type", content_type)
        try:
            with self.http.open(request, timeout=5) as response:
                return response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            # An app error page is still what the agent sees.
            return exc.read().decode("utf-8", "replace")

    def open_chart(self, patient_id: str) -> str:
        """Open a chart page and return its text, as the harness reads it."""

        parser = _Text()
        parser.feed(self._send(f"{self.base}/chart/patient/{patient_id}"))
        return " ".join(" ".join(parser.parts).split())

    def click_save(self, patient_id: str, note: str, *, fault: str = "") -> None:
        url = f"{self.base}/api/encounter" + (f"?fault={fault}" if fault else "")
        body = {"patient_id": patient_id, "type": "Referral", "note": note}
        self._send(url, json.dumps(body).encode(), "application/json")


def person_approves(
    run_dir: str,
    request_id: str,
    request_sha256: str,
    *,
    signer: "ApproverSigner",
    key_dir: Optional[Path] = None,
) -> None:
    """What ``openadapt-flow item-approval approve`` does for a person.

    ``signer`` is the person's registered approver key; the answer counts only
    because it signs it. ``key_dir`` is where that key is registered (the
    guard's own ``key_dir``).
    """

    from openadapt_flow.approval_mode.item_approval import (
        ItemApprovalStore,
        make_answer,
    )
    from openadapt_flow.approval_mode.model import ITEM_APPROVAL_ROUTE

    store = ItemApprovalStore(Path(run_dir), key_dir=key_dir)
    request = store.read_request(request_id)
    if request.digest() != request_sha256:
        raise ValueError("not the request the person saw")
    store.write_answer(
        make_answer(
            request,
            decision="approve",
            decided_by="human",
            route=ITEM_APPROVAL_ROUTE,
            approver_id=None,
            auth_method="none",
            policy=store.context(),
            signer=signer,
        )
    )


def run_case(
    case: str,
    guard: GuardedSave,
    agent: FakeAgent,
    *,
    signer: "ApproverSigner",
    task_id: Optional[str] = None,
) -> dict[str, Any]:
    """Run one demo case and return what happened, step by step.

    ``signer`` is the simulated person's approver key, registered in
    ``guard.key_dir``.
    """

    task = referral_task(task_id or f"demo-{case}-0001")
    opened = "p2" if case == "wrong_patient" else "p1"
    fault = "no_persist" if case == "dropped" else ""
    steps: list[str] = []
    out: dict[str, Any] = {"case": case, "steps": steps, "saved": False}
    screen = agent.open_chart(opened)
    steps.append(
        f"agent opened chart {opened}; task names {task.expected_record['patient_id']}"
    )
    try:
        g = guard.begin(task, screen_text=screen, agent_id="demo-agent-01")
    except GuardRefused as exc:
        steps.append(f"guard_begin refused: {exc.code}")
        out["refused"] = exc.code
        return out
    steps.append("guard_begin: identity verified, baseline read")
    commit = g.commit(screenshot_png=tiny_png(), screen_text=screen)
    if commit.status == "waiting_for_approval":
        steps.append("guard_commit: waiting_for_approval")
        assert commit.approval_run_dir and commit.approval_request_id
        person_approves(
            commit.approval_run_dir,
            commit.approval_request_id,
            commit.approval_request_sha256 or "",
            signer=signer,
            key_dir=guard.key_dir,
        )
        steps.append("person approved the save")
        commit = g.commit()
    steps.append(f"guard_commit: {commit.status}")
    agent.click_save(opened, NOTE, fault=fault)
    out["saved"] = True
    steps.append("agent clicked Save" + (f" (server fault: {fault})" if fault else ""))
    result = g.confirm()
    steps.append(
        f"guard_confirm: {result.verdict.value} (receipt outcome {result.outcome})"
    )
    out.update(result.to_dict())
    if case == "dropped":
        try:
            guard.begin(task, screen_text=screen, agent_id="demo-agent-01")
            steps.append("retry: allowed (unexpected)")
        except GuardRefused as exc:
            steps.append(f"retry of the same task: refused ({exc.code})")
            out["retry_refused"] = exc.code
    return out


def main(argv: Optional[list[str]] = None) -> int:
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    try:
        base = url.rstrip("/")
        with tempfile.TemporaryDirectory() as tmp:
            from openadapt_flow.approval_mode.demo_approver import (
                demo_approver,
                demo_root,
            )

            # The demo plays the person on made-up data, so it starts a
            # throwaway approver roster in its own temporary key directory and
            # pins the guard to it. Real use pins the deployment's root
            # (approval_mode/approver_roster.py).
            key_dir = Path(tmp) / "demo-keys"
            signer = demo_approver(key_dir)
            guard = GuardedSave(
                parse_verify_config(verify_yaml(base)),
                Path(tmp) / "state",
                approval="each_item",
                key_dir=key_dir,
                approver_root=demo_root(signer),
            )
            agent = FakeAgent(base)
            for case in ("correct", "wrong_patient", "dropped"):
                before = len(db.snapshot()["records"])
                out = run_case(case, guard, agent, signer=signer)
                after = len(db.snapshot()["records"])
                print(f"== {case}")
                for step in out["steps"]:
                    print(f"   {step}")
                print(f"   rows in the system of record: {before} -> {after}")
    finally:
        stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main(sys.argv[1:]))
