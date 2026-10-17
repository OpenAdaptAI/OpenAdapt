"""``attach-seal`` reaches its own handler, not the bundle ``seal`` command.

``__main__`` used to define ``_cmd_seal`` twice; the bundle ``seal`` handler
won and ``attach-seal`` died with ``AttributeError: 'Namespace' object has no
attribute 'source'``.
"""

from __future__ import annotations

from pathlib import Path

import openadapt_flow.__main__ as cli
from openadapt_flow import cloud_seal


def test_attach_seal_calls_attach_from_cloud(tmp_path, monkeypatch, capsys):
    calls = []

    def fake_attach(run_dir: Path, *, execution_id: str, cloud_url=None):
        calls.append((run_dir, execution_id, cloud_url))
        return cloud_seal.AttachResult(
            attached=True, code="attached", execution_id=execution_id
        )

    monkeypatch.setattr(cloud_seal, "attach_from_cloud", fake_attach)
    code = cli.main(["attach-seal", str(tmp_path), "--execution-id", "exec-123"])
    assert code == 0
    assert calls == [(tmp_path, "exec-123", None)]
    assert "exec-123" in capsys.readouterr().out


def test_attach_seal_and_seal_have_distinct_handlers():
    parser = cli.build_parser()
    attach = parser.parse_args(["attach-seal", "run", "--execution-id", "e"])
    assert attach.func is cli._cmd_attach_seal
    assert attach.func is not cli._cmd_seal
