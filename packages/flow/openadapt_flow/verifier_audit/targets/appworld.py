"""AppWorld (Apache-2.0) as an audit target, through an out-of-process worker.

Reference trajectories are recorded API-call logs, one ``<task_id>.jsonl``
per task (``{"method", "url", "data"}`` per line), such as AppWorld's own
``api_calls.jsonl`` from running a task's shipped solution. Scorer:
AppWorld's own ``evaluate`` (the task's shipped unit tests). Ground truth:
a row-level multiset diff of the SQLite dump against the task's seed dump,
read by the worker with ``sqlite3``.

AppWorld's protected task data may be redistributed only encrypted, so audit
reports of AppWorld should be written with ``--redact`` before they leave a
private store.
"""

from __future__ import annotations

import json
import re
import select
import subprocess
from pathlib import Path
from typing import Any, Optional, Sequence

from openadapt_flow.verifier_audit.model import (
    Call,
    CallKind,
    Episode,
    Verdict,
    canonical_effect,
)

WORKER = Path(__file__).with_name("appworld_worker.py")
_SEGMENT_ID = re.compile(r"^\d+$")


def _kind(method: str, url: str) -> CallKind:
    if "/auth/token" in url:
        return CallKind.AUTH
    if url.startswith("/supervisor/complete_task") or (
        url.startswith("/supervisor/message") and method.lower() == "post"
    ):
        return CallKind.CLAIM
    if method.lower() == "get" or url.startswith("/supervisor"):
        return CallKind.READ
    return CallKind.WRITE


def to_call(raw: dict[str, Any]) -> Call:
    method, url = (
        raw["method"].lower(),
        raw["url"] if raw["url"].startswith("/") else "/" + raw["url"],
    )
    parts: list[str] = []
    args: dict[str, Any] = {}
    for part in url.split("/"):
        if _SEGMENT_ID.match(part):
            args[f"_path_{len(args)}"] = part
            parts.append("{}")
        else:
            parts.append(part)
    args.update(raw.get("data") or {})
    return Call(
        f"{method.upper()} {'/'.join(parts)}",
        args,
        _kind(method, url),
        {"method": method},
    )


def to_raw(call: Call) -> dict[str, Any]:
    method, template = call.name.split(" ", 1)
    path_args = sorted(
        (k for k in call.args if k.startswith("_path_")), key=lambda k: int(k[6:])
    )
    url = template
    for key in path_args:
        url = url.replace("{}", str(call.args[key]), 1)
    data = {k: v for k, v in call.args.items() if not k.startswith("_path_")}
    return {"method": method.lower(), "url": url, "data": data}


class AppWorldTarget:
    post_claim_steps = True

    def __init__(
        self,
        python: str,
        appworld_root: str,
        references: str,
        cache: Optional[str] = None,
        timeout: str = "1800",
    ) -> None:
        self.appworld_root = appworld_root
        self.references = Path(references)
        self.name = "appworld"
        cache_dir = cache or str(
            Path(appworld_root) / "experiments" / "verifier_audit_seed_cache"
        )
        self.timeout = float(timeout)
        self._next_id = 0
        Path(cache_dir).mkdir(parents=True, exist_ok=True)
        self._log = open(Path(cache_dir) / "worker.log", "a")  # noqa: SIM115
        self._proc = subprocess.Popen(
            [python, "-I", str(WORKER), appworld_root, cache_dir],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=self._log,
            text=True,
            bufsize=1,
        )

    def tasks(self) -> list[str]:
        return sorted(p.stem for p in self.references.glob("*.jsonl"))

    def reference(self, task: str) -> list[Call]:
        lines = (self.references / f"{task}.jsonl").read_text().splitlines()
        return [to_call(json.loads(line)) for line in lines if line.strip()]

    def instruction(self, task: str) -> Optional[str]:
        spec = Path(self.appworld_root) / "data" / "tasks" / task / "specs.json"
        if not spec.is_file():
            return None
        return str(json.loads(spec.read_text()).get("instruction", ""))

    def run(self, task: str, calls: Sequence[Call], *, score: bool) -> Episode:
        claim = next(
            (
                i
                for i in range(len(calls) - 1, -1, -1)
                if calls[i].kind is CallKind.CLAIM
            ),
            None,
        )
        # A claim-time dump costs as much as the final one; take it only where
        # it can differ from the final state (a write after the claim) and on
        # the reference, which the late-write family compares against.
        if claim is not None and not (
            list(calls) == self.reference(task)
            or any(c.kind is CallKind.WRITE for c in calls[claim + 1 :])
        ):
            claim = None
        self._next_id += 1
        req = {
            "id": self._next_id,
            "task": task,
            "calls": [to_raw(c) for c in calls],
            "claim_index": claim,
            "score": score,
        }
        assert self._proc.stdin and self._proc.stdout
        self._proc.stdin.write(json.dumps(req) + "\n")
        self._proc.stdin.flush()
        resp = self._read(self._next_id)
        return Episode(
            final_effect=canonical_effect(resp["final_rows"]),
            claim_effect=canonical_effect(resp["claim_rows"])
            if resp["claim_rows"] is not None
            else None,
            verdict=Verdict(resp["verdict"]) if resp["verdict"] else None,
            call_errors={int(k): v for k, v in resp["call_errors"].items()},
            scorer_detail=resp["detail"],
        )

    def _read(self, want: int) -> dict[str, Any]:
        """Read the response to request ``want``. Lines that are not this
        request's JSON response are skipped, so stray output cannot shift
        later responses onto the wrong request."""
        assert self._proc.stdout
        while True:
            ready, _, _ = select.select([self._proc.stdout], [], [], self.timeout)
            if not ready:
                self._proc.kill()
                raise TimeoutError(f"AppWorld worker gave no answer in {self.timeout}s")
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError(
                    "AppWorld worker exited; see worker.log in the cache"
                )
            try:
                resp = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(resp, dict) and resp.get("id") == want:
                return resp

    def close(self) -> None:
        if self._proc.stdin:
            self._proc.stdin.close()
        self._proc.wait(timeout=60)
        self._log.close()


def make_target(**kwargs: Any) -> AppWorldTarget:
    return AppWorldTarget(**kwargs)
