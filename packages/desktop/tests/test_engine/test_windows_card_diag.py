"""TEMPORARY: diagnose the Windows card check in CI. Removed before merge."""

import os
import traceback

import pytest

from openadapt_desktop import exception_queue


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_diag(tmp_path):
    path = exception_queue.registry_root() / "diag-run" / "x.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}")
    out = []
    before = os.lstat(path)
    out.append(f"lstat dev={before.st_dev} ino={before.st_ino} attrs={getattr(before, 'st_file_attributes', None)}")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0))
    try:
        info = os.fstat(fd)
        out.append(f"fstat dev={info.st_dev} ino={info.st_ino}")
        try:
            out.append(f"me={sorted(exception_queue._windows_me())}")
        except Exception:
            out.append("me failed: " + traceback.format_exc())
        try:
            import msvcrt

            from openadapt_desktop.approver_anchor import _windows_security

            owner, aces = _windows_security(path, handle=msvcrt.get_osfhandle(fd))
            out.append(f"handle owner={owner} aces={aces}")
        except Exception:
            out.append("handle security failed: " + traceback.format_exc())
        try:
            from openadapt_desktop.approver_anchor import _windows_security

            owner, aces = _windows_security(path)
            out.append(f"named owner={owner} aces={aces}")
        except Exception:
            out.append("named security failed: " + traceback.format_exc())
    finally:
        os.close(fd)
    pytest.fail("DIAG\n" + "\n".join(out))
