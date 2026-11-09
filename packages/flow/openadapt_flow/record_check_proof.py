"""Run a proposed record check end to end against a fault-injection fixture.

:mod:`openadapt_flow.record_check_synthesis` judges a proposed check on bad
writes built from the recorded records. This module goes further on the two
fixtures that inject bad writes at the persistence boundary:

- ``mockmed``: the MockMed fault server (:mod:`openadapt_flow.mockmed.fault_server`,
  the ``--break-it`` fixture), one in-memory store read at ``/api/db``;
- ``effect-e2e``: the SQLite record service of ``benchmark/effect_e2e`` (repo
  only), with an encounters table, a billing table the REST read does not see,
  and the app's own save banner.

For each fault, the save goes through the real
:class:`~openadapt_flow.runtime.replayer.Replayer` and
:class:`~openadapt_flow.runtime.actuators.ApiActuator`, the proposed check is
the replayer's effect verifier, and the fixture's own ground truth (which never
sees the check) says whether the save was right. The check passes only when
every wrong save stops the run and the clean save is reported as done.

Each run uses new values for free-text parameters, so a check that froze the
demonstration's values fails the clean save. No model calls, localhost only.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

from openadapt_flow.runtime.effects.effect import Effect

#: The write is synchronous through the API actuator, so a short poll is not
#: a source of flakiness; a refuted effect polls to this deadline.
PROOF_EFFECT_TIMEOUT_S = 0.3
ACTUATOR_TIMEOUT_S = 0.6


class ProofUnavailable(RuntimeError):
    """The named fixture is not available in this install."""


@dataclass
class Fixture:
    name: str
    #: ``() -> (origin, sqlite_path_or_None, stop, handle)``
    serve: Callable[[], tuple[str, Optional[Path], Callable[[], None], Any]]
    reset: Callable[[Any, bool], None]
    snapshot: Callable[[Any], Any]
    #: ``(before, after, run_params) -> (correct, fault_class)``
    judge: Callable[[Any, Any, Mapping[str, str]], tuple[bool, str]]
    #: ``(name, seed_concurrent)``; the first is the clean save.
    scenarios: tuple[tuple[str, bool], ...]
    write_path: str
    default_body: dict[str, str]


# -- MockMed -----------------------------------------------------------------


def _mockmed_serve() -> tuple[str, Optional[Path], Callable[[], None], Any]:
    from openadapt_flow.mockmed.fault_server import serve

    url, db, stop = serve()
    return url.rstrip("/"), None, stop, db


def _mockmed_judge(
    before: list[dict[str, Any]], after: list[dict[str, Any]], params: Mapping[str, str]
) -> tuple[bool, str]:
    patient = params.get("patient_id", "p1")
    note = params.get("note", "")
    before_ids = {r["id"] for r in before}
    after_ids = {r["id"] for r in after}
    if any(r["id"] not in after_ids for r in before):
        return False, "collateral_loss"
    new = [r for r in after if r["id"] not in before_ids]
    mine = [r for r in new if str(r.get("patient_id")) == patient]
    if not mine:
        return False, "wrong_record" if new else "absent"
    if len(mine) > 1:
        return False, "duplicate"
    if str(mine[0].get("note")) != note:
        return False, "partial"
    if len(new) > 1:
        return False, "collateral_write"
    return True, "correct"


MOCKMED = Fixture(
    name="mockmed",
    serve=_mockmed_serve,
    reset=lambda db, seed: db.reset(seed_concurrent=seed),
    snapshot=lambda db: [dict(r) for r in db.snapshot()["records"]],
    judge=_mockmed_judge,
    scenarios=(
        ("ok", False),
        ("no_persist", False),
        ("partial", False),
        ("wrong_record", False),
        ("stale", True),
        ("optimistic", False),
        ("session", False),
    ),
    write_path="/api/encounter",
    default_body={"patient_id": "{patient_id}", "type": "Triage", "note": "{note}"},
)


def fixture(name: str) -> Fixture:
    if name == "mockmed":
        return MOCKMED
    if name in ("effect-e2e", "effect_e2e"):
        try:
            from benchmark.effect_e2e.prove_check import FIXTURE
        except ImportError as exc:  # pragma: no cover - wheel installs
            raise ProofUnavailable(
                "the effect-e2e fixture lives in the repository "
                "(packages/flow/benchmark); run from a checkout"
            ) from exc
        return FIXTURE
    raise ProofUnavailable(f"unknown fault fixture {name!r} (mockmed, effect-e2e)")


# -- replayer fakes (the API tier never touches the screen) --------------------


def _tiny_png() -> bytes:
    raw = b"\x00\xff\xff\xff"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


_PNG = _tiny_png()


class _NoScreen:
    """Backend and vision stand-in; the API step never reads or acts on it."""

    viewport = (2, 2)

    def screenshot(self) -> bytes:
        return _PNG

    def wait_settled(self, backend: Any, **_kw: Any) -> bytes:
        return _PNG

    def text_present(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def pixels_changed(self, *_a: Any, **_kw: Any) -> bool:
        return True

    def phash_png(self, *_a: Any, **_kw: Any) -> str:
        return "aa"

    def phash_distance(self, *_a: Any, **_kw: Any) -> int:
        return 0

    def ocr(self, *_a: Any, **_kw: Any) -> list:
        return []

    def __getattr__(self, name: str) -> Any:
        if name.startswith("find_"):
            return lambda *_a, **_kw: None
        if name in ("click", "type_text", "press", "scroll"):
            return lambda *_a, **_kw: None
        raise AttributeError(name)


# -- rebinding the check to the fixture ---------------------------------------


def rebind(
    section: Mapping[str, Any], *, origin: str, sqlite: Optional[Path]
) -> dict[str, Any]:
    """The verifier section pointed at the live fixture instead of the
    recording-time addresses (same paths, queries and fields)."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            out = {}
            for key, value in node.items():
                if key == "base_url" and isinstance(value, str):
                    parts = urlsplit(value)
                    out[key] = origin + parts.path
                elif key == "sqlite_database":
                    if sqlite is None:
                        raise ProofUnavailable(
                            "the check reads a database this fixture does not have"
                        )
                    out[key] = str(sqlite)
                else:
                    out[key] = walk(value)
            return out
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(dict(section))


def body_template(
    write_requests: list[dict[str, Any]],
    params: Mapping[str, str],
    default: Mapping[str, str],
) -> dict[str, str]:
    """The save's JSON body with recorded values replaced by ``{param}``."""

    import json

    lookup = {value: name for name, value in params.items()}
    for line in write_requests:
        body = line.get("body")
        try:
            parsed = json.loads(body) if isinstance(body, str) else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict) and parsed:
            return {
                str(key): (
                    "{" + lookup[str(value)] + "}"
                    if str(value) in lookup
                    else str(value)
                )
                for key, value in parsed.items()
            }
    return dict(default)


def run_values(params: Mapping[str, str], scenario: str) -> dict[str, str]:
    """New values for free-text parameters; identifiers stay (they must exist)."""

    out = {}
    for name, value in params.items():
        if any(ch.isspace() for ch in value) or len(value) > 64:
            out[name] = f"{value} (proof {scenario})"
        else:
            out[name] = value
    return out


# -- the run -------------------------------------------------------------------


def prove_live(
    fixture_name: str,
    section: Mapping[str, Any],
    effects: list[Effect],
    params: Mapping[str, str],
    *,
    write_requests: Optional[list[dict[str, Any]]] = None,
    work_dir: Path,
) -> dict[str, Any]:
    """Run every fixture fault through the replayer with the proposed check."""

    from openadapt_flow.deployment import EffectsConfig, build_effect_verifier
    from openadapt_flow.ir import (
        ActionKind,
        ApiBinding,
        Step,
        Workflow,
    )
    from openadapt_flow.runtime.actuators import ApiActuator
    from openadapt_flow.runtime.replayer import Replayer

    fx = fixture(fixture_name)
    origin, sqlite_path, stop, handle = fx.serve()
    body = body_template(write_requests or [], params, fx.default_body)
    checked = [
        effect.model_copy(
            update={
                "needs_operator_confirmation": False,
                "timeout_s": PROOF_EFFECT_TIMEOUT_S,
            }
        )
        for effect in effects
    ]
    rows: list[dict[str, Any]] = []
    try:
        live_section = rebind(section, origin=origin, sqlite=sqlite_path)
        for index, (scenario, seed) in enumerate(fx.scenarios):
            values = run_values(params, scenario)
            fx.reset(handle, seed)
            before = fx.snapshot(handle)
            verifier = build_effect_verifier(
                EffectsConfig.model_validate(live_section), values
            )
            workflow = Workflow(
                name=f"record-check-proof-{scenario}",
                steps=[
                    Step(
                        id="save",
                        intent="save the record",
                        action=ActionKind.KEY,
                        key="Enter",
                        risk="irreversible",
                        effects=checked,
                        api_binding=ApiBinding(
                            method="POST",
                            url_template=f"{fx.write_path}?fault={scenario}",
                            body_template=body,
                            effects=[e.model_copy(deep=True) for e in checked],
                            timeout_s=ACTUATOR_TIMEOUT_S,
                        ),
                    )
                ],
                params=values,
            )
            run_dir = work_dir / f"{fx.name}-{index:02d}-{scenario}"
            (run_dir / "bundle" / "templates").mkdir(parents=True, exist_ok=True)
            screen = _NoScreen()
            report = Replayer(
                screen,
                vision=screen,
                effect_verifier=verifier,
                api_actuator=ApiActuator(origin, timeout_s=ACTUATOR_TIMEOUT_S),
                poll_interval_s=0.01,
            ).run(workflow, bundle_dir=run_dir / "bundle", run_dir=run_dir / "out")
            after = fx.snapshot(handle)
            correct, fault_class = fx.judge(before, after, values)
            step = report.results[0] if report.results else None
            effect_verified = getattr(step, "effect_verified", None)
            done = bool(report.success)
            if correct and done:
                result = "clean pass"
            elif correct and effect_verified is False:
                result = "false stop"
            elif correct:
                result = "stopped by the actuator (outcome unknown)"
            elif done:
                result = "MISSED"
            elif effect_verified is False:
                result = "caught by the check"
            else:
                result = "caught by the actuator"
            rows.append(
                {
                    "scenario": scenario,
                    "ground_truth": fault_class,
                    "reported_done": done,
                    "effect_verified": effect_verified,
                    "result": result,
                }
            )
    finally:
        stop()
    misses = [
        f"{row['scenario']}: {row['result']} (ground truth {row['ground_truth']})"
        for row in rows
        if row["result"] in ("MISSED", "false stop")
    ]
    clean = rows[0]["result"] == "clean pass" if rows else False
    if rows and not clean and not misses:
        misses.append(f"{rows[0]['scenario']}: the clean save did not finish")
    return {
        "fixture": fx.name,
        "runs": rows,
        "misses": misses,
        "passed": bool(rows) and clean and not misses,
    }
