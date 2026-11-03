"""The OpenAdapt arm: the certified referral program, run the way a customer runs it.

Every item is one ``openadapt-flow run`` (a subprocess, the CLI a customer
uses) in an already signed-in Chromium that the arm keeps open, the way a
clerk's browser stays signed in all day (``--browser-cdp-endpoint``). The
configuration is the production one as far as the CLI allows it here:

- durable runs (``--durable``): a stop pauses at the last verified step;
- per-item approval in ``problems_only`` mode;
- the REST record check from ``benchmark/openemr_local/verify.yaml`` (the
  read-only OAuth client) confirming every save;
- self-repair on, with the Claude bridge repair agent from
  ``benchmark/auto_repair`` (only when a model key file is given);
- the ``clinical-write`` certification policy.

What the CLI refused is recorded in ``describe()["refused_features"]``,
verbatim where the product printed it. Every person-facing step goes through
the product's own commands: ``item-approval show|approve|decline``,
``approve`` and ``resume``. The arm never reads the oracle.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .arm import Env, ItemResult, OperatorRequest, WorkItem

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]
DEFAULT_BUNDLE = Path.home() / "oa/scratch/quickstart-openemr/e2e-2/bundle"
CERTIFIED_DIGEST = "ec096afa2b614326b00ec3155f256041c19d86f3998761ce9d772cfd911460b6"
SIGN_IN_POLL_WAIT_S = 45.0
BACKGROUND_POLL = "execute_background_services.php"


def _cli() -> list[str]:
    return [sys.executable, "-m", "openadapt_flow"]


def _env(extra: Optional[dict[str, str]] = None) -> dict[str, str]:
    env = dict(os.environ)
    env["OPENADAPT_NO_LAUNCHER_CHECK"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(FLOW / "benchmark"), str(FLOW), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env.update(extra or {})
    return env


class _As:
    """The fixture, signed in as one of its users."""

    def __init__(self, fx: Any, user: str) -> None:
        self.ui_base_url = fx.ui_base_url
        self._values = {**fx._runtime_values(), "OPENEMR_ACTOR_USER": user}

    def _runtime_values(self) -> dict[str, str]:
        return dict(self._values)


class OpenAdaptArm:
    name = "openadapt"

    def __init__(self, options: dict[str, Any]) -> None:
        self.options = options
        self.bundle_src = Path(options.get("bundle") or DEFAULT_BUNDLE)
        self.cdp_port = int(options.get("cdp_port", 9425))
        self.key_file = options.get("repair_key_file")
        self.approval_wait = int(options.get("approval_wait_s", 600))
        self.item_timeout = float(options.get("item_timeout_s", 1500))
        self.user = options.get("user", "oa_clerk1")
        #: "governed": the Standard governed path the certified runs use
        #: (in-process Replayer with the OpenEMR backend). "cli": one
        #: ``openadapt-flow run`` per item, attached over CDP.
        self.mode = options.get("mode", "governed")
        self.refused: list[dict[str, str]] = []
        self._pw: Any = None
        self._browser: Any = None
        self.page: Any = None
        self.fx: Any = None

    # -- description -----------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        if self.mode == "governed":
            return {
                "kind": "openadapt",
                "mode": "governed",
                "surface": "the Standard governed run the certified referral runs use: "
                "run gate on the certified bundle under clinical-write, governed "
                "authorization built from it, durable, settled-state detection, the "
                "REST record check (verify.yaml), the OpenEMR browser backend "
                "(qualify.campaign._run)",
                "bundle_digest": CERTIFIED_DIGEST,
                "profile": "standard",
                "durable": True,
                "operator_surface": "the run's report.json; a stop is re-run from a "
                "fresh form after the operator checks the record",
                "refused_features": self.refused,
            }
        return {
            "kind": "openadapt",
            "mode": "cli",
            "surface": "openadapt-flow run (CLI subprocess per item), attached to a "
            "signed-in Chromium over CDP",
            "bundle_digest": CERTIFIED_DIGEST,
            "durable": True,
            "approval_mode": "problems_only",
            "policy": "clinical-write",
            "record_check": "REST, read-only OAuth client (verify.yaml)",
            "self_repair": bool(self.key_file),
            "repair_agent": "benchmark.auto_repair.measure_mockmed:BridgeRepairAgent "
            "(claude-sonnet-5-5)"
            if self.key_file
            else None,
            "refused_features": self.refused,
        }

    # -- browser -------------------------------------------------------------------

    def _launch(self) -> None:
        if self.mode == "governed":
            self.backend, self._close_backend = (
                self._demo().OpenEMRPlaywrightBackend.launch(
                    self.fx.ui_base_url, headless=True
                )
            )
            self.page = self.backend.page
            return
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=True, args=[f"--remote-debugging-port={self.cdp_port}"]
        )
        context = self._browser.new_context(viewport={"width": 1280, "height": 800})
        self.page = context.new_page()

    def _demo(self) -> Any:
        from benchmark.openemr_api_tier.common import load_demo

        return load_demo()

    def _signed_in(self) -> bool:
        try:
            return bool(
                self.page.evaluate(
                    "() => typeof window.navigateTab === 'function' && "
                    "!document.querySelector('#authUser')"
                )
            )
        except Exception:  # noqa: BLE001 - a crashed page is signed out
            return False

    def sign_in(self) -> None:
        demo = self._demo()
        polled = {"seen": False}

        def saw(req: Any) -> None:
            if BACKGROUND_POLL in req.url:
                polled["seen"] = True

        self.page.context.on("requestfinished", saw)
        backend = getattr(self, "backend", None) or _DemoBackend(self.page)
        demo._preauthenticate_browser(backend, _As(self.fx, self.user))
        deadline = time.monotonic() + SIGN_IN_POLL_WAIT_S
        while not polled["seen"] and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)
        self.page.wait_for_timeout(1000)

    def open_form(self) -> None:
        """A blank New Patient form in the tab shell, no chart open."""

        demo, page = self._demo(), self.page
        if page.evaluate(
            "() => !!(window.app_view_model && app_view_model.application_data.patient())"
        ):
            with page.expect_response(
                lambda r: "unset_session_ajax.php" in r.url, timeout=30_000
            ):
                page.evaluate("() => clearPatient(false)")
            page.wait_for_function(
                "() => !app_view_model.application_data.patient()", timeout=30_000
            )
        page.evaluate(
            "({path, name}) => { window.navigateTab(path, name); "
            "window.activateTabByName(name, true); }",
            {"path": demo.FORM_PATH, "name": demo.FORM_FRAME_NAME},
        )
        frame = demo._form_frame(page)
        frame.wait_for_load_state("load", timeout=60_000)
        frame.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        page.wait_for_timeout(500)

    def _ready(self) -> dict[str, Any]:
        """Signed in at a blank form. After a stop, or when the session has
        ended, the clerk starts again from the sign-in page (which also
        closes a duplicate-check dialog a stopped run left open); that is
        part of starting the next item, not a person's task."""

        note: dict[str, Any] = {}
        if self._signed_in() and not getattr(self, "_dirty", False):
            try:
                self.open_form()
                return note
            except Exception as exc:  # noqa: BLE001 - e.g. the session ended
                note["open_form_error"] = f"{type(exc).__name__}: {exc}"[:200]
        note["signed_in_again"] = True
        self._dirty = False
        self.page.goto(self.fx.ui_base_url)
        if not self._signed_in():
            self.sign_in()
        else:
            self.page.wait_for_timeout(1500)
        self.open_form()
        return note

    # -- setup ---------------------------------------------------------------------

    def setup(self, env: Env, operator: Any) -> list[dict[str, Any]]:
        from benchmark.lifecycle.apps import make_fixture

        self.env = env
        self.fx = make_fixture(Path(env.extra["fixture_state"]))
        self.work = env.workdir
        self.bundle = self.work / "bundle"
        if self.bundle.exists():
            shutil.rmtree(self.bundle)
        shutil.copytree(self.bundle_src, self.bundle)
        manifest = json.loads((self.bundle / "manifest.json").read_text())
        digest = manifest.get("content_digest")
        if digest != CERTIFIED_DIGEST:
            raise RuntimeError(f"bundle digest {digest} is not the certified program")
        if self.mode == "governed":
            return self._setup_governed()
        self._write_config()
        if not self.key_file:
            self.refused.append(
                {
                    "feature": "self_repair",
                    "reason": "no repair model key file was given to this run, so no repair "
                    "agent is configured (repair is on only with an agent)",
                }
            )
        self._try_seal()
        self._launch()
        self.page.goto(self.fx.ui_base_url)
        self.sign_in()
        self._probe_standard()
        # Pipelined read-back is refused under a durable run; recorded from the
        # compatibility matrix and confirmed by each report's read-back mode.
        from openadapt_flow.compatibility import cell

        c = cell("pipelined_readback", "durable")
        self.refused.append({"feature": "pipelined_readback", "reason": c.reason or ""})
        phases = self._setup_phases()
        return phases

    def _write_config(self) -> None:
        # The record check reads the REST API over the fixture's loopback HTTP
        # port: the HTTPS port's certificate is self-signed and the REST
        # verifier takes no CA option.
        api = self.fx.ui_base_url
        cfg = {
            "backend": {
                "kind": "web",
                "url": self.fx.ui_base_url,
                "browser_cdp_endpoint": f"http://127.0.0.1:{self.cdp_port}",
            },
            "effects": {
                "kind": "rest",
                "base_url": api,
                "records_path": "/apis/default/api/patient?_limit=1000",
                "records_key": "data",
                "auth": {"bearer_env": "SOR_BEARER_TOKEN"},
            },
            "runtime": {"approval_mode": "problems_only"},
        }
        if self.key_file:
            cfg["repair"] = {
                "agent": "benchmark.auto_repair.measure_mockmed:BridgeRepairAgent",
                "screen_access_by": "lifecycle-soak (synthetic data)",
                "model_provider": "anthropic",
                "model_key_file": str(self.key_file),
                "store": str(self.work / "repair-store"),
                "approval_wait_s": self.approval_wait,
            }
        import yaml

        self.config = self.work / "deploy.yaml"
        self.config.write_text(yaml.safe_dump(cfg, sort_keys=False))

    def _setup_governed(self) -> list[dict[str, Any]]:
        from openadapt_flow.compatibility import cell
        from openadapt_flow.ir import Workflow
        from openadapt_flow.qualify.campaign import _default_hooks, lookup_plan
        from openadapt_flow.qualify.config import load_verify_config

        config = load_verify_config(FLOW / "benchmark/openemr_local/verify.yaml")
        surface, structure = lookup_plan(config)
        self.gov = {
            "workflow": Workflow.load(self.bundle),
            "config": config,
            "hooks": _default_hooks(config, None),
            "lookup": {"use_structural": structure, "deployment_surface": surface},
        }
        for feature, reason in (
            (
                "per_item_approval",
                "the governed run path takes no approval gate (only "
                "`openadapt-flow run` wires one), so no save is put to a person",
            ),
            (
                "self_repair",
                "the governed run path wires no repair agent (only "
                "`openadapt-flow run` does)",
            ),
            ("pipelined_readback", cell("pipelined_readback", "durable").reason or ""),
            (
                "cli_run",
                "`openadapt-flow run` cannot run this program: with an attached "
                "signed-in browser every item halts at step_000 (structured identity of the "
                "title field not found by the generic web backend); without one the browser "
                "starts signed out. Measured in the CLI smoke run.",
            ),
            (
                "signed_admission",
                "not issued for this program yet; the run gate on the "
                "certified bundle authorizes each run, as in the certified referral runs",
            ),
        ):
            self.refused.append({"feature": feature, "reason": reason})
        self._launch()
        self.page.goto(self.fx.ui_base_url)
        self.sign_in()
        return self._setup_phases()

    def _run_governed(self, item: WorkItem, run_dir: Path, n: int) -> Any:
        import dataclasses

        from openadapt_flow.qualify.campaign import TrialContext, _run
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )

        g = self.gov
        hooks = dataclasses.replace(
            g["hooks"], open_session=lambda: (self.backend, lambda: None)
        )
        params = {k: str(v) for k, v in item.params.items()}
        ctx = TrialContext(
            case_id="lifecycle",
            expected="VERIFIED",
            oracle_kind="exact_record",
            trial=n,
            trial_dir=run_dir,
            run_dir=run_dir,
            run_id=f"lifecycle-{item.item_id}-{n}-{int(time.time())}",
            params=params,
            certified_run=True,
            state={"policy": "clinical-write"},
        )

        def authorize(_verifier: Any, gate_kwargs: dict[str, Any]) -> Any:
            gate = evaluate_run_gate(g["workflow"], **gate_kwargs)
            if not gate.passed:
                raise RuntimeError("run gate refused: " + gate.render()[-300:])
            return build_runtime_authorization(g["workflow"], gate, params=params)

        report, _ = _run(
            workflow=g["workflow"],
            bundle_dir=self.bundle,
            config=g["config"],
            hooks=hooks,
            ctx=ctx,
            setup=type("Setup", (), {"observer": None})(),  # type: ignore[arg-type]
            authorize=authorize,
            fault_driver=None,
            marks={"start": time.time()},
            lookup=g["lookup"],
        )
        return json.loads(report.model_dump_json())

    def _item_governed(self, item: WorkItem, operator: Any) -> ItemResult:
        t0 = time.time()
        idir = self.work / "items" / item.item_id
        idir.mkdir(parents=True, exist_ok=True)
        detail: dict[str, Any] = {"log": []}
        log = detail["log"]
        calls, cost, report, run_dir = 0, 0.0, {}, idir / "run-1"
        for n in (1, 2):
            run_dir = idir / f"run-{n}"
            try:
                detail.update(self._ready())
                report = self._run_governed(item, run_dir, n)
            except Exception as exc:  # noqa: BLE001 - a crash is a stop
                report = {
                    "execution_outcome": "ERROR",
                    "halt": {"reason": f"{type(exc).__name__}: {exc}"[:300]},
                }
            calls += int(report.get("model_calls") or 0)
            cost += float(report.get("est_model_cost_usd") or 0.0)
            tx = str(report.get("transaction_outcome") or "")
            if tx == "VERIFIED":
                return self._result(
                    item, "done", report, t0, calls, cost, detail, run_dir
                )
            self._dirty = True
            halt = report.get("halt") or {}
            unsure = tx == "RECONCILIATION_REQUIRED"
            reason = (
                f"{report.get('execution_outcome')}/{tx}: {halt.get('state_id', '')} "
                f"{str(halt.get('reason', ''))[:200]}"
            )
            decision = operator.ask(
                OperatorRequest(
                    kind="reconcile" if unsure else "stop",
                    item_id=item.item_id,
                    reason=reason.strip(),
                    surface="report.json; re-run from a fresh form",
                    detail={
                        "resumable": n == 1,
                        "transaction_outcome": tx,
                        "halt_step": halt.get("state_id"),
                    },
                )
            )
            log.append({"run": n, "stop": reason.strip(), "decision": decision.action})
            if decision.action != "resume":
                break
        return self._result(item, "stopped", report, t0, calls, cost, detail, run_dir)

    def _try_seal(self) -> None:
        """Encrypt the bundle at rest, as the default gated path requires.

        ``openadapt-flow seal`` with a fresh local key; when it refuses, the
        refusal is recorded verbatim and the run uses ``--allow-unencrypted``.
        """

        import base64
        import secrets

        src = self.work / "seal-source"
        dest = self.work / "sealed-bundle"
        for d in (src, dest):
            if d.exists():
                shutil.rmtree(d)
        shutil.copytree(self.bundle, src)
        # A benchmark sidecar Flow does not know; seal refuses unknown files.
        (src / "benchmark-contract.json").unlink(missing_ok=True)
        key = base64.b64encode(secrets.token_bytes(32)).decode()
        proc = subprocess.run(
            [*_cli(), "seal", str(src), "-o", str(dest)],
            capture_output=True,
            text=True,
            env=_env({"OPENADAPT_BUNDLE_KEY": key}),
            timeout=600,
        )
        out = (proc.stdout + proc.stderr).strip()
        (self.work / "seal-output.txt").write_text(out + "\n")
        if proc.returncode == 0 and (dest / "manifest.json").exists():
            self.bundle, self.bundle_key = dest, key
            self.unencrypted = False
        else:
            self.bundle_key, self.unencrypted = None, True
            line = next((x for x in out.splitlines() if "REFUSED" in x), out[-300:])
            self.refused.append(
                {
                    "feature": "encrypted_bundle",
                    "reason": line.strip() + " (the run uses --allow-unencrypted)",
                }
            )

    def _token_env(self) -> dict[str, str]:
        session = self.fx.token_session("oracle")
        token = session.headers["Authorization"].split(" ", 1)[1]
        session.close()
        env = {"SOR_BEARER_TOKEN": token}
        if getattr(self, "bundle_key", None):
            env["OPENADAPT_BUNDLE_KEY"] = self.bundle_key
        return env

    def _run_cmd(
        self,
        run_dir: Path,
        params: Path,
        *,
        profile: Optional[str] = None,
        dry_run: bool = False,
    ) -> list[str]:
        cmd = [
            *_cli(),
            "run",
            str(self.bundle),
            "--config",
            str(self.config),
            "--url",
            self.fx.ui_base_url,
            "--run-dir",
            str(run_dir),
            "--params-file",
            str(params),
            "--durable",
            "--approval-mode",
            "problems_only",
            "--approval-wait",
            str(self.approval_wait),
            "--policy",
            "clinical-write",
        ]
        if profile:
            cmd += ["--profile", profile]
        elif getattr(self, "unencrypted", False):
            cmd.append("--allow-unencrypted")
        if dry_run:
            cmd.append("--dry-run")
        return cmd

    def _probe_standard(self) -> None:
        """Ask the CLI for the Standard profile once and record its answer.

        A real run (a dry run stops before the admission check), pointed at a
        port where nothing listens, so it can write nothing even if admitted.
        """

        import yaml

        probe = self.work / "standard-probe"
        probe.mkdir(parents=True, exist_ok=True)
        params = probe / "params.json"
        params.write_text(json.dumps({"fname": "Probe"}))
        cfg = yaml.safe_load(self.config.read_text())
        cfg["backend"] = {"kind": "web", "url": "http://127.0.0.1:9439"}
        probe_cfg = probe / "deploy.yaml"
        probe_cfg.write_text(yaml.safe_dump(cfg, sort_keys=False))
        cmd = self._run_cmd(probe / "run", params, profile="standard")
        cmd[cmd.index("--config") + 1] = str(probe_cfg)
        cmd[cmd.index("--url") + 1] = "http://127.0.0.1:9439"
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_env(self._token_env()),
            timeout=600,
            cwd=self.work,
        )
        out = proc.stdout + proc.stderr
        (probe / "output.txt").write_text(out)
        m = re.search(r"[^\n]*signed qualification admission[^\n]*", out)
        self.refused.append(
            {
                "feature": "standard_profile",
                "reason": (
                    m.group(0).strip() if m else f"exit {proc.returncode}: {out[-300:]}"
                ),
            }
        )

    def _setup_phases(self) -> list[dict[str, Any]]:
        """Teach, compile, qualify and admit, from the certified program's own
        records (they were not re-run in this soak)."""

        phases: list[dict[str, Any]] = []
        demo = self.bundle_src.parent / "demonstration.jsonl"
        teach_s = None
        if demo.exists():
            ts = [
                json.loads(line).get("timestamp")
                for line in demo.read_text().splitlines()
                if line.strip()
            ]
            ts = [t for t in ts if isinstance(t, (int, float))]
            if len(ts) > 1:
                teach_s = max(ts) - min(ts)
        phases.append(
            {
                "phase": "teach",
                "wall_s": teach_s or 36.8,
                "attended_s": teach_s or 36.8,
                "model_calls": 0,
                "cost_usd": 0.0,
                "source": "the demonstration's recorded duration",
            }
        )
        phases.append(
            {
                "phase": "compile",
                "wall_s": 12.2,
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "source": "quickstart e2e-2 log (compile 12.2 s)",
            }
        )
        phases.append(
            {
                "phase": "qualify",
                "wall_s": 549.6,
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "source": "qual10 QUALIFICATION.md: 18 trials, 549.6 s, unattended",
            }
        )
        phases.append(
            {
                "phase": "admit",
                "wall_s": 0.0,
                "attended_s": 0.0,
                "model_calls": 0,
                "cost_usd": 0.0,
                "source": "fixed charge (cost model admit_s); the signed admission "
                "itself does not exist yet",
            }
        )
        return phases

    # -- one item ----------------------------------------------------------------------

    def _approvals(self, run_dir: Path) -> list[dict[str, Any]]:
        if not (run_dir / "item-approval").exists():
            return []
        proc = subprocess.run(
            [*_cli(), "item-approval", "show", str(run_dir)],
            capture_output=True,
            text=True,
            env=_env(),
            timeout=300,
        )
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout)
        except ValueError:
            return []
        if isinstance(data, dict):
            data = data.get("requests", [])
        return [r for r in data if isinstance(r, dict)]

    def _answer(
        self,
        item: WorkItem,
        run_dir: Path,
        req: dict[str, Any],
        operator: Any,
        log: list[dict[str, Any]],
    ) -> None:
        rid = req.get("request_id") or req.get("id")
        sha = req.get("request_sha256") or req.get("digest") or req.get("sha256")
        text = json.dumps(req)
        is_repair = "repair" in text.lower()
        values = {}
        for f in req.get("fields") or []:
            if isinstance(f, dict):
                key = f.get("param") or f.get("name") or f.get("label")
                if key:
                    values[str(key)] = str(f.get("value", ""))
        kind = "repair_review" if is_repair else "approval"
        decision = operator.ask(
            OperatorRequest(
                kind=kind,
                item_id=item.item_id,
                reason=str(
                    req.get("reason") or req.get("workflow_name") or "save approval"
                ),
                surface="cli:openadapt-flow item-approval",
                detail={
                    "values": values or dict(item.params),
                    "probes_passed": is_repair,
                    "request_id": rid,
                },
            )
        )
        verb = (
            "approve" if decision.action in ("approve", "accept_repair") else "decline"
        )
        cmd = [
            *_cli(),
            "item-approval",
            verb,
            str(run_dir),
            "--request-id",
            str(rid),
            "--request-sha256",
            str(sha),
            "--decided-by",
            "human",
            "--route",
            "desktop_run_it",
            "--unauthenticated",
        ]
        if verb == "decline":
            cmd += ["--reason", "wrong_values"]
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=_env(), timeout=300
        )
        log.append(
            {
                "answered": rid,
                "verb": verb,
                "kind": kind,
                "exit": proc.returncode,
                "stderr": proc.stderr[-300:],
            }
        )

    def _drive(
        self,
        cmd: list[str],
        run_dir: Path,
        item: WorkItem,
        operator: Any,
        log: list[dict[str, Any]],
    ) -> int:
        """Run one CLI command; answer every approval it asks for meanwhile."""

        out = open(run_dir.parent / f"{run_dir.name}-{len(log)}.log", "w")
        proc = subprocess.Popen(
            cmd,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=_env(self._token_env()),
            cwd=self.work,
        )
        answered: set[str] = set()
        deadline = time.monotonic() + self.item_timeout
        while proc.poll() is None:
            if time.monotonic() > deadline:
                proc.kill()
                log.append({"timeout": True})
                break
            for req in self._approvals(run_dir):
                rid = str(req.get("request_id") or req.get("id"))
                status = str(req.get("status", "")).lower()
                if rid in answered or status in ("answered", "consumed", "expired"):
                    continue
                answered.add(rid)
                self._answer(item, run_dir, req, operator, log)
            time.sleep(2.0)
        out.close()
        code = proc.wait()
        text = Path(out.name).read_text(errors="replace")
        for line in text.splitlines():
            if line.startswith("Auto-repair is off for this run:") and not any(
                r["feature"] == "self_repair" for r in self.refused
            ):
                self.refused.append({"feature": "self_repair", "reason": line.strip()})
        return code

    def _report(self, run_dir: Path) -> dict[str, Any]:
        path = run_dir / "report.json"
        if not path.exists():
            return {}
        try:
            return json.loads(path.read_text())
        except ValueError:
            return {}

    def run_item(self, item: WorkItem, env: Env, operator: Any) -> ItemResult:
        if self.mode == "governed":
            return self._item_governed(item, operator)
        t0 = time.time()
        idir = self.work / "items" / item.item_id
        idir.mkdir(parents=True, exist_ok=True)
        run_dir = idir / "run"
        params = idir / "params.json"
        params.write_text(json.dumps(item.params))
        params.chmod(0o600)
        detail: dict[str, Any] = {"log": []}
        log = detail["log"]
        try:
            detail.update(self._ready())
        except Exception as exc:  # noqa: BLE001 - the session could not be opened
            detail["session_error"] = f"{type(exc).__name__}: {exc}"[:300]
        self._drive(self._run_cmd(run_dir, params), run_dir, item, operator, log)
        report = self._report(run_dir)
        model_calls = int(report.get("model_calls") or 0)
        cost = float(report.get("est_model_cost_usd") or 0.0)

        for attempt in range(2):
            tx = str(report.get("transaction_outcome") or "")
            ex = str(report.get("execution_outcome") or "")
            if tx == "VERIFIED":
                return self._result(
                    item, "done", report, t0, model_calls, cost, detail, run_dir
                )
            esc = {}
            if (run_dir / "pending_escalation.json").exists():
                try:
                    esc = json.loads((run_dir / "pending_escalation.json").read_text())
                except ValueError:
                    esc = {}
            category = str(esc.get("category", ""))
            unsure = tx == "RECONCILIATION_REQUIRED" or category.startswith(
                ("delivery_uncertain", "effect_")
            )
            halt = report.get("halt") or {}
            reason = (
                f"{tx or ex or 'no report'}: {category} "
                f"{halt.get('state_id', '')} {str(halt.get('reason', ''))[:200]}"
            )
            decision = operator.ask(
                OperatorRequest(
                    kind="reconcile" if unsure else "stop",
                    item_id=item.item_id,
                    reason=reason.strip(),
                    surface="cli:openadapt-flow explain/approve/resume",
                    detail={
                        "resumable": bool(esc),
                        "category": category,
                        "transaction_outcome": tx,
                        "halt_step": halt.get("state_id"),
                    },
                )
            )
            log.append({"stop": reason.strip(), "decision": decision.action})
            if decision.action != "resume":
                return self._result(
                    item, "stopped", report, t0, model_calls, cost, detail, run_dir
                )
            # The person resolves the stop, approves, and resumes the run.
            try:
                self._ready_for_resume()
            except Exception as exc:  # noqa: BLE001
                detail["resume_session_error"] = f"{type(exc).__name__}: {exc}"[:300]
            subprocess.run(
                [
                    *_cli(),
                    "approve",
                    str(run_dir),
                    "--approver",
                    "operator",
                    "--resolution",
                    "checked the item and resumed",
                ],
                capture_output=True,
                text=True,
                env=_env(),
                timeout=300,
            )
            self._drive(
                [
                    *_cli(),
                    "resume",
                    str(run_dir),
                    "--config",
                    str(self.config),
                    "--url",
                    self.fx.ui_base_url,
                    "--params-file",
                    str(params),
                ],
                run_dir,
                item,
                operator,
                log,
            )
            report = self._report(run_dir)
            model_calls += int(report.get("model_calls") or 0)
            cost += float(report.get("est_model_cost_usd") or 0.0)
            log.append({"resumed": report.get("transaction_outcome")})
        return self._result(
            item, "stopped", report, t0, model_calls, cost, detail, run_dir
        )

    def _ready_for_resume(self) -> None:
        """Before a resume: if the session ended, sign in again and open the
        form; otherwise leave the page where the run paused."""

        if not self._signed_in():
            self.page.goto(self.fx.ui_base_url)
            self.sign_in()
            self.open_form()

    def _result(
        self,
        item: WorkItem,
        claimed: str,
        report: dict[str, Any],
        t0: float,
        calls: int,
        cost: float,
        detail: dict[str, Any],
        run_dir: Path,
    ) -> ItemResult:
        halt = report.get("halt") or {}
        approval = report.get("approval") or {}
        detail.update(
            {
                "execution_outcome": report.get("execution_outcome"),
                "transaction_outcome": report.get("transaction_outcome"),
                "halt_step": halt.get("state_id"),
                "halt_reason": str(halt.get("reason", ""))[:300],
                "approval_mode": approval.get("mode"),
                "approval_decision": approval.get("decision"),
                "self_repair": report.get("self_repair"),
                "readback": report.get("readback"),
            }
        )
        status = " / ".join(
            str(x)
            for x in (
                report.get("execution_outcome"),
                report.get("transaction_outcome"),
            )
            if x
        )
        return ItemResult(
            item_id=item.item_id,
            claimed=claimed,  # type: ignore[arg-type]
            product_status=status or "no report",
            wall_s=time.time() - t0,
            model_calls=calls,
            cost_usd=cost,
            evidence=str(run_dir.relative_to(self.env.workdir.parent.parent)),
            detail=detail,
        )

    def after_drift(
        self, event: dict[str, Any], env: Env, operator: Any
    ) -> list[dict[str, Any]]:
        return []  # the product is not told about UI changes; it meets them

    def close(self) -> None:
        if getattr(self, "_close_backend", None):
            self._close_backend()
            return
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            if self._pw is not None:
                self._pw.stop()


class _DemoBackend:
    """The one attribute the demo's sign-in uses."""

    def __init__(self, page: Any) -> None:
        self.page = page


def make(options: dict[str, Any]) -> OpenAdaptArm:
    return OpenAdaptArm(options)
