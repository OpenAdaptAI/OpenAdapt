"""`replay`/`run --workers N`: refusals and the per-item wiring.

The browser and the Replayer are faked; the pool, the queue and the CLI
glue are real.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.__main__ import main
from openadapt_flow.compiler.loop_authoring import author_data_driven_loop
from openadapt_flow.ir import ActionKind, ParamSpec, Step, Workflow
from tests.test_cli_new_commands import _loop_program


def _for_each_bundle(tmp_path: Path, n: int = 6) -> tuple[Path, str]:
    body = Workflow(
        name="note",
        steps=[
            Step(
                id="type_note",
                intent="type <note>",
                action=ActionKind.TYPE,
                param="note",
            )
        ],
        param_specs={"note": ParamSpec(name="note", example="x")},
    )
    looped = author_data_driven_loop(body, [{"note": f"n{i}"} for i in range(n)])
    bundle = tmp_path / "bundle"
    looped.save(bundle)
    (rel,) = tuple(looped.data_sources)
    return bundle, rel


def _config(
    tmp_path: Path,
    *,
    max_workers: int = 4,
    accounts: int = 2,
    same_user=False,
    independent=True,
) -> Path:
    lines = [
        "backend:\n  url: http://app.example/form\n",
        "worklist:\n",
        f"  max_workers: {max_workers}\n",
        f"  items_independent: {'true' if independent else 'false'}\n",
        "  accounts:\n",
    ]
    for i in range(accounts):
        lines.append(
            f"    - name: clerk-{i}\n"
            f"      browser_cdp_endpoint: http://127.0.0.1:{9300 + (0 if same_user else i)}\n"
        )
    path = tmp_path / "deploy.yaml"
    path.write_text("".join(lines))
    return path


class _Page:
    def __init__(self, ctx: "_Context") -> None:
        self.ctx = ctx
        self.url = "about:blank"

    def goto(self, url: str) -> None:
        self.url = url

    def set_viewport_size(self, size: Any) -> None:
        pass

    def close(self) -> None:
        pass


class _Context:
    def __init__(self, endpoint: str) -> None:
        self.endpoint = endpoint

    def cookies(self) -> list[dict[str, str]]:
        # Each endpoint is its own signed-in browser: its own session cookie.
        return [
            {
                "domain": "app.example",
                "path": "/",
                "name": "sid",
                "value": self.endpoint,
            }
        ]

    def new_page(self) -> _Page:
        return _Page(self)


class _Browser:
    def __init__(self, endpoint: str) -> None:
        self.contexts = [_Context(endpoint)]


class _Chromium:
    def connect_over_cdp(self, endpoint: str) -> _Browser:
        return _Browser(endpoint)


class _PW:
    chromium = _Chromium()

    def start(self) -> "_PW":
        return self

    def stop(self) -> None:
        pass


class _Report:
    def __init__(self, success: bool) -> None:
        self.success = success
        self.screenshots_may_leave_box = False
        self.execution_outcome = None
        self.transaction_outcome = None
        self.halt = None
        self.total_ms = 1.0


def _install(monkeypatch, calls: list, *, halt_note: str | None = None) -> None:
    lock = threading.Lock()

    class _Replayer:
        def __init__(self, backend, **kwargs):
            self.backend = backend
            self.kwargs = kwargs

        def run(self, workflow, **kwargs):
            note = next(iter(kwargs["worklists"].values()))[0]["note"]
            with lock:
                calls.append(
                    {
                        "note": note,
                        "endpoint": self.backend.ctx.endpoint,
                        "worklists": kwargs["worklists"],
                        "run_dir": kwargs["run_dir"],
                        "auth": self.kwargs.get("governed_authorization"),
                    }
                )
            time.sleep(0.05)  # long enough for both workers to claim items
            return _Report(success=note != halt_note)

    import playwright.sync_api as psa

    import openadapt_flow.backends.playwright_backend as pwb
    import openadapt_flow.runtime as runtime_mod
    import openadapt_flow.runtime.grounder as grounder_mod
    import openadapt_flow.runtime.remote_vlm as remote_mod

    monkeypatch.setattr(psa, "sync_playwright", lambda: _PW())
    monkeypatch.setattr(pwb, "PlaywrightBackend", lambda page, **kw: page)
    monkeypatch.setattr(grounder_mod, "build_grounder", lambda fallback=None: None)
    monkeypatch.setattr(remote_mod, "appliance_from_env", lambda: None)
    monkeypatch.setattr(runtime_mod, "Replayer", _Replayer)


def _replay(bundle: Path, cfg: Path, run_dir: Path, *extra: str) -> int:
    return main(
        ["replay", str(bundle), "--config", str(cfg), "--run-dir", str(run_dir), *extra]
    )


def test_two_workers_split_the_items_one_row_each(
    tmp_path, monkeypatch, capsys
) -> None:
    bundle, rel = _for_each_bundle(tmp_path)
    calls: list = []
    _install(monkeypatch, calls)
    rc = _replay(bundle, _config(tmp_path), tmp_path / "run", "--workers", "2")
    assert rc == 0, capsys.readouterr().out
    assert sorted(c["note"] for c in calls) == [f"n{i}" for i in range(6)]
    for c in calls:
        # Each item is its own run over a one-row worklist, in its own dir.
        assert c["worklists"] == {rel: [{"note": c["note"]}]}
        assert c["run_dir"].parent.name == "items"
    # Both sessions did work, and no item ran twice.
    assert {c["endpoint"] for c in calls} == {
        "http://127.0.0.1:9300",
        "http://127.0.0.1:9301",
    }
    pool = json.loads((tmp_path / "run" / "worklist_pool.json").read_text())
    assert pool["counts"] == {"completed": 6}
    assert {w["name"] for w in pool["per_worker"]} == {"clerk-0", "clerk-1"}
    assert (tmp_path / "run" / "WORKLIST.md").exists()


def test_forced_halt_on_one_worker_stops_new_items(tmp_path, monkeypatch) -> None:
    bundle, _ = _for_each_bundle(tmp_path, n=40)
    calls: list = []
    _install(monkeypatch, calls, halt_note="n1")
    rc = _replay(bundle, _config(tmp_path), tmp_path / "run", "--workers", "2")
    assert rc == 1
    pool = json.loads((tmp_path / "run" / "worklist_pool.json").read_text())
    assert pool["stop"]["idx"] == 1
    assert len(calls) < 40
    assert pool["counts"]["halted"] == 1
    assert pool["counts"]["pending"] > 0


@pytest.mark.parametrize(
    "cfg_kw,flag,needle",
    [
        ({"max_workers": 2}, "4", "limit of 2"),
        ({"accounts": 1}, "2", "needs 2 accounts"),
        ({"same_user": True}, "2", "sign in as the same user"),
        ({"independent": False}, "2", "items_independent"),
    ],
)
def test_unsafe_pool_is_refused_before_any_item(
    tmp_path, monkeypatch, capsys, cfg_kw, flag, needle
) -> None:
    bundle, _ = _for_each_bundle(tmp_path)
    calls: list = []
    _install(monkeypatch, calls)
    rc = _replay(
        bundle, _config(tmp_path, **cfg_kw), tmp_path / "run", "--workers", flag
    )
    assert rc == 2
    assert needle in capsys.readouterr().out
    assert calls == []


def test_no_deployment_means_one_worker_only(tmp_path, monkeypatch, capsys) -> None:
    bundle, _ = _for_each_bundle(tmp_path)
    calls: list = []
    _install(monkeypatch, calls)
    rc = main(
        [
            "replay",
            str(bundle),
            "--url",
            "http://app.example",
            "--run-dir",
            str(tmp_path / "r"),
            "--workers",
            "2",
        ]
    )
    assert rc == 2
    assert "limit of 1" in capsys.readouterr().out
    assert calls == []


def test_program_with_steps_outside_the_loop_is_refused(
    tmp_path, monkeypatch, capsys
) -> None:
    wf = _loop_program()  # an induced program: a click, then the loop
    bundle = tmp_path / "bundle"
    wf.save(bundle)
    calls: list = []
    _install(monkeypatch, calls)
    rc = _replay(bundle, _config(tmp_path), tmp_path / "run", "--workers", "2")
    assert rc == 2
    assert "one loop over a worklist" in capsys.readouterr().out
    assert calls == []
