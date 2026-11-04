"""What both script arms share: the script contract, the sandbox, and the runner.

A script is a Python module that defines::

    def register(ctx, referral: dict) -> dict:
        # returns {"result": "SAVED" | "NOT SAVED", "reason": str}

``ctx`` gives the script exactly what the other arms get:

- ``ctx.page``: the Playwright page, already signed in, on a blank New Patient
  form; ``ctx.form``: that form's frame.
- ``ctx.find_patients(fname=, lname=, DOB=)``: the clerk's REST search.
- ``ctx.commit``: False in a dry run. In a dry run the save request
  (``new_comprehensive_save.php``) is blocked at the network layer, so a dry
  run cannot write, whatever the script does.
- ``ctx.log(msg)``: a line in the run's evidence.

The sandbox is a static check, not isolation: a script may import only
``re``, ``time``, ``datetime`` and ``json``, and may not name ``open``,
``exec``, ``eval``, ``compile``, ``__import__``, ``globals``, ``os``,
``sys`` or ``subprocess``. Scripts touch only the local synthetic fixture.
"""

from __future__ import annotations

import ast
import time
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable

ALLOWED_IMPORTS = {"re", "time", "datetime", "json"}
FORBIDDEN_NAMES = {
    "open", "exec", "eval", "compile", "__import__", "globals", "locals", "os", "sys",
    "subprocess", "importlib", "builtins", "__builtins__", "breakpoint", "input",
}
SAVE_URL_PART = "new_comprehensive_save.php"


class ScriptRejected(ValueError):
    """The script failed the static check."""


def check_source(source: str) -> None:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                if a.name.split(".")[0] not in ALLOWED_IMPORTS:
                    raise ScriptRejected(f"import {a.name} is not allowed")
        elif isinstance(node, ast.ImportFrom):
            if (node.module or "").split(".")[0] not in ALLOWED_IMPORTS:
                raise ScriptRejected(f"from {node.module} import is not allowed")
        elif isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            raise ScriptRejected(f"name {node.id} is not allowed")
        elif isinstance(node, ast.Attribute) and node.attr.startswith("__"):
            raise ScriptRejected(f"attribute {node.attr} is not allowed")
    if not any(isinstance(n, ast.FunctionDef) and n.name == "register" for n in tree.body):
        raise ScriptRejected("the script must define register(ctx, referral)")


def load(source: str) -> Callable[..., dict[str, Any]]:
    check_source(source)
    ns: dict[str, Any] = {"__name__": "lifecycle_script"}
    exec(compile(source, "<script>", "exec"), ns)  # noqa: S102 - checked above
    return ns["register"]


@dataclass
class Ctx:
    page: Any
    form: Any
    find_patients: Callable[..., list[dict[str, Any]]]
    commit: bool = True
    lines: list[str] = field(default_factory=list)

    def log(self, msg: str) -> None:
        self.lines.append(str(msg)[:500])


@dataclass
class RunOutcome:
    result: str  # SAVED | NOT SAVED | ERROR
    reason: str
    seconds: float
    log: list[str]
    blocked_saves: int = 0

    @property
    def broke(self) -> bool:
        """The script failed in a way that needs maintenance, not a business stop."""

        return self.result == "ERROR" or self.reason.upper().startswith("UNEXPECTED")


def run(source: str, session: Any, rest: Any, referral: dict[str, str], *, commit: bool) -> RunOutcome:
    """Run the script once on a fresh blank form."""

    session.blank_form()
    page = session.page
    blocked = {"n": 0}

    def block(route: Any) -> None:
        blocked["n"] += 1
        route.abort()

    if not commit:
        page.route(f"**/*{SAVE_URL_PART}*", block)
    ctx = Ctx(page=page, form=session.form_frame(), find_patients=rest.find, commit=commit)
    t0 = time.time()
    try:
        fn = load(source)
        out = fn(ctx, dict(referral))
        if not isinstance(out, dict) or out.get("result") not in ("SAVED", "NOT SAVED"):
            return RunOutcome("ERROR", f"bad return value: {out!r}"[:300], time.time() - t0, ctx.lines, blocked["n"])
        return RunOutcome(out["result"], str(out.get("reason", ""))[:500], time.time() - t0, ctx.lines, blocked["n"])
    except ScriptRejected as exc:
        return RunOutcome("ERROR", f"rejected: {exc}", time.time() - t0, ctx.lines, blocked["n"])
    except Exception as exc:  # noqa: BLE001 - a failing script is the data
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)
        return RunOutcome("ERROR", "".join(tb[-3:])[-1500:], time.time() - t0, ctx.lines, blocked["n"])
    finally:
        if not commit:
            # The confirm click may submit after the script returned. Keep the
            # block on for a few seconds, then reload the shell so no pending
            # submit survives, and only then lift the block.
            try:
                page.wait_for_timeout(5000)
                page.reload(wait_until="domcontentloaded")
            except Exception:  # noqa: BLE001
                pass
            try:
                page.unroute(f"**/*{SAVE_URL_PART}*")
            except Exception:  # noqa: BLE001
                pass


#: The demonstration every arm learns from, as a person would describe the
#: recording: the same steps ``scripts/openemr_local_demo.py record`` takes.
DEMONSTRATION = """\
Demonstration (one recorded registration, values from one referral):
Start: signed in as the clerk, a blank New Patient form open in OpenEMR's tab
shell (iframe name "openadapt-new-patient", page /interface/new/new_comprehensive.php).
1. Title: click the select #form_title, type "Ms.", press Enter.
2. First name: click #form_fname, Ctrl+A, type the first name.
3. Last name: click #form_lname, Ctrl+A, type the last name.
4. Date of birth: click #form_DOB, Ctrl+A, type YYYY-MM-DD.
5. Sex: click the select #form_sex, type "Female", press Enter.
6. Scroll down 300 px; click the "Contact" section header to expand it
   (contact fields have hidden duplicates while it is collapsed).
7. Scroll down 600 px.
8. Street: #form_street. City: #form_city.
9. State: click the select #form_state, type the state's name
   ("Massachusetts"), press Enter.
10. Postal code: #form_postal_code. Home phone: #form_phone_home.
    Email: #form_email.
11. Country: click the select #form_country_code, type "USA", press Enter.
12. Click the form's save button (#create, labelled "Create New Patient").
    This opens OpenEMR's duplicate-check popup in an iframe in the top page.
    When no chart matches, the popup says "No matches were found." and offers
    a confirm button (#confirmCreate, "Confirm Create New Patient"). When a
    chart may match, it lists the matching charts instead.
13. Click #confirmCreate once it is ready (its document is loaded and the
    button has stopped moving). This performs the real save (a POST to
    new_comprehensive_save.php). The tab then shows the new patient's
    dashboard.
After the save, the chart is readable through the REST API
(GET /apis/default/api/patient?fname=..&lname=..&DOB=..).
"""

API_DOC = """\
OpenEMR REST (read only, via ctx.find_patients): GET /apis/default/api/patient
with any of fname, lname, DOB (YYYY-MM-DD). Returns a list of charts with uuid,
pid, title, fname, lname, DOB, sex, street, city, state, postal_code,
phone_home, email. The state is stored as its code (MA), the country as USA.
"""
