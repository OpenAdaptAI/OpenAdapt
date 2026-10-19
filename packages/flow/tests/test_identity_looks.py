"""Extra looks before stopping on a look-alike identifier (runtime.identity_looks).

Pure tests of the decision rule and the copy look. The replayer path, with a
real render and real OCR, is in ``test_identity_looks_replayer.py``.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.ir import IdentityCheck
from openadapt_flow.runtime import identity_looks as L
from openadapt_flow.runtime.known_ids import KnownIdList
from openadapt_flow.runtime.replayer import identity_read_contradicts

BAND = "AC50061 Smith, John 01/15/1980"


def _abstain(observed: str = BAND) -> IdentityCheck:
    return IdentityCheck(
        status="abstain",
        abstain_reason="glyph_confusable_identifier",
        coverage=1.0,
        expected=BAND,
        observed=observed,
    )


def _frames(*reads: IdentityCheck):
    return [(r, "a" * 64) for r in reads]


def _decide(**kw):
    kw.setdefault("frames", _frames(_abstain(), _abstain()))
    kw.setdefault("magnified", _abstain())
    return L.decide(_abstain(), contradicts=identity_read_contradicts, **kw)


def _copy(text: str) -> L.ClipboardRead:
    return L.ClipboardRead(text=text, frame_sha256="b" * 64)


def test_ocr_looks_alone_never_accept():
    check = _decide()
    assert check.status == "abstain"
    assert [look.kind for look in check.looks] == ["frame", "frame", "magnified"]
    assert all(look.outcome == "agrees" for look in check.looks)


def test_copy_equal_to_run_value_settles_the_record():
    check = _decide(
        clipboard=_copy("AC50061\tSmith, John\t01/15/1980"), exact_expected=("ac50061",)
    )
    assert check.status == "verified"
    assert check.looks[-1].kind == "clipboard"
    assert check.looks[-1].outcome == "resolves"


def test_copy_of_look_alike_twin_is_a_mismatch():
    check = _decide(
        clipboard=_copy("AC5OO61 Smith, John 01/15/1980"), exact_expected=("ac50061",)
    )
    assert check.status == "mismatch"
    assert check.looks[-1].outcome == "contradicts"


def test_copy_without_an_exact_expected_id_cannot_accept():
    # Recorded-record mode: the recorded band is OCR text, not exact.
    check = _decide(clipboard=_copy("AC50061 Smith, John 01/15/1980"))
    assert check.status == "abstain"
    assert check.looks[-1].outcome == "agrees"


def test_copy_of_an_unlisted_id_contradicts_the_list():
    ids = KnownIdList(ids=("AC50061", "ZZ1000"), source="t", complete=True)
    check = _decide(
        clipboard=_copy("AC5OO61"),
        known_ids=ids,
        known_ids_lift=_abstain().model_copy(update={"status": "verified"}),
    )
    assert check.status == "mismatch"


def test_known_id_list_resolves_when_frames_agree():
    ids = KnownIdList(ids=("AC50061", "ZZ1000"), source="t", complete=True)
    lift = _abstain().model_copy(update={"status": "verified"})
    check = _decide(known_ids=ids, known_ids_lift=lift)
    assert check.status == "verified"
    assert check.looks[-1].kind == "known_ids"


def test_a_frame_naming_another_record_is_final():
    other = IdentityCheck(
        status="mismatch", expected=BAND, observed="ZZ9999 Doe, Jane 02/02/1990"
    )
    check = _decide(
        frames=_frames(other, _abstain()),
        clipboard=_copy("AC50061"),
        exact_expected=("ac50061",),
    )
    assert check.status == "mismatch"
    assert check.looks[0].outcome == "contradicts"


def test_frames_that_read_another_identifier_block_acceptance():
    drifted = _abstain("AC5OO6l Smith, John 01/15/1980")  # same class, still ok
    moved = _abstain("AC50071 Smith, John 01/15/1980")  # different class
    assert (
        _decide(
            frames=_frames(drifted, _abstain()),
            clipboard=_copy("AC50061"),
            exact_expected=("ac50061",),
        ).status
        == "verified"
    )
    check = _decide(
        frames=_frames(moved, _abstain()),
        clipboard=_copy("AC50061"),
        exact_expected=("ac50061",),
    )
    assert check.status == "abstain"
    assert check.looks[0].outcome == "unavailable"


def test_no_fresh_frames_means_no_acceptance():
    check = _decide(frames=[], clipboard=_copy("AC50061"), exact_expected=("ac50061",))
    assert check.status == "abstain"


def test_magnified_read_naming_another_record_is_final():
    other = IdentityCheck(
        status="mismatch", expected=BAND, observed="ZZ9999 Doe, Jane 02/02/1990"
    )
    check = _decide(
        magnified=other, clipboard=_copy("AC50061"), exact_expected=("ac50061",)
    )
    assert check.status == "mismatch"


def test_copy_that_covers_a_different_cell_settles_nothing():
    check = _decide(clipboard=_copy("ZZ1000 Filler, Pat"), exact_expected=("ac50061",))
    assert check.status == "abstain"
    assert check.looks[-1].reason == "copy_differs_from_read"


def test_only_glyph_abstains_take_looks():
    short = IdentityCheck(status="abstain", abstain_reason="identity_evidence_short")
    assert (
        L.decide(
            short, frames=[], magnified=None, contradicts=identity_read_contradicts
        )
        is short
    )


def test_exact_expected_ids_come_only_from_band_params():
    run = {"mrn": "AC50061", "note": "AC99999"}
    assert L.exact_expected_ids(run, ["mrn"]) == ("ac50061",)
    assert L.exact_expected_ids(run, []) == ()


def test_copy_config_rejects_unknown_gesture():
    with pytest.raises(ValueError):
        L.IdentityCopyConfig(gesture="triple_click")


# ------------------------------------------------------------ copy look


def _png(highlight=None, extra=None) -> bytes:
    img = Image.new("RGB", (200, 100), "white")
    draw = ImageDraw.Draw(img)
    draw.text((20, 40), "AC50061", fill="black")
    if highlight:
        draw.rectangle(highlight, fill=(51, 144, 255))
    if extra:
        draw.rectangle(extra, fill="red")
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class _Clipboard:
    def __init__(self, text="user's own text"):
        self.text = text
        self.writes = []

    def read_text(self):
        return self.text

    def write_text(self, text):
        self.writes.append(text)
        self.text = text


class _Backend:
    def __init__(self, clipboard, copied="AC50061", after=None):
        self.clipboard = clipboard
        self.copied = copied
        self.after = after
        self.frame = _png()
        self.drags = []
        self.keys = []

    def screenshot(self):
        return self.frame

    def drag(self, x, y, ex, ey):
        self.drags.append((x, y, ex, ey))
        self.frame = self.after if self.after is not None else self.frame

    def click(self, x, y, *, double=False):
        raise AssertionError("drag gesture must not click")

    def press(self, key):
        self.keys.append(key)
        if self.copied is not None:
            self.clipboard.text = self.copied


REGION = (15, 35, 80, 20)


def test_copy_reads_the_cell_and_restores_the_clipboard():
    clip = _Clipboard()
    backend = _Backend(clip, after=_png(highlight=(16, 36, 90, 52)))
    read = L.copy_identifier(backend, clip, REGION)
    assert read.text == "AC50061"
    assert not read.changed_outside
    assert backend.drags and backend.drags[0][1] == 45  # mid height
    assert backend.keys == ["Control+c"]
    assert clip.text == "user's own text"  # restored


def test_marker_left_in_clipboard_counts_as_nothing_copied():
    clip = _Clipboard()
    read = L.copy_identifier(_Backend(clip, copied=None), clip, REGION)
    assert read.text is None and read.reason == "nothing_copied"


def test_screen_change_outside_the_cell_is_flagged():
    clip = _Clipboard()
    backend = _Backend(clip, after=_png(extra=(150, 80, 190, 95)))
    read = L.copy_identifier(backend, clip, REGION)
    assert read.changed_outside


def test_backend_without_drag_takes_no_look():
    class NoDrag:
        def screenshot(self):
            return _png()

    read = L.copy_identifier(NoDrag(), _Clipboard(), REGION)
    assert read.text is None and read.reason == "backend_cannot_drag"


def test_crops_match_tolerates_noise_not_a_changed_glyph():
    a = L.crop_png(_png(), REGION)
    img = Image.open(io.BytesIO(a)).convert("RGB")
    px = img.load()
    px[0, 0] = tuple(max(0, c - 3) for c in px[0, 0])
    noisy = io.BytesIO()
    img.save(noisy, format="PNG")
    assert L.crops_match(a, noisy.getvalue())
    other = Image.new("RGB", (200, 100), "white")
    ImageDraw.Draw(other).text((20, 40), "AC5OO61", fill="black")
    buf = io.BytesIO()
    other.save(buf, format="PNG")
    assert not L.crops_match(a, L.crop_png(buf.getvalue(), REGION))


def test_copy_region_is_the_single_line_holding_a_lookalike_id():
    from types import SimpleNamespace as NS

    within = (0, 0, 600, 40)
    mrn = NS(text="AC50061", region=(10, 5, 90, 20))
    name = NS(text="Smith, John", region=(200, 5, 120, 20))
    dob = NS(text="01/15/1980", region=(400, 5, 110, 20))
    assert L.copy_region([mrn, name, dob], within) == (8, 3, 94, 24)
    other = NS(text="ZZ10O1", region=(520, 5, 60, 20))
    assert L.copy_region([mrn, other], within) is None  # two candidates
    assert L.copy_region([name, dob], within) is None  # none
    outside = NS(text="AC50061", region=(10, 100, 90, 20))
    assert L.copy_region([outside], within) is None
