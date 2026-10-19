"""``openadapt-agent serve --teach``: what the operator fixes at start.

No app and no browser: ``serve`` is replaced so each test sees the bridges the
command built, or its refusal.
"""

from __future__ import annotations

from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from openadapt_flow.agent.cli import main

# Never import the MCP SDK at collection: other tests in the same process
# check that emitting a server does not import it.
pytestmark = pytest.mark.skipif(
    find_spec("mcp") is None, reason="needs the agent extra"
)


@pytest.fixture()
def served(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_serve(bridge: Any = None, **kwargs: Any) -> None:
        calls.append({"bridge": bridge, **kwargs})

    monkeypatch.setattr("openadapt_flow.agent.mcp.serve", fake_serve)
    return calls


@pytest.fixture()
def verify(tmp_path: Path) -> Path:
    path = tmp_path / "verify.yaml"
    path.write_text(
        "verifier:\n  kind: rest\n  base_url: http://127.0.0.1:9\n"
        "  records_path: /api/db\n  records_key: records\n"
    )
    return path


def _deploy(tmp_path: Path, backend: str) -> Path:
    path = tmp_path / "deploy.yaml"
    path.write_text(f"name: notes\nbackend:\n{backend}")
    return path


def test_a_web_app(served: list, verify: Path, tmp_path: Path) -> None:
    code = main(
        [
            "serve",
            "--teach",
            str(verify),
            "--url",
            "http://127.0.0.1:9/#tasks",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert code == 0
    teach = served[0]["teach"]
    assert teach.backend_kind == "web" and teach.config.admitter is None
    assert teach.config.cache_dir == (tmp_path / "runs" / "teach").resolve()
    bridge = served[0]["bridge"]
    assert bridge.allow_run and bridge.catalog == {}
    names = {spec.name for spec in teach.list_tool_specs()}
    assert {"teach_start", "teach_act", "teach_finish", "teach_admit"} <= names
    assert "teach_screen" not in names


def test_a_native_app_from_the_deployment(
    served: list, verify: Path, tmp_path: Path
) -> None:
    deploy = _deploy(tmp_path, "  kind: macos\n  macos_app: TextEdit\n")
    code = main(
        [
            "serve",
            "--teach",
            str(verify),
            "--config",
            str(deploy),
            "--teach-screens",
            "scrubbed",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )
    assert code == 0
    teach = served[0]["teach"]
    assert teach.backend_kind == "macos"
    assert "teach_screen" in {spec.name for spec in teach.list_tool_specs()}


@pytest.mark.parametrize(
    ("extra", "backend", "reason"),
    [
        ([], None, "needs the app"),
        (["--url", "http://x", "--teach-key", "a.pem"], None, "go together"),
        ([], "  kind: rdp\n  rdp_host: 10.0.0.5\n", "network RDP"),
        (["--url", "http://x", "--authoring"], None, "own recording tools"),
        (["--url", "http://x", "--bundles", "b"], None, "--mode production"),
    ],
)
def test_refusals(
    served: list,
    verify: Path,
    tmp_path: Path,
    capsys,
    extra: list,
    backend: Any,
    reason: str,
) -> None:
    argv = ["serve", "--teach", str(verify), "--runs-dir", str(tmp_path / "r"), *extra]
    if backend:
        argv += ["--config", str(_deploy(tmp_path, backend))]
    assert main(argv) == 2
    assert reason in capsys.readouterr().err
    assert served == []


def test_teach_flags_need_teach(served: list, capsys) -> None:
    assert main(["serve", "--teach-hold"]) == 2
    assert "need --teach" in capsys.readouterr().err


def test_a_verifier_that_cannot_be_read_is_refused_at_start(
    served: list, tmp_path: Path, capsys
) -> None:
    assert (
        main(["serve", "--teach", str(tmp_path / "missing.yaml"), "--url", "http://x"])
        == 2
    )
    assert "is not a file" in capsys.readouterr().err


def test_a_scrubbed_screen_that_cannot_be_scrubbed_is_not_shared(
    verify: Path, tmp_path: Path
) -> None:
    import threading

    from openadapt_flow.agent.bridge import BridgeError
    from openadapt_flow.agent.teach import TeachBridge, TeachConfig

    def broken() -> Any:
        raise RuntimeError("no scrubber")

    teach = TeachBridge(
        TeachConfig(
            verify=verify,
            cache_dir=tmp_path / "teach",
            url="http://127.0.0.1:9",
            screens="scrubbed",
            scrubber_factory=broken,
        )
    )

    class Run:
        done = threading.Event()

        def call(self, kind: str, payload: Any = None) -> dict:
            assert kind == "screenshot"
            return {"png": b"\x89PNG raw frame"}

    teach._run = Run()
    with pytest.raises(BridgeError, match="no screen was shared"):
        teach.dispatch("teach_screen", {})
