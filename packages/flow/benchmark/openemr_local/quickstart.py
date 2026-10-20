"""OpenEMR for ``openadapt quickstart --app openemr`` (synthetic data only).

The quickstart's three acts on a real OpenEMR 8.0.0.3 instead of the bundled
MockMed. Everything here reuses the pinned local fixture and the certified
referral recipe:

- the fixture is :class:`benchmark.openemr_local.fixture.OpenEMRFixture`
  (pinned images, verified source files, hashed SQL baseline), with its own
  Compose project and ports so it never touches another copy, and its state
  in the user's cache folder so the next quickstart reuses it;
- Act 1 records the referral with the benchmark's own scripted recorder
  (network capture on) and compiles it the way the certified bundle was
  compiled: ``drop_redundant_keys`` and ``pixel_verify_enabled``, then
  ``compile_recording``'s fix-ups, including the shared duplicate rule
  (``apply_referral_rules``: confirm a new chart only when OpenEMR's
  duplicate check finds no match);
- Act 2 runs referrals on clerk accounts cloned from the fixture's user (the
  throughput harness's ``add_clerks``), one signed-in browser per clerk, each
  run checked by the engine's REST record check and read back here with SQL;
- Act 3 adds an existing chart and runs a referral that matches it. The
  program stops before saving, and SQL finds no new chart.

Every patient, address, phone number (555-01xx) and email
(``example.invalid``) is made up. OpenAdapt isn't affiliated with OpenEMR.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from . import fixture as fx

HERE = Path(__file__).resolve().parent
FLOW = HERE.parents[1]

#: The quickstart's own Compose project and loopback ports.
PROJECT = "openadapt-quickstart-openemr"
UI_PORT = "9741"
API_PORT = "9740"

#: Act 2's referrals and Act 3's possible duplicate: the referral timing
#: benchmark's synthetic patients, the same ones behind the site's Seals.
REFERRALS = (
    "r3-ines-carvalho",
    "r4-theo-lindqvist",
    "r5-grace-adeyemi",
    "r7-arlo-whitcombe",
    "r8-priya-raman",
)
DUPLICATE = "r6-rosa-delgado"

#: OpenEMR's tab shell polls this ten seconds after it loads, then every
#: minute; a session waits for the first poll before its first run.
BACKGROUND_POLL = "execute_background_services.php"
SIGN_IN_POLL_WAIT_S = 45.0

#: The fields the read-back compares (the referral's 12).
SQL_FIELDS = (
    "id",
    "title",
    "fname",
    "lname",
    "DOB",
    "sex",
    "street",
    "city",
    "state",
    "postal_code",
    "phone_home",
    "email",
    "country_code",
)


def default_state_dir() -> Path:
    """Where the quickstart keeps its fixture state between runs."""

    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "openadapt" / "quickstart-openemr"


class QuickstartFixture(fx.OpenEMRFixture):
    """The pinned fixture under its own project, ports and state folder."""

    def __init__(self, state_dir: Path | str, **kwargs: Any) -> None:
        super().__init__(state_dir, **kwargs)
        self.lock = {**self.lock, "project": PROJECT}

    @property
    def ui_base_url(self) -> str:
        return f"http://127.0.0.1:{UI_PORT}"

    @property
    def api_base_url(self) -> str:
        return f"https://127.0.0.1:{API_PORT}"

    def _expected_runtime(self) -> dict[str, str]:
        return {
            "OPENEMR_IMAGE": self.lock["services"]["openemr"],
            "MARIADB_IMAGE": self.lock["services"]["mariadb"],
            "OPENEMR_ACTOR_USER": "openadapt_actor",
            "OPENEMR_HTTP_PORT": UI_PORT,
            "OPENEMR_HTTPS_PORT": API_PORT,
        }

    def _validate_runtime_env(self) -> None:
        values = self._runtime_values()
        for name, value in self._expected_runtime().items():
            if values.get(name) != value:
                raise fx.FixtureError(f"runtime.env {name} differs from the lock")
        for name in (
            "MARIADB_ROOT_PASSWORD",
            "OPENEMR_DB_PASSWORD",
            "OPENEMR_ACTOR_PASSWORD",
        ):
            if not re.fullmatch(r"[A-Za-z0-9_-]{24,}", values.get(name, "")):
                raise fx.FixtureError(f"runtime.env {name} is absent or malformed")
        self.runtime_env.chmod(0o600)

    def prepare(self) -> None:
        """The base preparation, with this fixture's ports in runtime.env."""

        import secrets

        self._protect_state_dir()
        if not self.runtime_env.exists():
            values = {
                **self._expected_runtime(),
                "MARIADB_ROOT_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_DB_PASSWORD": secrets.token_urlsafe(32),
                "OPENEMR_ACTOR_PASSWORD": secrets.token_urlsafe(32),
            }
            self._write_private_exclusive(
                self.runtime_env,
                (
                    "# generated local synthetic fixture; never commit\n"
                    + "".join(f"{key}={value}\n" for key, value in values.items())
                ).encode(),
            )
        super().prepare()

    # -- state ------------------------------------------------------------

    def bootstrapped(self) -> bool:
        """True when a previous quickstart finished setting this fixture up."""

        return all(
            path.is_file()
            for path in (
                self.runtime_env,
                self.clients_path,
                self.snapshot_path,
                self.snapshot_hash_path,
            )
        )

    def running(self) -> bool:
        """True when both containers of this project are running."""

        try:
            out = self._compose(
                "ps", "--status", "running", "--services", timeout_s=30.0
            ).stdout.decode()
        except fx.FixtureError:
            return False
        return {"openemr", "db"} <= set(out.split())

    def healthy(self) -> bool:
        try:
            self._compose(
                "exec",
                "-T",
                "openemr",
                "/usr/bin/curl",
                "--fail",
                "--insecure",
                "--silent",
                "https://localhost/meta/health/readyz",
                timeout_s=15.0,
            )
        except fx.FixtureError:
            return False
        return True

    def discard(self) -> None:
        """Remove this project's containers and volumes and its state folder.

        Only for a setup that never finished (no baseline): nothing in it is
        anyone's work. Another project or state folder is never touched.
        """

        if self.runtime_env.exists():
            with contextlib.suppress(fx.FixtureError):
                self._compose("down", "--volumes", timeout_s=180.0)
        if self.state_dir.name == default_state_dir().name or os.environ.get(
            "OPENADAPT_QUICKSTART_OPENEMR_STATE"
        ):
            shutil.rmtree(self.state_dir, ignore_errors=True)


def make_fixture(state_dir: Optional[Path] = None) -> QuickstartFixture:
    state = state_dir or Path(
        os.environ.get("OPENADAPT_QUICKSTART_OPENEMR_STATE") or default_state_dir()
    )
    return QuickstartFixture(state)


# -- bring-up ---------------------------------------------------------------


#: What :func:`bring_up` reports for each phase: (phase, detail). Phases are
#: "pull", "start", "setup", "reset" and "ready".
Say = Callable[[str, str], None]


def _image_present(image: str, run: Callable[..., Any] = subprocess.run) -> bool:
    return (
        run(
            ["docker", "image", "inspect", image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def pull(
    image: str, label: str, say: Say, popen: Callable[..., Any] = subprocess.Popen
) -> None:
    """Pull one pinned image, reporting layers as Docker finishes them."""

    if _image_present(image):
        say("pull", f"{label}: already downloaded")
        return
    process = popen(
        ["docker", "pull", image],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    layers: set[str] = set()
    done: set[str] = set()
    assert process.stdout is not None
    for line in process.stdout:
        layer, _, status = line.strip().partition(": ")
        if not re.fullmatch(r"[0-9a-f]{12,64}", layer):
            continue
        layers.add(layer)
        if status.startswith(("Pull complete", "Already exists")):
            done.add(layer)
        say("pull", f"{label}: {len(done)} of {len(layers)} layers")
    if process.wait() != 0:
        raise fx.FixtureError(f"docker pull failed for {label}")


def bring_up(fixture: QuickstartFixture, say: Say) -> str:
    """Make the fixture ready at its verified baseline; return how.

    "reused": it was running; "restarted": it was set up but stopped;
    "created": it was set up now (downloads the images when they are absent,
    and OpenEMR installs itself on first start).
    """

    if fixture.bootstrapped() and fixture.running() and fixture.healthy():
        say("reset", "restoring the synthetic baseline")
        fixture.reset()
        return "reused"
    if fixture.bootstrapped():
        say("start", "starting the existing copy")
        fixture._compose("up", "-d", timeout_s=600.0)
        fixture._wait_ready(timeout_s=600.0)
        say("reset", "restoring the synthetic baseline")
        fixture.reset()
        return "restarted"
    if fixture.state_dir.exists():
        # A setup that stopped part-way; it holds no one's work.
        fixture.discard()
    fixture.runtime_preflight()
    say("setup", "checking the pinned OpenEMR release")
    fixture.prepare()
    pull(fixture.lock["services"]["openemr"], "OpenEMR 8.0.0.3", say)
    pull(fixture.lock["services"]["mariadb"], "MariaDB 11.8", say)
    fixture.image_identity()
    say("start", "OpenEMR installs itself on first start")
    fixture._compose("up", "-d", timeout_s=600.0)
    fixture._wait_ready(timeout_s=900.0)
    fixture._verify_running_source()
    say("setup", "adding synthetic API clients")
    fixture.bootstrap()
    fixture.snapshot()
    return "created"


# -- the referral program ---------------------------------------------------

#: The certified referral program: the bundle behind the site's OpenEMR
#: Seals (qualification qual10, clinical-write, 6 of 6 cases), and the
#: demonstration it was compiled from, without its URLs (they held that
#: session's sign-in token). See referral_program/README.md.
PROGRAM_DIR = HERE / "referral_program"
BUNDLE = PROGRAM_DIR / "program"
DEMONSTRATION = PROGRAM_DIR / "demonstration.jsonl"
POLICY = "clinical-write"


def load_demo() -> Any:
    """The OpenEMR reference harness (sign-in, form frame, backend)."""

    from benchmark.openemr_api_tier.common import load_demo as load

    return load()


def referral(key: str) -> Any:
    """The synthetic patient for one referral key."""

    from benchmark.openemr_api_tier.common import REFERRALS as ALL
    from benchmark.openemr_api_tier.common import spec_for

    return spec_for({r.key: r for r in ALL}[key])


def existing_chart() -> Any:
    from benchmark.openemr_api_tier.common import EXISTING_ROSA, Referral, spec_for

    return spec_for(Referral("existing", EXISTING_ROSA))


def demonstration() -> list[dict[str, Any]]:
    """The recorded actions the program was compiled from, in order."""

    return [
        json.loads(line)
        for line in DEMONSTRATION.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def point_qualify_hooks_here(fixture: QuickstartFixture) -> None:
    """Point verify.yaml's hooks (sign-in, read-back client) at this fixture.

    ``benchmark/openemr_local/verify.yaml`` reads these variables; the lock
    copy names this fixture's Compose project.
    """

    lock = fixture.state_dir / "environment.lock.json"
    text = json.dumps(fixture.lock, indent=2) + "\n"
    if not lock.is_file() or lock.read_text() != text:
        lock.write_text(text)
    os.environ["OPENEMR_FIXTURE_STATE"] = str(fixture.state_dir)
    os.environ["OPENEMR_FIXTURE_LOCK"] = str(lock)
    os.environ["OPENEMR_UI_URL"] = fixture.ui_base_url
    os.environ["OPENEMR_API_URL"] = fixture.api_base_url


def copy_program(bundle: Path) -> Any:
    """Copy the certified program to ``bundle``; load it (digest-checked)."""

    from openadapt_flow.ir import Workflow

    shutil.copytree(BUNDLE, bundle)
    return Workflow.load(bundle)


_ENV: dict[str, Any] = {}


def certified_env() -> dict[str, Any]:
    """verify.yaml's config, lookup plan and hooks, loaded once per process."""

    if not _ENV:
        from openadapt_flow.qualify.campaign import _default_hooks, lookup_plan
        from openadapt_flow.qualify.config import load_verify_config

        config = load_verify_config(HERE / "verify.yaml")
        surface, structure = lookup_plan(config)
        _ENV.update(
            config=config,
            hooks=_default_hooks(config, None),
            lookup={"use_structural": structure, "deployment_surface": surface},
        )
    return _ENV


def add_clerks(fixture: QuickstartFixture, n: int) -> list[str]:
    """``n`` synthetic clerk accounts, one per parallel browser."""

    from benchmark.openemr_throughput.harness import add_clerks as add

    return add(fixture, n)


def seed_existing_chart(fixture: QuickstartFixture) -> int:
    """Add the existing chart Act 3's referral matches; return the HTTP status."""

    session = fixture.token_session("actor")
    try:
        return session.post(
            f"{fixture.api_base_url}/apis/default/api/patient",
            json=dict(existing_chart().fields),
        ).status_code
    finally:
        session.close()


def charts(fixture: QuickstartFixture) -> list[dict[str, str]]:
    """Every chart in OpenEMR's database, read with SQL."""

    out = fixture._db(
        "SELECT " + ",".join(SQL_FIELDS) + " FROM openemr.patient_data ORDER BY id"
    ).decode()
    return [
        {k: ("" if v == "NULL" else v) for k, v in zip(SQL_FIELDS, line.split("\t"))}
        for line in out.splitlines()
    ]


def charts_for(fixture: QuickstartFixture, spec: Any) -> tuple[int, int]:
    """(charts with this referral's email, fields matching when exactly one)."""

    mine = [row for row in charts(fixture) if row.get("email") == spec.email]
    fields = dict(spec.fields)
    if len(mine) != 1:
        return len(mine), 0
    return 1, sum(1 for k, v in fields.items() if mine[0].get(k) == v)


class _As:
    """The fixture, signed in as another of its users (same password)."""

    def __init__(self, fixture: QuickstartFixture, user: str) -> None:
        self.ui_base_url = fixture.ui_base_url
        self._values = {**fixture._runtime_values(), "OPENEMR_ACTOR_USER": user}

    def _runtime_values(self) -> dict[str, str]:
        return dict(self._values)


class Session:
    """One browser signed in to OpenEMR as one user, at a blank New Patient form."""

    def __init__(
        self, fixture: QuickstartFixture, user: str, *, headless: bool
    ) -> None:
        self.fixture = fixture
        self.user = user
        self.demo = load_demo()
        self.backend, self._close = self.demo.OpenEMRPlaywrightBackend.launch(
            fixture.ui_base_url, headless=headless
        )
        self._signed_in = False
        self._polled = threading.Event()
        self.backend.page.context.on("requestfinished", self._saw_request)
        self.backend.page.context.on("requestfailed", self._saw_request)

    def _saw_request(self, request: Any) -> None:
        if BACKGROUND_POLL in request.url:
            self._polled.set()

    def sign_in(self) -> None:
        """Sign in and wait out the tab shell's first background poll.

        OpenEMR's tab shell posts ``execute_background_services.php`` ten
        seconds after it loads, then every minute. A run that starts inside
        that window meets an in-flight request at its first step, and on a
        busy computer the readiness check gives up before it ends. Waiting
        for the first poll (bounded), then for a quiet page, is part of
        signing in.
        """

        if self._signed_in:
            return
        self.demo._preauthenticate_browser(self.backend, _As(self.fixture, self.user))
        self._signed_in = True
        deadline = time.monotonic() + SIGN_IN_POLL_WAIT_S
        page = self.backend.page
        while not self._polled.is_set() and time.monotonic() < deadline:
            page.wait_for_timeout(250)
        while time.monotonic() < deadline:
            if self.backend.page_quiescent(0.5) is not False:
                return
            page.wait_for_timeout(250)

    def open_form(self) -> None:
        """Where the recording started: no chart open, a blank form."""

        demo, page = self.demo, self.backend.page
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
        try:
            frame = demo._form_frame(page, timeout_s=5.0)
        except fx.FixtureError:
            frame = None
        if frame is not None:
            fname = frame.locator("#form_fname")
            fname.wait_for(state="visible", timeout=60_000)
            if fname.input_value() == "":
                return
            # A run that stopped part-way left this form filled in: load it
            # again, inside the same tab shell.
            frame.evaluate("() => window.location.reload()")
            page.wait_for_timeout(500)
        else:
            page.evaluate(
                "({path, name}) => { window.navigateTab(path, name); "
                "window.activateTabByName(name, true); }",
                {"path": demo.FORM_PATH, "name": demo.FORM_FRAME_NAME},
            )
        frame = demo._form_frame(page)
        frame.locator("#form_fname").wait_for(state="visible", timeout=60_000)
        if frame.locator("#form_fname").input_value() != "":
            raise fx.FixtureError("the New Patient form was not blank")

    def run(
        self, workflow: Any, bundle: Path, key: str, run_dir: Path, *, trial: int = 1
    ) -> dict[str, Any]:
        """Run the program for one referral, as a certified run; return the facts.

        The certified referral runs' own path (``certified_referrals``): the
        Standard run gate on the certified bundle under ``clinical-write``,
        the governed authorization built from it, durable runs, settled-state
        detection, and verify.yaml's REST record check. The one difference is
        the session: this clerk's browser, already signed in. The facts come
        from the run's report and a SQL read of OpenEMR's database.
        """

        from openadapt_flow.plain_outcome import plain_result
        from openadapt_flow.qualify.campaign import TrialContext, _run
        from openadapt_flow.report import render_run_report
        from openadapt_flow.run_gate import (
            build_runtime_authorization,
            evaluate_run_gate,
        )

        spec = referral(key)
        params = {k: str(v) for k, v in spec.params().items()}
        started = time.monotonic()
        self.sign_in()
        self.open_form()
        env = certified_env()
        hooks = dataclasses.replace(
            env["hooks"], open_session=lambda: (self.backend, lambda: None)
        )
        ctx = TrialContext(
            case_id="quickstart",
            expected="VERIFIED",
            oracle_kind="exact_record",
            trial=trial,
            trial_dir=run_dir,
            run_dir=run_dir,
            run_id=f"quickstart-openemr-{key}-{int(time.time())}",
            params=params,
            certified_run=True,
            state={"policy": POLICY},
        )
        gate_seen: dict[str, Any] = {}

        def authorize(_verifier: Any, gate_kwargs: dict[str, Any]) -> Any:
            gate = evaluate_run_gate(workflow, **gate_kwargs)
            gate_seen["gate"] = gate
            if not gate.passed:
                raise fx.FixtureError(
                    "the Standard run gate refused the referral program:\n"
                    + gate.render()
                )
            return build_runtime_authorization(workflow, gate, params=params)

        report, _ = _run(
            workflow=workflow,
            bundle_dir=bundle,
            config=env["config"],
            hooks=hooks,
            ctx=ctx,
            setup=type("Setup", (), {"observer": None})(),  # type: ignore[arg-type]
            authorize=authorize,
            fault_driver=None,
            marks={"start": time.time()},
            lookup=env["lookup"],
        )
        with contextlib.suppress(Exception):
            render_run_report(run_dir)
        found, matched = charts_for(self.fixture, spec)
        envelope = getattr(report, "outcome_envelope", None)
        required = int(envelope.required_contracts.effect) if envelope else 0
        confirmed = int(envelope.passed_contracts.effect) if envelope else 0
        halt = getattr(report, "halt", None)
        return {
            "key": key,
            "name": f"{spec.fname} {spec.lname}",
            "dob": spec.dob,
            "run_dir": str(run_dir),
            "execution_outcome": str(report.execution_outcome),
            "transaction_outcome": report.transaction_outcome,
            "execution_profile": getattr(report, "execution_profile", None),
            "plain_label": plain_result(
                report.transaction_outcome, report.execution_outcome
            ).label,
            "model_calls": int(report.model_calls),
            "cost_usd": float(getattr(report, "est_model_cost_usd", 0.0) or 0.0),
            "effects_required": required,
            "effects_confirmed": confirmed,
            "charts": found,
            "fields_matched": matched,
            "fields_total": len(dict(spec.fields)),
            "halt_step": getattr(halt, "state_id", None) if halt else None,
            "halt_reason": str(getattr(halt, "reason", "") or "") if halt else "",
            "seconds": time.monotonic() - started,
        }

    def screenshot(self, path: Path) -> Optional[Path]:
        try:
            self.backend.page.screenshot(path=str(path))
        except Exception:  # noqa: BLE001 - the report still has the frame
            return None
        return path

    def close(self) -> None:
        self._close()


def checked(facts: Mapping[str, Any]) -> bool:
    """A referral the engine verified and SQL found exactly once, field for field."""

    return (
        facts["execution_outcome"] == "VERIFIED"
        and facts["transaction_outcome"] == "VERIFIED"
        and facts["effects_required"] > 0
        and facts["effects_confirmed"] == facts["effects_required"]
        and facts["charts"] == 1
        and facts["fields_matched"] == facts["fields_total"]
    )


def main() -> int:
    """Bring the quickstart's fixture up and print how (for timing it alone)."""

    started = time.monotonic()
    fixture = make_fixture()
    how = bring_up(
        fixture, lambda phase, detail: print(f"{phase}: {detail}", flush=True)
    )
    print(json.dumps({"how": how, "seconds": round(time.monotonic() - started, 1)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
