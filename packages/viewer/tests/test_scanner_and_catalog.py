"""Regression tests for the user-visible behaviour changed by the ruff 0.16 sweep.

Each test here pins one thing a user can observe:

* the segmentation scanner no longer hides an unparseable ``processing_timestamp``
  behind the file's mtime in silence (it was a bare ``except: pass``);
* making the timestamps timezone-aware did NOT move any wall clock the user reads;
* the packaged stylesheet no longer degrades to the inline fallback silently.
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from openadapt_viewer.builders.page_builder import PageBuilder
from openadapt_viewer.catalog import RecordingCatalog
from openadapt_viewer.scanner import RecordingScanner
from openadapt_viewer.segmentation_catalog import SegmentationCatalogEntry
from openadapt_viewer.viewers.capture import generator as capture_generator


@pytest.fixture
def scanner(tmp_path):
    return RecordingScanner(RecordingCatalog(db_path=str(tmp_path / "catalog.db")))


def _episodes_file(tmp_path: Path, processing_timestamp) -> Path:
    path = tmp_path / "rec-001_episodes.json"
    payload = {"episodes": [], "boundaries": []}
    if processing_timestamp is not None:
        payload["processing_timestamp"] = processing_timestamp
    path.write_text(json.dumps(payload))
    return path


class TestProcessingTimestampFallback:
    """`except: pass` made a bad timestamp look identical to an absent one."""

    def test_unparseable_timestamp_falls_back_to_mtime_and_says_so(
        self, scanner, tmp_path, capsys
    ):
        path = _episodes_file(tmp_path, "not a timestamp")

        result = scanner._extract_segmentation_info(path)

        assert result.created_at == path.stat().st_mtime
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "processing_timestamp" in out

    def test_valid_timestamp_is_used_and_is_silent(self, scanner, tmp_path, capsys):
        stamp = "2024-01-15T10:30:00"
        path = _episodes_file(tmp_path, stamp)

        result = scanner._extract_segmentation_info(path)

        assert result.created_at == datetime.fromisoformat(stamp).timestamp()
        assert "Warning" not in capsys.readouterr().out

    def test_absent_timestamp_is_silent(self, scanner, tmp_path, capsys):
        path = _episodes_file(tmp_path, None)

        result = scanner._extract_segmentation_info(path)

        assert result.created_at == path.stat().st_mtime
        assert "Warning" not in capsys.readouterr().out

    def test_keyboard_interrupt_is_not_swallowed(self, scanner, tmp_path, monkeypatch):
        """The bare `except:` also caught KeyboardInterrupt and SystemExit."""
        path = _episodes_file(tmp_path, "2024-01-15T10:30:00")

        class _Interrupting(datetime):
            @classmethod
            def fromisoformat(cls, _value):
                raise KeyboardInterrupt

        monkeypatch.setattr("openadapt_viewer.scanner.datetime", _Interrupting)
        with pytest.raises(KeyboardInterrupt):
            scanner._extract_segmentation_info(path)


class TestTimestampsRenderUnchanged:
    """`.astimezone()` anchors the local zone; it must not shift what is shown."""

    @pytest.mark.parametrize("epoch", [0.0, 1704067200.0, 1751000000.5])
    def test_catalog_entry_renders_local_wall_clock(self, epoch):
        entry = SegmentationCatalogEntry(
            file_path="/tmp/x_episodes.json",
            recording_name="X",
            recording_id="x",
            created_at=epoch,
            episode_count=0,
            file_type="episodes",
        )

        # Deliberately the pre-change expression -- naive local time. The whole
        # point of this test is that the aware version renders identically, so
        # DTZ006 is suppressed here rather than "fixed" away.
        expected = datetime.fromtimestamp(epoch).strftime(  # noqa: DTZ006
            "%Y-%m-%d %H:%M:%S"
        )

        assert entry.to_dict()["created_at_formatted"] == expected

    @pytest.mark.parametrize("epoch", [0.0, 1704067200.0])
    def test_astimezone_preserves_every_wall_clock_field(self, epoch):
        naive = datetime.fromtimestamp(epoch)  # noqa: DTZ006 - that is the point
        aware = datetime.fromtimestamp(epoch).astimezone()

        assert aware.timetuple()[:6] == naive.timetuple()[:6]
        assert aware.timestamp() == naive.timestamp()


class TestCoreCssFallbackIsAudible:
    """An unreadable packaged core.css used to degrade the page in total silence."""

    def test_page_builder_warns_when_core_css_unreadable(self, monkeypatch, capsys):
        def _boom(self, *args, **kwargs):
            raise OSError("simulated read failure")

        monkeypatch.setattr(Path, "read_text", _boom)

        css = PageBuilder(title="t")._get_core_css()

        assert "--oa-bg-primary" in css  # fallback CSS was used
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "core.css" in out

    def test_capture_generator_warns_when_core_css_unreadable(self, monkeypatch, capsys):
        def _boom(self, *args, **kwargs):
            raise OSError("simulated read failure")

        monkeypatch.setattr(Path, "read_text", _boom)

        css = capture_generator._get_core_css()

        assert "--oa-bg-primary" in css
        out = capsys.readouterr().out
        assert "Warning" in out
        assert "core.css" in out
