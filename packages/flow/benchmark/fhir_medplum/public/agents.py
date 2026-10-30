"""Agents the benchmark can run. Plug yours in with ``--agent``.

Every agent gets the same three things and returns one claim:

- ``task``: ``{"id", "type", "resource_type", "instruction", "values"}``.
  The instruction is plain text (model.task_text); ``values`` holds the
  same values, structured. Both name people, not record ids.
- ``fhir_url``: the FHIR R4 base to use (the harness's proxy).
- ``token``: a bearer token for a plain member of the Medplum project.

The claim is ``{"status": "done" | "not_done", "id": "<Type/id or id>",
"note": "..."}``: done with the record's id when the record now exists as
the task asks (created or found), not_done otherwise. ``cost_usd`` is
optional.

Choices for ``--agent``:

- ``scripted``: a deterministic agent with no model; searches, then creates.
- ``scripted-fix``: the same, then reads the record back and fixes it once
  with a PUT if a value is wrong.
- ``claude``: Claude through the Claude Agent SDK (``claude-agent-sdk``),
  with built-in tools off (openadapt_flow.integrations.agents.claude_sdk.only)
  and one tool for FHIR requests. Auth: ANTHROPIC_API_KEY or a signed-in
  Claude Code.
- ``cmd:<command line>``: any program. The task JSON is on its stdin;
  OPENADAPT_BENCH_FHIR_URL and OPENADAPT_BENCH_TOKEN are in its
  environment; the last line of its stdout is the claim JSON.
- ``py:<module>:<function>``: ``function(task, fhir_url, token) -> claim``.
"""

from __future__ import annotations

import importlib
import json
import os
import shlex
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

AgentFn = Callable[[dict[str, Any], str, str], dict[str, Any]]

SYSTEM = (
    "You do clinical data entry on a FHIR R4 server, using the fhir_request "
    "tool. Read before you write, write each resource with its own request, "
    "and don't write anything the task doesn't ask for. When you're done, "
    "call finish once: status done with the record's id (Type/id) if the "
    "record now exists exactly as asked (created or already there), or "
    "status not_done if you couldn't finish."
)


def _request(
    fhir_url: str,
    token: str,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
) -> tuple[int, Any]:
    url = fhir_url.rstrip("/") + "/" + path.lstrip("/")
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/fhir+json"}
    if data is not None:
        headers["Content-Type"] = "application/fhir+json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:  # noqa: S310
            raw = resp.read()
            return resp.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            return exc.code, json.loads(raw) if raw else None
        except ValueError:
            return exc.code, raw.decode(errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)


# -- scripted: no model -------------------------------------------------------------


def _search(fhir_url: str, token: str, query: str) -> list[dict[str, Any]]:
    status, body = _request(fhir_url, token, "GET", query)
    if status != 200 or not isinstance(body, dict):
        raise RuntimeError(f"search {query} failed: HTTP {status}")
    return [e["resource"] for e in body.get("entry") or [] if "resource" in e]


def scripted_fix(task: dict[str, Any], fhir_url: str, token: str) -> dict[str, Any]:
    """:func:`scripted`, then read the record back and fix it once if wrong."""

    return scripted(task, fhir_url, token, fix=True)


def scripted(
    task: dict[str, Any], fhir_url: str, token: str, *, fix: bool = False
) -> dict[str, Any]:
    """Find the record or create it once, from the task's structured values.

    It is the careful agent a vendor would write by hand: it searches
    first, writes once, and never retries a failed write. It reads
    ``task["values"]`` (the harness provides them alongside the text).
    """

    from benchmark.fhir_medplum.public.model import (
        allergy_resource,
        appointment_resource,
        is_the_task,
        patient_resource,
        wrong_values,
    )

    kind, v = task["type"], task["values"]
    q = urllib.parse.quote
    try:
        want = dict(v)
        if kind == "patient":
            found = _search(
                fhir_url,
                token,
                f"Patient?family={q(v['family'])}&birthdate={v['birthDate']}",
            )
            body = patient_resource(v)
        else:
            person = v["person"]
            pats = _search(
                fhir_url,
                token,
                f"Patient?family={q(person['family'])}"
                f"&given={q(person['given'])}&birthdate={person['birthDate']}",
            )
            if len(pats) != 1:
                return {"status": "not_done", "note": f"{len(pats)} patients match"}
            want["patient_ref"] = f"Patient/{pats[0]['id']}"
            if kind == "allergy":
                found = _search(
                    fhir_url, token, f"AllergyIntolerance?patient={want['patient_ref']}"
                )
                body = allergy_resource(v, want["patient_ref"])
            else:
                doc = v["doctor"]
                docs = _search(
                    fhir_url,
                    token,
                    f"Practitioner?family={q(doc['family'])}&given={q(doc['given'])}",
                )
                if len(docs) != 1:
                    return {
                        "status": "not_done",
                        "note": f"{len(docs)} practitioners match",
                    }
                want["practitioner_ref"] = f"Practitioner/{docs[0]['id']}"
                found = _search(
                    fhir_url, token, f"Appointment?patient={want['patient_ref']}"
                )
                body = appointment_resource(
                    v, want["patient_ref"], want["practitioner_ref"]
                )
    except RuntimeError as exc:
        return {"status": "not_done", "note": str(exc)}
    found = [r for r in found if is_the_task(kind, r, want)]
    if len(found) == 1:
        return {
            "status": "done",
            "id": f"{found[0]['resourceType']}/{found[0]['id']}",
            "note": "already there",
        }
    if len(found) > 1:
        return {"status": "not_done", "note": f"{len(found)} records already match"}
    status, made = _request(fhir_url, token, "POST", body["resourceType"], body)
    if status in (200, 201) and isinstance(made, dict) and made.get("id"):
        ref = f"{made['resourceType']}/{made['id']}"
        if not fix:
            return {"status": "done", "id": ref, "note": "created"}
        status, now = _request(fhir_url, token, "GET", ref)
        if status != 200 or not isinstance(now, dict):
            return {
                "status": "not_done",
                "id": ref,
                "note": f"read back: HTTP {status}",
            }
        if not wrong_values(kind, now, want):
            return {"status": "done", "id": ref, "note": "created, read back"}
        status, _ = _request(fhir_url, token, "PUT", ref, {**body, "id": made["id"]})
        if status not in (200, 201):
            return {
                "status": "not_done",
                "id": ref,
                "note": f"fix failed: HTTP {status}",
            }
        status, now = _request(fhir_url, token, "GET", ref)
        if (
            status == 200
            and isinstance(now, dict)
            and not wrong_values(kind, now, want)
        ):
            return {"status": "done", "id": ref, "note": "created, fixed once"}
        return {"status": "not_done", "id": ref, "note": "still wrong after one fix"}
    return {"status": "not_done", "note": f"create failed: HTTP {status}"}


# -- claude: the Claude Agent SDK ---------------------------------------------------


def claude(
    model: str = "claude-sonnet-5-5",
    max_turns: int = 25,
    *,
    system: str = SYSTEM,
    effort: Optional[str] = None,
    max_budget_usd: Optional[float] = None,
) -> AgentFn:
    """``effort`` and ``max_budget_usd`` go to the Agent SDK when set."""

    extra: dict[str, Any] = {}
    if effort:
        extra["effort"] = effort
    if max_budget_usd:
        extra["max_budget_usd"] = max_budget_usd

    def run(task: dict[str, Any], fhir_url: str, token: str) -> dict[str, Any]:
        import anyio

        return anyio.run(
            _claude_once, task, fhir_url, token, model, max_turns, system, extra
        )

    return run


async def _claude_once(
    task: dict[str, Any],
    fhir_url: str,
    token: str,
    model: str,
    max_turns: int,
    system: str = SYSTEM,
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    from claude_agent_sdk import (
        ResultMessage,
        create_sdk_mcp_server,
        query,
        tool,
    )

    from openadapt_flow.integrations.agents.claude_sdk import only

    claim: dict[str, Any] = {}

    @tool(
        "fhir_request",
        "Send one HTTP request to the FHIR R4 server. path is relative to the "
        "FHIR base, e.g. 'Patient?family=Smith' or 'Patient/123'. body is a "
        "JSON resource as a string, or empty for GET and DELETE.",
        {"method": str, "path": str, "body": str},
    )
    async def fhir_request(args: dict[str, Any]) -> dict[str, Any]:
        method = str(args.get("method", "GET")).upper()
        raw = args.get("body") or ""
        try:
            body = json.loads(raw) if raw.strip() else None
        except ValueError as exc:
            return {
                "content": [{"type": "text", "text": f"body is not JSON: {exc}"}],
                "is_error": True,
            }
        status, reply = await anyio_to_thread(
            _request, fhir_url, token, method, str(args.get("path", "")), body
        )
        text = json.dumps(reply) if not isinstance(reply, str) else reply
        return {"content": [{"type": "text", "text": f"HTTP {status}\n{text[:20000]}"}]}

    @tool(
        "finish",
        "Report the outcome once, at the end. status is 'done' or 'not_done'; "
        "id is the record's Type/id when done.",
        {"status": str, "id": str, "note": str},
    )
    async def finish(args: dict[str, Any]) -> dict[str, Any]:
        claim.update({k: args.get(k) for k in ("status", "id", "note")})
        return {"content": [{"type": "text", "text": "recorded"}]}

    server = create_sdk_mcp_server("fhir", tools=[fhir_request, finish])
    options = only(
        {"fhir": server},
        system_prompt=system,
        model=model,
        max_turns=max_turns,
        setting_sources=[],
        **(extra or {}),
    )
    cost = 0.0
    async for message in query(prompt=task["instruction"], options=options):
        if isinstance(message, ResultMessage):
            cost = float(message.total_cost_usd or 0.0)
    if claim.get("status") not in ("done", "not_done"):
        claim = {"status": "not_done", "note": "the agent never called finish"}
    claim["cost_usd"] = cost
    return claim


async def anyio_to_thread(fn: Callable[..., Any], *args: Any) -> Any:
    import anyio

    return await anyio.to_thread.run_sync(lambda: fn(*args))


# -- your own agent -------------------------------------------------------------------


def command(cmdline: str, timeout_s: float = 600.0) -> AgentFn:
    def run(task: dict[str, Any], fhir_url: str, token: str) -> dict[str, Any]:
        public = {
            k: task[k] for k in ("id", "type", "resource_type", "instruction", "values")
        }
        env = {
            **os.environ,
            "OPENADAPT_BENCH_FHIR_URL": fhir_url,
            "OPENADAPT_BENCH_TOKEN": token,
        }
        try:
            proc = subprocess.run(
                shlex.split(cmdline),
                input=json.dumps(public),
                capture_output=True,
                text=True,
                env=env,
                timeout=timeout_s,
            )
        except subprocess.TimeoutExpired:
            return {"status": "not_done", "note": f"timed out after {timeout_s:.0f} s"}
        lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
        try:
            claim = json.loads(lines[-1])
        except (IndexError, ValueError):
            return {
                "status": "not_done",
                "note": f"no claim on stdout (exit {proc.returncode})",
            }
        return claim if isinstance(claim, dict) else {"status": "not_done"}

    return run


def load(spec: str, *, model: str) -> tuple[str, AgentFn]:
    """``--agent`` to (name, function)."""

    if spec == "scripted":
        return "scripted", scripted
    if spec == "scripted-fix":
        return "scripted-fix", scripted_fix
    if spec == "claude":
        return f"claude ({model})", claude(model)
    if spec.startswith("cmd:"):
        return spec, command(spec[4:])
    if spec.startswith("py:"):
        mod, _, fn = spec[3:].rpartition(":")
        return spec, getattr(importlib.import_module(mod), fn)
    raise SystemExit(
        f"unknown --agent {spec!r}: scripted, claude, cmd:..., or py:mod:fn"
    )
