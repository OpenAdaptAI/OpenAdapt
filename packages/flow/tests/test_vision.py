"""Unit tests for openadapt_flow.vision (match, ocr, hashing, settle).

All fixtures are synthetic images generated with numpy/cv2 — no network,
no real app, no Agent A code.
"""

from __future__ import annotations

import logging
import time

import cv2
import numpy as np
import pytest

from openadapt_flow.vision import (
    Match,
    find_structural_template,
    find_template,
    find_text,
    ocr,
    phash_distance,
    phash_png,
    wait_settled,
)


def to_png(img: np.ndarray) -> bytes:
    ok, buf = cv2.imencode(".png", img)
    assert ok
    return buf.tobytes()


def blank(w: int = 1280, h: int = 800, gray: int = 245) -> np.ndarray:
    return np.full((h, w, 3), gray, dtype=np.uint8)


def draw_button(img: np.ndarray, x: int, y: int, w: int, h: int, label: str) -> None:
    cv2.rectangle(img, (x, y), (x + w, y + h), (200, 200, 200), -1)
    cv2.rectangle(img, (x, y), (x + w, y + h), (80, 80, 80), 2)
    cv2.putText(
        img,
        label,
        (x + 12, y + h // 2 + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.7,
        (0, 0, 0),
        2,
        cv2.LINE_AA,
    )


def draw_glyph(img: np.ndarray, x: int, y: int) -> None:
    """A bold geometric pattern that survives rescaling."""
    cv2.rectangle(img, (x, y), (x + 120, y + 60), (30, 60, 200), -1)
    cv2.circle(img, (x + 40, y + 30), 20, (250, 250, 250), -1)
    cv2.rectangle(img, (x + 80, y + 10), (x + 110, y + 50), (10, 200, 90), -1)


# -- find_template ------------------------------------------------------------


class TestFindTemplate:
    def setup_method(self) -> None:
        self.img = blank()
        draw_button(self.img, 560, 400, 160, 48, "Sign In")
        draw_glyph(self.img, 100, 100)
        self.screen = to_png(self.img)
        # template = exact crop of the button area
        self.tmpl_region = (550, 392, 180, 64)
        x, y, w, h = self.tmpl_region
        self.template = to_png(self.img[y : y + h, x : x + w])

    def test_exact_match(self) -> None:
        m = find_template(self.screen, self.template)
        assert isinstance(m, Match)
        x, y, w, h = self.tmpl_region
        assert m.region == (x, y, w, h)
        assert m.point == (x + w // 2, y + h // 2)
        assert m.confidence > 0.99

    def test_search_region_returns_screen_coords(self) -> None:
        m = find_template(
            self.screen, self.template, search_region=(500, 350, 300, 200)
        )
        assert m is not None
        assert m.region[:2] == self.tmpl_region[:2]  # screen coords, not local

    def test_search_region_excluding_target(self) -> None:
        # blank area, big enough for the template, but no button in it
        m = find_template(
            self.screen, self.template, search_region=(800, 550, 400, 220)
        )
        assert m is None

    def test_template_larger_than_search_region(self) -> None:
        # region smaller than the template at every scale: all scales
        # skipped, graceful None (no exception)
        m = find_template(self.screen, self.template, search_region=(560, 400, 40, 20))
        assert m is None

    def test_search_region_out_of_bounds_clamped(self) -> None:
        m = find_template(
            self.screen, self.template, search_region=(-50, -50, 900, 900)
        )
        assert m is not None
        assert m.region[:2] == self.tmpl_region[:2]

    def test_fully_out_of_bounds_region(self) -> None:
        assert (
            find_template(
                self.screen, self.template, search_region=(5000, 5000, 10, 10)
            )
            is None
        )

    def test_multi_scale_matches_rescaled_template(self) -> None:
        # Template captured ~18% larger than on-screen: the 0.85 rung of the
        # scale ladder brings it back to ~1.0x and should match.
        x, y, w, h = 100, 100, 121, 61
        crop = self.img[y : y + h, x : x + w]
        big = cv2.resize(
            crop, (int(w * 1.18), int(h * 1.18)), interpolation=cv2.INTER_CUBIC
        )
        m = find_template(self.screen, to_png(big))
        assert m is not None
        assert abs(m.region[0] - x) <= 3
        assert abs(m.region[1] - y) <= 3

    def test_no_match_below_threshold(self) -> None:
        other = blank()
        draw_button(other, 300, 300, 160, 48, "Different")
        glyph_tmpl = to_png(self.img[100:160, 100:220])
        assert find_template(to_png(other), glyph_tmpl) is None

    def test_threshold_is_honored(self) -> None:
        # with threshold 0 even a junk match is returned
        other = to_png(blank())
        m = find_template(other, self.template, threshold=0.0)
        assert m is not None and m.confidence < 0.82


class TestFindStructuralTemplate:
    def test_palette_inversion_preserves_structure(self) -> None:
        recorded = blank(420, 220)
        draw_button(recorded, 120, 80, 160, 48, "Save Encounter")
        x, y, w, h = (105, 65, 190, 78)
        template = to_png(recorded[y : y + h, x : x + w])
        themed = 255 - recorded

        # The ordinary grayscale matcher correctly rejects the palette flip;
        # REGION_STABLE's edge representation recognizes the same structure.
        assert (
            find_template(
                to_png(themed),
                template,
                search_region=(80, 40, 260, 140),
                threshold=0.9,
            )
            is None
        )
        match = find_structural_template(
            to_png(themed),
            template,
            search_region=(80, 40, 260, 140),
            threshold=0.8,
        )
        assert match is not None
        assert match.confidence > 0.95
        assert abs(match.region[0] - x) <= 1
        assert abs(match.region[1] - y) <= 1

    def test_true_region_change_is_not_rescued(self) -> None:
        recorded = blank(420, 220)
        draw_button(recorded, 120, 80, 160, 48, "Save Encounter")
        x, y, w, h = (105, 65, 190, 78)
        template = to_png(recorded[y : y + h, x : x + w])
        changed = blank(420, 220, gray=30)
        cv2.rectangle(changed, (100, 60), (300, 150), (220, 220, 220), -1)

        assert (
            find_structural_template(
                to_png(changed),
                template,
                search_region=(80, 40, 260, 140),
                threshold=0.8,
            )
            is None
        )

    def test_flat_template_is_unverifiable_not_a_match(self) -> None:
        assert (
            find_structural_template(
                to_png(blank(420, 220, gray=30)),
                to_png(blank(190, 78, gray=220)),
                search_region=(80, 40, 260, 140),
                threshold=0.8,
            )
            is None
        )


# -- ocr / find_text ----------------------------------------------------------


class TestOcr:
    def setup_method(self) -> None:
        img = blank()
        draw_button(img, 560, 400, 160, 48, "Sign In")
        cv2.putText(
            img,
            "Referral Tasks",
            (100, 84),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        self.screen = to_png(img)

    def test_ocr_finds_labels(self) -> None:
        texts = {line.text.lower() for line in ocr(self.screen)}
        assert any("sign in" in t for t in texts)
        assert any("referral tasks" in t for t in texts)

    def test_ocr_region_restricts_and_offsets(self) -> None:
        region = (540, 380, 200, 90)  # around the Sign In button only
        lines = ocr(self.screen, region=region)
        assert lines, "expected OCR text inside the region"
        for line in lines:
            assert "referral" not in line.text.lower()
            x, y, w, h = line.region
            # coordinates are global (screen) coords inside the region
            assert region[0] <= x and x + w <= region[0] + region[2]
            assert region[1] <= y and y + h <= region[1] + region[3]

    def test_ocr_empty_region(self) -> None:
        assert ocr(self.screen, region=(2000, 2000, 50, 50)) == []

    def test_find_text_fuzzy(self) -> None:
        m = find_text(self.screen, "sign in")  # case-insensitive fuzzy
        assert m is not None
        assert m.confidence >= 0.8
        # click point is inside the button label's box
        assert 540 <= m.point[0] <= 740 and 390 <= m.point[1] <= 460

    def test_find_text_absent(self) -> None:
        assert find_text(self.screen, "Completely Absent Label") is None

    def test_find_text_min_ratio(self) -> None:
        # near-miss text passes only with a permissive ratio
        strict = find_text(self.screen, "Sign Inn Now", min_ratio=0.95)
        assert strict is None


class TestTextPresent:
    """Segmentation-tolerant presence check (postcondition criterion).

    Regression for the TestMoveDrift CI flake: MockMed's save banner is
    'Encounter saved — <note>', and the compiled TEXT_PRESENT asserts the
    stable prefix. rapidocr sometimes returns the banner as ONE box (prefix
    merged with the note) and sometimes as two; whole-line find_text falls
    below min_ratio in the merged case, so the postcondition false-failed
    on a correct screen. text_present must pass regardless of segmentation
    while a genuinely missing target (modal-drift screen, which shares the
    words 'Encounter' and 'Save') still fails.
    """

    def banner_screen(self, banner: str) -> bytes:
        img = blank()
        cv2.putText(
            img,
            banner,
            (40, 160),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        return to_png(img)

    def test_target_merged_into_longer_line_passes(self) -> None:
        from openadapt_flow.vision import text_present

        # One long rendered line: whole-line similarity to the short
        # target is ~0.46, but the target is plainly contained.
        screen = self.banner_screen("Encounter saved - E2E triage booking three months")
        merged = any(
            "saved" in line.text.lower() and "months" in line.text.lower()
            for line in ocr(screen)
        )
        if merged:  # segmentation is the engine's choice; pin the bug
            assert find_text(screen, "Encounter saved-") is None
        assert text_present(screen, "Encounter saved-")

    def test_target_alone_on_line_passes(self) -> None:
        from openadapt_flow.vision import text_present

        screen = self.banner_screen("Encounter saved -")
        assert text_present(screen, "Encounter saved-")

    def test_shared_words_do_not_fake_presence(self) -> None:
        from openadapt_flow.vision import text_present

        # The modal-drift screen: 'Encounter'/'Save Encounter' visible,
        # but 'Encounter saved' never happened — must stay absent (this
        # is what makes the modal scenario fail honestly).
        img = blank()
        cv2.putText(
            img,
            "New Encounter",
            (40, 90),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.8,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        cv2.putText(
            img,
            "Encounter Type",
            (40, 150),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        draw_button(img, 700, 380, 220, 48, "Save Encounter")
        cv2.putText(
            img,
            "Survey: how did we do?",
            (400, 300),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (0, 0, 0),
            2,
            cv2.LINE_AA,
        )
        screen = to_png(img)
        assert not text_present(screen, "Encounter saved-")

    def test_blank_and_empty_target(self) -> None:
        from openadapt_flow.vision import text_present

        assert not text_present(to_png(blank()), "Encounter saved-")
        assert not text_present(self.banner_screen("anything"), "   ")


# -- hashing ------------------------------------------------------------------


class TestHashing:
    def test_identical_distance_zero(self) -> None:
        img = blank()
        draw_glyph(img, 200, 200)
        png = to_png(img)
        assert phash_distance(phash_png(png), phash_png(png)) == 0

    def test_different_content_nonzero(self) -> None:
        a = blank()
        draw_glyph(a, 100, 100)
        b = blank()
        draw_glyph(b, 900, 600)
        assert phash_distance(phash_png(to_png(a)), phash_png(to_png(b))) > 0

    def test_region_crop_equals_precrop(self) -> None:
        img = blank()
        draw_glyph(img, 300, 300)
        draw_button(img, 700, 500, 160, 48, "Other")
        region = (280, 280, 200, 120)
        x, y, w, h = region
        pre = to_png(img[y : y + h, x : x + w])
        assert phash_png(to_png(img), region=region) == phash_png(pre)

    def test_region_clamped(self) -> None:
        img = blank(200, 100)
        draw_glyph(img, 20, 20)
        # extends past the image; clamped, no exception
        assert isinstance(phash_png(to_png(img), region=(150, 50, 500, 500)), str)

    def test_empty_region_raises(self) -> None:
        with pytest.raises(ValueError):
            phash_png(to_png(blank(100, 100)), region=(500, 500, 10, 10))


# -- wait_settled -------------------------------------------------------------


class FakeBackend:
    """Backend stub returning a canned PNG sequence (last frame repeats)."""

    def __init__(self, frames: list[bytes]) -> None:
        self._frames = frames
        self.calls = 0

    @property
    def viewport(self) -> tuple[int, int]:
        return (1280, 800)

    def screenshot(self) -> bytes:
        idx = min(self.calls, len(self._frames) - 1)
        self.calls += 1
        return self._frames[idx]

    def click(self, x: int, y: int, *, double: bool = False) -> None: ...

    def type_text(self, text: str) -> None: ...

    def press(self, key: str) -> None: ...


def frame_with_block(x: int) -> bytes:
    img = blank(320, 200)
    cv2.rectangle(img, (x, 60), (x + 60, 140), (20, 20, 20), -1)
    return to_png(img)


class TestWaitSettled:
    def test_settles_on_stable_frames(self) -> None:
        changing = [frame_with_block(10), frame_with_block(120)]
        stable = frame_with_block(240)
        backend = FakeBackend(changing + [stable, stable, stable])
        out = wait_settled(backend, interval_s=0.01, stable_frames=2)
        assert out == stable
        assert backend.calls >= 4  # 2 changing + at least 2 stable

    def test_immediate_stability(self) -> None:
        stable = frame_with_block(50)
        backend = FakeBackend([stable, stable])
        out = wait_settled(backend, interval_s=0.01, stable_frames=2)
        assert out == stable
        assert backend.calls == 2

    def test_timeout_returns_last_frame(self) -> None:
        frames = [frame_with_block(10 * i) for i in range(25)]
        backend = FakeBackend(frames)
        start = time.monotonic()
        out = wait_settled(backend, interval_s=0.01, stable_frames=3, timeout_s=0.15)
        elapsed = time.monotonic() - start
        assert isinstance(out, bytes) and out  # returns *something*
        assert elapsed < 2.0  # bounded by timeout, not the frame count
        assert backend.calls > 1

    def test_timeout_logs_warning(self, caplog) -> None:
        """A never-settling screen must be diagnosable: timeout warns."""
        frames = [frame_with_block(10 * i) for i in range(25)]
        backend = FakeBackend(frames)
        with caplog.at_level(logging.WARNING, logger="openadapt_flow.vision.settle"):
            wait_settled(backend, interval_s=0.01, stable_frames=3, timeout_s=0.15)
        assert any("did not settle" in record.getMessage() for record in caplog.records)

    def test_no_warning_when_settled(self, caplog) -> None:
        stable = frame_with_block(50)
        backend = FakeBackend([stable, stable])
        with caplog.at_level(logging.WARNING, logger="openadapt_flow.vision.settle"):
            wait_settled(backend, interval_s=0.01, stable_frames=2)
        assert not [
            record
            for record in caplog.records
            if record.name == "openadapt_flow.vision.settle"
        ]
