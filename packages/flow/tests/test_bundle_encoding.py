"""A bundle written on Windows is UTF-8 with LF newlines, like everywhere else.

OCR records whatever text a remote screen shows, including characters such as
"ε" that the Windows default codec (cp1252) cannot encode. Found compiling a
recording over a real RDP session on Windows Server 2022.
"""

from __future__ import annotations

import pathlib

from openadapt_flow.ir import Workflow


def test_save_and_load_ignore_the_platform_codec(tmp_path, monkeypatch):
    original = pathlib.Path.write_text

    def cp1252_write_text(self, data, encoding=None, errors=None, newline=None):
        # What text mode does on Windows when no encoding is given.
        return original(
            self,
            data,
            encoding=encoding or "cp1252",
            errors=errors,
            newline=newline if newline is not None else "\r\n",
        )

    monkeypatch.setattr(pathlib.Path, "write_text", cp1252_write_text)
    workflow = Workflow(name="remote ε — screen", steps=[])
    workflow.save(tmp_path)

    raw = (tmp_path / "workflow.json").read_bytes()
    assert b"\r\n" not in raw
    assert "remote ε — screen" in raw.decode("utf-8")
    assert Workflow.load(tmp_path).name == "remote ε — screen"
