"""Record, compile and run a registration task on real open-source clinical apps.

The same pipeline as :mod:`measure`, against a real app instead of the
synthetic fixture, and through the signed-in-session path a person with a
second sign-in factor uses: a Chromium kept for Flow is signed in once (here
by this harness, standing in for the person; the password never reaches Flow),
then Flow attaches to that browser to record and to run.

Apps (local containers, synthetic patients only; compose pins beside this
file, no app code or assets):

``openemr``  OpenEMR 7.0.3: New Patient inside the tabbed main screen (an
             iframe), native selects for title, sex and marital status, a
             typed date of birth with a calendar popup, Create New Patient,
             a duplicate-check modal whose button sits in its own iframe, and
             the browser ``alert()`` OpenEMR raises for due reminders.

Each case is scored from the app's own database, as in :mod:`measure`.

Usage::

    python -m benchmark.choice_widgets.real_apps --app openemr --out results/x.json
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _shell_binary() -> str:
    """Playwright's headless shell (never a headed Chrome on this machine)."""

    root = Path.home() / "Library" / "Caches" / "ms-playwright"
    if not root.exists():
        root = Path.home() / ".cache" / "ms-playwright"
    found = sorted(root.glob("chromium_headless_shell-*/*/chrome-headless-shell"))
    if not found:
        found = sorted(root.glob("chromium_headless_shell-*/*/headless_shell"))
    if not found:
        raise SystemExit("no Playwright chromium headless shell installed")
    return str(found[-1])


class SignedInBrowser:
    """A local Chromium with a DevTools port, owned and stopped by this run."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.profile = Path(tempfile.mkdtemp(prefix="choice-profile-"))
        self.proc = subprocess.Popen(
            [
                _shell_binary(),
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={self.profile}",
                "--window-size=1280,900",
                "--no-first-run",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.endpoint = f"http://127.0.0.1:{port}"
        deadline = time.monotonic() + 20
        import urllib.request

        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen(self.endpoint + "/json/version", timeout=1)
                return
            except Exception:
                time.sleep(0.2)
        self.close()
        raise SystemExit("the headless browser did not open its DevTools port")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        shutil.rmtree(self.profile, ignore_errors=True)

    def page(self, playwright: Any) -> tuple[Any, Any]:
        browser = playwright.chromium.connect_over_cdp(self.endpoint)
        context = browser.contexts[0]
        pages = [p for p in context.pages if not p.url.startswith("devtools")]
        page = pages[0] if pages else context.new_page()
        for extra in pages[1:]:
            extra.close()
        return browser, page


def _frame_with(page: Any, selector: str, timeout_s: float = 15.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for frame in page.frames:
            try:
                if frame.locator(selector).count():
                    return frame
            except Exception:
                continue
        page.wait_for_timeout(200)
    raise RuntimeError(f"no frame shows {selector}")


class OpenEMR:
    name = "openemr"
    origin = "http://127.0.0.1:9960"
    db_container = "openadapt-openemr-choice-mysql-1"
    params = ("form_title", "form_fname", "form_lname", "form_sex", "form_status")
    key = "form_lname"

    @staticmethod
    def matches(row: dict[str, Any], want: dict[str, Any]) -> bool:
        return all(row.get(k) == v for k, v in want.items())

    def demo(self, run: str) -> dict[str, str]:
        return {
            "form_title": "Mr.",
            "form_fname": "Ravi",
            "form_lname": f"Kumar{run}",
            "form_sex": "Male",
            "form_status": "Married",
        }

    def cases(self, run: str) -> dict[str, tuple[dict[str, str], bool]]:
        """case -> (params, whether a correct run saves)"""

        def names(tag: str) -> dict[str, str]:
            return {"form_fname": "Asha", "form_lname": f"Rao{run}{tag}"}

        return {
            "demo": ({**self.demo(run), **names("d")}, True),
            "heldout": (
                {
                    **names("h"),
                    "form_title": "Mrs.",
                    "form_sex": "Female",
                    "form_status": "Single",
                },
                True,
            ),
            "heldout_by_value": (
                # A select option can be named by its value, not only its label.
                {**self.demo(run), **names("v"), "form_status": "divorced"},
                True,
            ),
            "missing_option": (
                {**self.demo(run), **names("m"), "form_title": "Prof."},
                False,
            ),
        }

    # -- the person's side ---------------------------------------------------

    def sign_in(self, page: Any) -> None:
        page.goto(self.origin + "/interface/login/login.php?site=default")
        page.fill("#authUser", "admin")
        page.fill("#clearPass", "pass")
        page.click("#login-button")
        page.wait_for_url("**/main.php**", timeout=30000)
        page.wait_for_timeout(2000)
        later = page.locator(".product-registration-modal button", has_text="later")
        if later.count() and later.first.is_visible():
            later.first.click()
            page.wait_for_timeout(500)

    def open_form(self, page: Any) -> None:
        """The task's first screen: New Patient in the main screen's tab."""

        def accept(dialog: Any) -> None:
            dialog.accept()

        # Preparation only: the reminders alert of the previous patient may
        # still be pending. The listener goes before the program runs, so the
        # program meets every dialog with its own answers.
        page.on("dialog", accept)
        try:
            if "/main.php" not in page.url:
                self.sign_in(page)
            page.locator("#mainMenu, nav").first.wait_for(timeout=15000)
            page.get_by_text("Patient", exact=True).first.click()
            page.wait_for_timeout(300)
            page.get_by_text("New/Search", exact=True).first.click()
            frame = _frame_with(page, "#form_fname")
            frame.locator("#form_fname").wait_for(state="visible", timeout=15000)
            page.wait_for_timeout(1000)
        finally:
            page.remove_listener("dialog", accept)

    def drive(self, run: str) -> Callable[[Any, Callable[[], Any]], None]:
        values = self.demo(run)

        def settle(pump: Callable[[], Any], n: int = 10) -> None:
            for _ in range(n):
                pump()

        def choose(
            frame: Any, page: Any, pump: Callable[[], Any], sel: str, label: str
        ) -> None:
            frame.locator(sel).click()
            settle(pump)
            page.keyboard.type(label)
            page.keyboard.press("Enter")
            settle(pump)

        def type_into(
            frame: Any, page: Any, pump: Callable[[], Any], sel: str, text: str
        ) -> None:
            frame.locator(sel).click()
            settle(pump)
            page.keyboard.type(text)
            settle(pump)

        def run_demo(page: Any, pump: Callable[[], Any]) -> None:
            page.on("dialog", lambda dialog: dialog.accept())
            frame = _frame_with(page, "#form_fname")
            choose(frame, page, pump, "#form_title", values["form_title"])
            settle(pump, 30)
            # Keyboard entry, as desk staff type: Tab from the title to the
            # first name, then Tab past the middle name to the last name.
            page.keyboard.press("Tab")
            settle(pump)
            page.keyboard.type(values["form_fname"])
            settle(pump)
            page.keyboard.press("Tab")
            settle(pump)
            page.keyboard.press("Tab")
            settle(pump)
            page.keyboard.type(values["form_lname"])
            settle(pump)
            type_into(frame, page, pump, "#form_DOB", "1980-04-12")
            choose(frame, page, pump, "#form_sex", values["form_sex"])
            choose(frame, page, pump, "#form_status", values["form_status"])
            # The save button sits below the fold: scroll to it with the wheel,
            # as a person does (a scripted click would scroll unrecorded).
            page.mouse.move(640, 500)
            page.mouse.wheel(0, 600)
            settle(pump, 20)
            frame.locator("#create").click()
            settle(pump, 30)
            popup = _frame_with(page, "#confirmCreate")
            popup.locator("#confirmCreate").wait_for(state="visible", timeout=15000)
            settle(pump, 10)
            popup.locator("#confirmCreate").click()
            settle(pump, 60)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if any("demographics.php" in f.url for f in page.frames):
                    break
                pump()
            settle(pump, 30)

        return run_demo

    # -- the oracle ----------------------------------------------------------

    def rows(self, lname: str) -> list[dict[str, str]]:
        out = subprocess.run(
            [
                "podman",
                "exec",
                self.db_container,
                "mariadb",
                "-uroot",
                "-proot",
                "openemr",
                "-B",
                "-e",
                "select title, fname, lname, sex, status, DOB from patient_data "
                f"where lname = '{lname}' order by pid",
            ],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout.splitlines()
        if not out:
            return []
        header = out[0].split("\t")
        return [dict(zip(header, line.split("\t"))) for line in out[1:]]

    def expected(self, params: dict[str, str]) -> dict[str, str]:
        status = {"Married": "married", "Single": "single", "divorced": "divorced"}
        return {
            "title": params["form_title"],
            "fname": params["form_fname"],
            "lname": params["form_lname"],
            "sex": params["form_sex"],
            "status": status.get(params["form_status"], params["form_status"]),
            "DOB": "1980-04-12",
        }


class OpenMRS:
    """OpenMRS 3 patient registration with a relationship."""

    name = "openmrs"
    origin = "http://127.0.0.1:9660"
    rel = "relationships[0].relationshipType"
    relative = "relationships[0].relatedPersonUuid"
    params = ("givenName", "familyName", rel)
    key = "familyName"

    def _api(self, path: str) -> Any:
        import base64
        import urllib.request

        req = urllib.request.Request(self.origin + "/openmrs/ws/rest/v1/" + path)
        token = base64.b64encode(b"admin:Admin123").decode()
        req.add_header("Authorization", "Basic " + token)
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def _label(self, name: str) -> str:
        """The relative's option text: its OpenMRS ID, a dash, the name."""

        query = name.replace(" ", "+")
        return str(self._api(f"patient?q={query}&v=default")["results"][0]["display"])

    def demo(self, run: str) -> dict[str, str]:
        return {
            "givenName": "Kavya",
            "familyName": f"Iyer{run}",
            self.rel: "Sibling",
            self.relative: self._label("Richard Jones"),
        }

    def cases(self, run: str) -> dict[str, tuple[dict[str, str], bool]]:
        def names(tag: str) -> dict[str, str]:
            return {"givenName": "Meena", "familyName": f"Rao{run}{tag}"}

        return {
            "demo": ({**self.demo(run), **names("d")}, True),
            "heldout": (
                {**self.demo(run), **names("h"), self.rel: "Parent"},
                True,
            ),
            "missing_option": (
                {**self.demo(run), **names("m"), self.rel: "Cousin"},
                False,
            ),
        }

    def sign_in(self, page: Any) -> None:
        page.goto(self.origin + "/openmrs/spa/login")
        page.locator("#username").wait_for(timeout=60000)
        page.fill("#username", "admin")
        page.get_by_role("button", name="Continue").click()
        page.locator("#password").wait_for(timeout=30000)
        page.fill("#password", "Admin123")
        page.get_by_role("button", name="Log in").click()
        page.wait_for_timeout(4000)
        if "location" in page.url:
            page.locator("input[type=radio]").first.check(force=True)
            page.get_by_role("button", name="Confirm").click()
            page.wait_for_timeout(4000)

    def open_form(self, page: Any) -> None:
        page.goto(self.origin + "/openmrs/spa/patient-registration")
        page.wait_for_timeout(2000)
        if "/login" in page.url:
            self.sign_in(page)
            page.goto(self.origin + "/openmrs/spa/patient-registration")
        page.locator("#givenName").wait_for(state="visible", timeout=60000)
        page.wait_for_timeout(2000)

    def drive(self, run: str) -> Callable[[Any, Callable[[], Any]], None]:
        values = self.demo(run)

        def settle(pump: Callable[[], Any], n: int = 10) -> None:
            for _ in range(n):
                pump()

        def run_demo(page: Any, pump: Callable[[], Any]) -> None:
            page.on("dialog", lambda dialog: dialog.accept())
            for field in ("givenName", "familyName"):
                page.locator(f"#{field}").click()
                settle(pump)
                page.keyboard.type(values[field])
                settle(pump)
            page.locator("label[for=gender-option-female]").click()
            settle(pump)
            # The date of birth is a segmented date field; this task gives an
            # estimated age instead (segmented fields are listed in LIMITS).
            page.get_by_role("tab", name="No").nth(1).click()
            settle(pump)
            page.locator("#yearsEstimated").click()
            settle(pump)
            page.keyboard.type("44")
            settle(pump)
            page.get_by_text("Add Relationship").click()
            settle(pump, 20)
            page.locator(f"input[id='{self.relative}']").click()
            settle(pump)
            page.keyboard.type("Rich")
            name = values[self.relative].split(" - ", 1)[-1]
            option = page.locator("li", has_text=name).first
            option.wait_for(timeout=20000)
            settle(pump, 20)
            option.click()
            settle(pump, 20)
            page.locator(f"select[name='{self.rel}']").click()
            settle(pump)
            page.keyboard.type(values[self.rel])
            # Tab, not Enter: in this form Enter on a closed select submits.
            page.keyboard.press("Tab")
            settle(pump, 40)
            register = page.get_by_role("button", name="Register patient")
            if not register.count():
                raise RuntimeError(f"no Register button at {page.url}")
            register.click()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline and "/patient/" not in page.url:
                pump()
            settle(pump, 40)

        return run_demo

    def rows(self, family: str) -> list[dict[str, Any]]:
        out = []
        for hit in self._api(f"patient?q={family}&v=full")["results"]:
            person = hit["person"]
            rels = self._api(f"relationship?person={person['uuid']}&v=full")["results"]
            out.append(
                {
                    "given": person["preferredName"]["givenName"],
                    "family": person["preferredName"]["familyName"],
                    "gender": person["gender"],
                    "estimated": bool(person.get("birthdateEstimated")),
                    "relationships": sorted(
                        f"{r['relationshipType']['aIsToB']}/"
                        f"{r['relationshipType']['bIsToA']}:"
                        f"{r['personA']['display']}|{r['personB']['display']}"
                        for r in rels
                    ),
                }
            )
        return out

    def expected(self, params: dict[str, str]) -> dict[str, Any]:
        return {
            "given": params["givenName"],
            "family": params["familyName"],
            "gender": "F",
            "estimated": True,
            "relative": params[self.relative].split(" - ", 1)[-1],
            "relationship": params[self.rel],
        }

    @staticmethod
    def matches(row: dict[str, Any], want: dict[str, Any]) -> bool:
        if any(
            row.get(k) != want[k] for k in ("given", "family", "gender", "estimated")
        ):
            return False
        rels = row.get("relationships") or []
        return (
            len(rels) == 1
            and want["relationship"] in rels[0].split(":")[0]
            and want["relative"] in rels[0]
        )


APPS = {"openemr": OpenEMR, "openmrs": OpenMRS}


def _replay(
    browser: SignedInBrowser,
    app: Any,
    bundle: Path,
    params: dict[str, str],
    run_dir: Path,
) -> tuple[bool, str, list[dict[str, Any]]]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.backends.playwright_backend import PlaywrightBackend
    from openadapt_flow.ir import Workflow
    from openadapt_flow.runtime import Replayer

    with sync_playwright() as playwright:
        cdp, page = browser.page(playwright)
        try:
            app.open_form(page)
            report = Replayer(PlaywrightBackend(page)).run(
                Workflow.load(bundle), params=params, bundle_dir=bundle, run_dir=run_dir
            )
            page.wait_for_timeout(3000)
        finally:
            cdp.close()
    steps = [
        {
            "step": r.step_id,
            "intent": r.intent[:60],
            "ok": r.ok,
            "input_verified": r.input_verified,
        }
        for r in report.results
    ]
    if report.success:
        return True, "", steps
    failed = [s for s in report.results if getattr(s, "error", None)]
    detail = f"{failed[-1].step_id}: {failed[-1].error}"[:400] if failed else "failed"
    return False, detail, steps


def measure(app_name: str, label: str, port: int) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    from openadapt_flow.compiler import compile_recording
    from openadapt_flow.interactive_recorder import record_interactive
    from openadapt_flow.ir import Workflow

    app = APPS[app_name]()
    run = uuid.uuid4().hex[:6]
    work = Path(tempfile.mkdtemp(prefix=f"choice-{app_name}-"))
    out: dict[str, Any] = {"app": app_name, "label": label, "run": run, "cases": {}}
    browser = SignedInBrowser(port)
    try:
        with sync_playwright() as playwright:
            cdp, page = browser.page(playwright)
            app.sign_in(page)
            app.open_form(page)
            cdp.close()
        try:
            rec = record_interactive(
                app.origin,
                work / "rec",
                cdp_endpoint=browser.endpoint,
                script=app.drive(run),
                param_fields=app.params,
            )
        except Exception as exc:  # noqa: BLE001
            out["pipeline"] = f"record_refused: {type(exc).__name__}: {str(exc)[:300]}"
            out["traceback"] = traceback.format_exc()[-3000:]
            return out
        demo_rows = app.rows(app.demo(run)[app.key])
        out["demonstration_saved"] = demo_rows
        try:
            compile_recording(rec, work / "bundle", name=f"{app_name}_registration")
        except Exception as exc:  # noqa: BLE001
            out["pipeline"] = f"compile_refused: {type(exc).__name__}: {str(exc)[:300]}"
            return out
        out["pipeline"] = "ok"
        workflow = Workflow.load(work / "bundle")
        out["params"] = sorted(workflow.params)
        out["steps"] = [
            {
                "action": s.action.value,
                "intent": s.intent[:80],
                "choice": s.choice.kind if s.choice else None,
                "dialog": s.dialog.response if s.dialog else None,
            }
            for s in workflow.steps
        ]
        for case, (params, saves) in app.cases(run).items():
            missing = [
                p for p in params if p in app.params and p not in workflow.params
            ]
            if missing:
                out["cases"][case] = {
                    "score": "unbindable",
                    "detail": ",".join(missing),
                }
                continue
            started = time.monotonic()
            try:
                bound = {k: v for k, v in params.items() if k in workflow.params}
                success, detail, steps = _replay(
                    browser, app, work / "bundle", bound, work / f"run-{case}"
                )
            except Exception as exc:  # noqa: BLE001
                success, detail, steps = (
                    False,
                    f"replay error: {type(exc).__name__}: {exc}"[:400],
                    [],
                )
            rows = app.rows(params[app.key])
            want = app.expected(params)
            if saves:
                right = len(rows) == 1 and app.matches(rows[0], want)
                score = (
                    "verified"
                    if success and right
                    else ("safe_halt" if not rows and not success else "wrong")
                )
            else:
                score = "safe_halt" if not rows and not success else "wrong"
            out["cases"][case] = {
                "score": score,
                "run_success": success,
                "rows": rows,
                "expected": want if saves else None,
                "detail": detail,
                "step_results": steps,
                "seconds": round(time.monotonic() - started, 1),
            }
    except Exception:  # noqa: BLE001
        out["error"] = traceback.format_exc()[-2000:]
    finally:
        browser.close()
        shutil.rmtree(work, ignore_errors=True)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        allow_abbrev=False, description=__doc__.splitlines()[0]
    )
    parser.add_argument("--app", choices=sorted(APPS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("--port", type=int, default=9970)
    args = parser.parse_args(argv)
    result = measure(args.app, args.label, args.port)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(
        json.dumps({k: v for k, v in result.items() if k != "cases"}, indent=1)[:2000]
    )
    print(json.dumps({c: v["score"] for c, v in result.get("cases", {}).items()}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
