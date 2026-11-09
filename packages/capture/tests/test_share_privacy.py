"""Nothing a share sends leaves the computer unscrubbed unless the operator opts out."""

from __future__ import annotations

import io
import json
import shutil
import sqlite3
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from openadapt_capture import share, share_scrub

PHI = "John Smith"
MRN = "MRN-483920"
EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "captures" / "demo_new"


class FakeScrubber:
    """Replaces the two known identifiers and blacks out every image."""

    def scrub_text(self, text: str, is_separated: bool = False) -> str:
        return text.replace(PHI, "<PERSON>").replace(MRN, "<MRN>")

    def scrub_image(self, image, fill_color=None):
        return Image.new("RGB", image.size, (0, 0, 0))


def _png(color=(255, 0, 0)) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(out, format="PNG")
    return out.getvalue()


def _make_recording(root: Path) -> Path:
    rec = root / f"intake_{PHI.replace(' ', '_')}"
    rec.mkdir()
    db = sqlite3.connect(rec / "recording.db")
    db.executescript(
        """
        CREATE TABLE recording (id INTEGER PRIMARY KEY, task_description VARCHAR,
                                platform VARCHAR, config JSON);
        CREATE TABLE window_event (id INTEGER PRIMARY KEY, title VARCHAR, state JSON);
        CREATE TABLE action_event (id INTEGER PRIMARY KEY, name VARCHAR,
            key_name VARCHAR, key_char VARCHAR, key_vk VARCHAR,
            canonical_key_char VARCHAR, element_state JSON);
        CREATE TABLE screenshot (id INTEGER PRIMARY KEY, png_data BLOB,
            png_sha256 VARCHAR, png_diff_data BLOB);
        CREATE TABLE audio_info (id INTEGER PRIMARY KEY, flac_data BLOB,
            transcribed_text VARCHAR, words_with_timestamps TEXT);
        """
    )
    db.execute(
        "INSERT INTO recording VALUES (1, ?, 'darwin', ?)",
        (f"Update chart for {PHI}", json.dumps({"window_title": PHI})),
    )
    db.execute(
        "INSERT INTO window_event VALUES (1, ?, ?)",
        (f"Chart - {PHI}", json.dumps({"children": [{"name": PHI, "value": MRN}]})),
    )
    for i, char in enumerate(PHI):
        db.execute(
            "INSERT INTO action_event VALUES (?, 'press', ?, ?, ?, ?, ?)",
            (i + 1, None, char, str(ord(char)), char, json.dumps({"label": MRN})),
        )
    db.execute(
        "INSERT INTO action_event VALUES (100, 'press', 'enter', NULL, NULL, NULL, NULL)"
    )
    db.execute(
        "INSERT INTO screenshot VALUES (1, ?, 'deadbeef', ?)",
        (_png(), PHI.encode()),
    )
    db.execute(
        "INSERT INTO audio_info VALUES (1, ?, ?, ?)",
        (PHI.encode(), f"patient {PHI}", json.dumps([{"word": "John"}])),
    )
    db.commit()
    db.close()
    (rec / "notes.json").write_text(json.dumps({"patient": PHI, "mrn": MRN}))
    (rec / "video.mp4").write_bytes(b"\x00\x00\x00 ftyp" + PHI.encode())
    (rec / "capture-terminal.json").write_text(json.dumps({"state": "COMPLETE"}))
    return rec


@pytest.fixture
def fake_scrubber():
    share_scrub.set_scrubber(FakeScrubber())
    yield
    share_scrub.reset_scrubber()


@pytest.fixture
def no_privacy_extra(monkeypatch):
    """Simulate a machine without openadapt-privacy installed."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("openadapt_privacy"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    share_scrub.reset_scrubber()
    monkeypatch.setattr(builtins, "__import__", fake_import)
    yield
    share_scrub.reset_scrubber()


@pytest.fixture
def wormhole(monkeypatch, tmp_path):
    """Capture what would be sent instead of running Magic Wormhole."""
    sent: list[dict] = []

    def fake_run(cmd, check=True, **kwargs):
        path = Path(cmd[-1])
        if path.suffix == ".zip":
            with zipfile.ZipFile(path) as zf:
                sent.append({n: zf.read(n) for n in zf.namelist()})
        else:
            sent.append({path.name: path.read_bytes()})

    monkeypatch.setattr(share, "_ensure_wormhole", lambda: "/bin/wormhole")
    monkeypatch.setattr(share, "_find_wormhole", lambda: "/bin/wormhole")
    monkeypatch.setattr(share.subprocess, "run", fake_run)
    monkeypatch.setenv("OPENADAPT_CAPTURE_DATA_DIR", str(tmp_path / "data"))
    return sent


def _all_bytes(files: dict[str, bytes]) -> bytes:
    return b"".join(name.encode() + data for name, data in files.items())


def test_scrubbed_copy_holds_no_identifiers(tmp_path, fake_scrubber):
    rec = _make_recording(tmp_path)
    out = share_scrub.scrub_recording(rec, tmp_path / "out")

    for file in out.rglob("*"):
        if file.is_file():
            raw = file.read_bytes()
            assert PHI.encode() not in raw, file
            assert MRN.encode() not in raw, file
    assert "John" not in out.name

    db = sqlite3.connect(out / "recording.db")
    keys = db.execute(
        "SELECT key_char, key_vk, canonical_key_char FROM action_event"
    ).fetchall()
    assert all(row == (None, None, None) for row in keys)
    assert db.execute(
        "SELECT key_name FROM action_event WHERE id=100"
    ).fetchone() == ("enter",)
    png, digest, diff = db.execute(
        "SELECT png_data, png_sha256, png_diff_data FROM screenshot"
    ).fetchone()
    assert Image.open(io.BytesIO(png)).getpixel((0, 0)) == (0, 0, 0)
    assert digest != "deadbeef" and diff is None
    assert db.execute("SELECT flac_data, words_with_timestamps FROM audio_info").fetchone() == (
        None,
        None,
    )
    db.close()

    manifest = json.loads((out / share_scrub.MANIFEST_NAME).read_text())
    withheld = {item["path"] for item in manifest["withheld"]}
    assert {"video.mp4", "capture-terminal.json"} <= withheld
    assert not (out / "video.mp4").exists()
    assert (out / "notes.json").exists()


def test_scrubbing_the_real_capture_schema(tmp_path, fake_scrubber):
    rec = tmp_path / "demo_new"
    shutil.copytree(EXAMPLE, rec)
    out = share_scrub.scrub_recording(rec, tmp_path / "out")
    assert out.name == "demo_new"
    assert (out / "capture-state.json").exists()
    db = sqlite3.connect(out / "recording.db")
    assert db.execute("SELECT count(*) FROM action_event").fetchone()[0] > 0
    assert (
        db.execute("SELECT count(*) FROM action_event WHERE key_char IS NOT NULL").fetchone()[0]
        == 0
    )
    db.close()


def test_send_scrubs_by_default(tmp_path, fake_scrubber, wormhole):
    rec = _make_recording(tmp_path)
    assert share.send(str(rec)) == "sent"

    (files,) = wormhole
    payload = _all_bytes(files)
    assert PHI.encode() not in payload and MRN.encode() not in payload
    assert any(name.endswith(share_scrub.MANIFEST_NAME) for name in files)
    assert not any(name.endswith("video.mp4") for name in files)
    assert not (tmp_path / "data" / share.OPT_OUT_LEDGER).exists()


def test_send_refuses_without_privacy_extra(tmp_path, no_privacy_extra, wormhole, capsys):
    rec = _make_recording(tmp_path)
    assert share.send(str(rec)) is None
    assert wormhole == []
    out = capsys.readouterr().out
    assert "openadapt-privacy" in out and "Nothing was sent" in out


def test_unscrubbed_needs_a_reason(tmp_path, fake_scrubber, wormhole):
    rec = _make_recording(tmp_path)
    assert share.send(str(rec), unscrubbed=True) is None
    assert share.send(str(rec), unscrubbed=True, reason="  ") is None
    assert wormhole == []


def test_unscrubbed_opt_out_is_recorded(tmp_path, no_privacy_extra, wormhole):
    rec = _make_recording(tmp_path)
    assert share.send(str(rec), unscrubbed=True, reason="synthetic demo data") == "sent"

    (files,) = wormhole
    assert PHI.encode() in _all_bytes(files)
    marker_name = next(n for n in files if n.endswith(share.OPT_OUT_MARKER))
    marker = json.loads(files[marker_name])
    assert marker["reason"] == "synthetic demo data"

    ledger = tmp_path / "data" / share.OPT_OUT_LEDGER
    (line,) = ledger.read_text().splitlines()
    record = json.loads(line)
    assert record["reason"] == "synthetic demo data"
    assert record["recording"] == str(rec.resolve())


def test_profile_send_withholds_strings(tmp_path, wormhole):
    from openadapt_capture import recorder

    profile = tmp_path / "profiling.json"
    profile.write_text(
        json.dumps(
            {
                "capture_dir": f"/captures/{PHI}",
                "config": {"RECORD_WINDOW_TITLE": PHI, "RECORD_VIDEO": True},
                "event_counts": {"action": 3},
            }
        )
    )
    recorder._send_profiling_via_wormhole(str(profile))

    (files,) = wormhole
    sent = json.loads(files["profiling.json"])
    assert PHI not in json.dumps(sent)
    assert sent["event_counts"] == {"action": 3}
    assert sent["config"]["RECORD_VIDEO"] is True
