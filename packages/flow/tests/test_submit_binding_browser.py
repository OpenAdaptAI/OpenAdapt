"""Real-Chromium tests: an unlabeled Save is bound to the form it submits.

Each test demonstrates on one page (typing into fields through the backend,
as the recorder does, then observing the control the demonstrator clicked),
then runs the click on a fresh page with a fresh backend that typed its own
values. The resolver is scripted (``FakeVision``) so each test decides exactly
which element the visual ladder lands on, including the wrong one; the
binding observation, its verification, and the delivery lease run for real in
the page.

What must never happen: a click on any button other than the demonstrated
Save. Every adversarial case asserts that nothing was clicked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pytest

from openadapt_flow.backend import StructuralResolutionRefused
from openadapt_flow.ir import ActionKind, Anchor, Step, SubmitBinding, Workflow
from openadapt_flow.runtime.replayer import Replayer
from openadapt_flow.submit_binding import (
    SubmitBindingObservation,
    decide_submit_binding,
    observation_from_page,
)
from tests.test_replayer import FakeVision, Match, make_png

sync_api = pytest.importorskip("playwright.sync_api")

pytestmark = pytest.mark.timeout(300)

# Material Design icon paths (Apache-2.0 glyph geometry): floppy-disk Save,
# trash can, and the chevron of a back button.
SAVE_ICON = (
    "M17 3H5c-1.11 0-2 .9-2 2v14c0 1.1.89 2 2 2h14c1.1 0 2-.9 2-2V7l-4-4zm-5 "
    "16c-1.66 0-3-1.34-3-3s1.34-3 3-3 3 1.34 3 3-1.34 3-3 3zm3-10H5V5h10v4z"
)
TRASH_ICON = (
    "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z"
)
BACK_ICON = "M15.41 7.41L14 6l-6 6 6 6 1.41-1.41L10.83 12z"

STYLE = """
<style>
body { font-family: Arial, sans-serif; margin: 0; }
.head { display: flex; align-items: center; height: 56px; padding: 0 24px; }
.icon { width: 40px; height: 40px; border: 0; background: transparent; }
.fields { padding: 16px 24px; display: grid; gap: 16px;
          grid-template-columns: 300px 300px; }
input { font-size: 16px; padding: 4px; }
.fab { position: fixed; bottom: 24px; }
.fab.right { right: 24px; }
.fab.left { left: 24px; }
.fab button { width: 56px; height: 56px; border-radius: 50%; border: 0;
              background: #00636b; }
</style>
"""


def _icon_button(cls: str, path: str, action: str) -> str:
    return (
        f'<button type="button" class="{cls}" '
        f"onclick=\"window.actions.push('{action}')\">"
        f'<svg width="24" height="24" viewBox="0 0 24 24"><path d="{path}">'
        "</path></svg></button>"
    )


def _fab(side: str, button: str) -> str:
    """openIMIS wraps each floating button as ``div > div > span > button``."""

    return f'<div class="fab {side}"><div><span>{button}</span></div></div>'


def claims_page(
    *,
    save_at: str = "fab-right",
    extra_icon_after_fields: bool = False,
    swap_icons: bool = False,
    replace_icon: bool = False,
) -> str:
    """openIMIS's claim form shape: a floating icon-only Save outside the form.

    The form has an unlabeled back chevron in its header (before the fields)
    and a service row whose delete icon sits in a table row. ``save_at``
    moves the Save: ``fab-right`` (as recorded), ``fab-left``, or ``header``.
    ``extra_icon_after_fields`` adds a second unlabeled icon after the fields.
    ``swap_icons`` puts a delete icon where the Save was and moves the Save
    to the left; ``replace_icon`` puts a delete icon there and drops the Save.
    """

    save = _icon_button("savefab", SAVE_ICON, "save")
    header_save = save if save_at == "header" else ""
    extra = (
        f'<div class="tools">{_icon_button("icon", TRASH_ICON, "discard")}</div>'
        if extra_icon_after_fields
        else ""
    )
    if replace_icon:
        fabs = _fab("right", _icon_button("savefab", TRASH_ICON, "discard"))
    elif swap_icons:
        fabs = _fab("right", _icon_button("savefab", TRASH_ICON, "discard")) + _fab(
            "left", save
        )
    elif save_at in ("fab-right", "fab-left"):
        fabs = _fab(save_at.split("-")[1], save)
    else:
        fabs = ""
    return f"""<!doctype html><html><head><meta charset="utf-8">{STYLE}</head>
<body><div id="page">
<form id="claim" onsubmit="return false">
  <div class="head">{_icon_button("icon back", BACK_ICON, "back")}
    <h1>Claim</h1>{header_save}</div>
  <div class="fields">
    <label>Insurance No. <input id="ins"></label>
    <label>Claim No. <input id="cno"></label>
    <label>Explanation <input id="expl"></label>
  </div>
  <table><tbody><tr>
    <td>Service</td><td><input id="svc" aria-label="Search service"></td>
    <td>{_icon_button("icon", TRASH_ICON, "row-delete")}</td>
  </tr></tbody></table>
  {extra}
</form>
{fabs}
</div><script>window.actions = [];</script></body></html>"""


def two_forms_page(*, swapped: bool = False) -> str:
    """Two forms, each with its own identical icon-only submit button."""

    def form(name: str) -> str:
        return f"""<form id="{name}" onsubmit="return false" style="padding:24px">
  <h2>{name}</h2>
  <label>Member <input id="{name}-member"></label>
  <label>Note <input id="{name}-note"></label>
  <div>{_icon_button(f"submit-{name}", SAVE_ICON, f"submit-{name}")}</div>
</form>"""

    first, second = ("payer", "provider")
    body = form(second) + form(first) if swapped else form(first) + form(second)
    return f"""<!doctype html><html><head><meta charset="utf-8">{STYLE}</head>
<body>{body}<script>window.actions = [];</script></body></html>"""


@pytest.fixture(scope="module")
def browser() -> Iterator[Any]:
    with sync_api.sync_playwright() as playwright:
        launched = playwright.chromium.launch(headless=True)
        try:
            yield launched
        finally:
            launched.close()


def _open(browser: Any, html: str) -> tuple[Any, Any]:
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    page = browser.new_page(
        viewport={"width": 1280, "height": 800}, device_scale_factor=1
    )
    page.set_content(html)
    return page, PlaywrightBackend(page)


def _fill(page: Any, backend: Any, values: dict[str, str]) -> None:
    """Type into each field through the backend, as record and replay do."""

    for selector, value in values.items():
        page.focus(selector)
        backend.type_text(value)


def _box(page: Any, selector: str) -> tuple[tuple[int, int], tuple[int, int, int, int]]:
    box = page.locator(selector).bounding_box()
    assert box is not None, selector
    point = (
        int(round(box["x"] + box["width"] / 2)),
        int(round(box["y"] + box["height"] / 2)),
    )
    region = (
        int(round(box["x"])),
        int(round(box["y"])),
        int(round(box["width"])),
        int(round(box["height"])),
    )
    return point, region


DEMO = {"#ins": "610420233", "#cno": "VD263957", "#expl": "Red skin"}
RUN = {"#ins": "610420118", "#cno": "VD264752", "#expl": "Fever"}


def _demonstrate(
    browser: Any,
    html: str,
    values: dict[str, str],
    control: str,
) -> tuple[SubmitBinding | None, str | None, Step]:
    """Fill the fields, observe the clicked control, and compile its step."""

    page, backend = _open(browser, html)
    try:
        _fill(page, backend, values)
        point, region = _box(page, control)
        raw = backend.submit_binding_at(*point)
        assert raw is not None
        binding, reason = decide_submit_binding(
            observation_from_page(raw),
            field_step_ids=tuple(f"step_{i:03d}" for i in range(len(values))),
        )
    finally:
        page.close()
    step = Step(
        id="save",
        intent="click recorded visual target",
        action=ActionKind.CLICK,
        risk="irreversible",
        anchor=Anchor(template="templates/save.png", region=region, click_point=point),
        submit_binding=binding,
        submit_binding_unbound_reason=reason,
    )
    return binding, reason, step


def _replay(
    browser: Any,
    tmp_path: Path,
    html: str,
    values: dict[str, str],
    step: Step,
    resolve_to: str,
) -> tuple[Any, list[str]]:
    """Run ``step`` on a fresh page; the visual ladder lands on ``resolve_to``."""

    page, backend = _open(browser, html)
    try:
        _fill(page, backend, values)
        point, region = _box(page, resolve_to)
        vision = FakeVision()
        vision.template_results = [
            Match(point=point, region=region, confidence=0.99) for _ in range(4)
        ]
        bundle = tmp_path / "bundle"
        (bundle / "templates").mkdir(parents=True, exist_ok=True)
        (bundle / "templates" / "save.png").write_bytes(make_png((56, 56)))
        report = Replayer(backend, vision=vision).run(
            Workflow(name="claims", steps=[step]),
            bundle_dir=bundle,
            run_dir=tmp_path / "run",
        )
        actions = page.evaluate("window.actions")
    finally:
        page.close()
    return report, actions


# -- the openIMIS shape -------------------------------------------------------


def test_floating_icon_save_is_bound_to_the_form_and_clicked(
    browser: Any, tmp_path: Path
) -> None:
    binding, reason, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")

    assert reason is None
    assert binding is not None
    # The fields share the <form>; the Save floats outside it, after them.
    assert binding.form_owner == "fields_only"
    assert binding.submit_role == "detached"
    assert binding.scope_kind == "container"
    # The same path a live probe observed on openIMIS 25.10.
    assert binding.scope_path == "div>div>span>button"
    assert binding.filled_fields == 3
    assert binding.fixed_position is True
    assert (binding.vertical, binding.horizontal) == ("below", "right")

    report, actions = _replay(
        browser, tmp_path, claims_page(), RUN, step, resolve_to=".savefab"
    )
    result = report.results[0]

    assert report.success is True, result.error
    assert result.submit_binding_verified is True
    assert result.actuation == "guarded_coordinate"
    assert result.delivery_receipt is not None
    assert actions == ["save"]


def test_the_back_icon_before_the_fields_is_never_clicked(
    browser: Any, tmp_path: Path
) -> None:
    """An unlabeled icon before the fields does not make the Save ambiguous,
    and resolving onto it refuses."""

    _, _, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")

    report, actions = _replay(
        browser, tmp_path, claims_page(), RUN, step, resolve_to=".back"
    )
    result = report.results[0]

    assert report.success is False
    assert result.safety_halt is True
    assert result.submit_binding_verified is False
    assert "does not follow the filled fields" in (result.error or "")
    assert actions == []


def test_a_row_delete_icon_is_never_clicked(browser: Any, tmp_path: Path) -> None:
    _, _, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")

    report, actions = _replay(
        browser, tmp_path, claims_page(), RUN, step, resolve_to="tr .icon"
    )

    assert report.success is False
    assert "table row" in (report.results[0].error or "")
    assert actions == []


def test_a_run_that_filled_other_fields_refuses(browser: Any, tmp_path: Path) -> None:
    _, _, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")

    fewer = {"#ins": "610420118", "#cno": "VD264752"}
    report, actions = _replay(
        browser, tmp_path, claims_page(), fewer, step, resolve_to=".savefab"
    )

    assert report.success is False
    assert "typed into 2 field(s)" in (report.results[0].error or "")
    assert actions == []


# -- two forms on a page ------------------------------------------------------


def test_two_forms_bind_the_icon_of_the_form_that_was_filled(
    browser: Any, tmp_path: Path
) -> None:
    values = {"#payer-member": "610420233", "#payer-note": "Red skin"}
    binding, reason, step = _demonstrate(
        browser, two_forms_page(), values, ".submit-payer"
    )

    assert reason is None
    assert binding is not None
    assert binding.form_owner == "shared"
    assert binding.scope_kind == "form"

    report, actions = _replay(
        browser, tmp_path, two_forms_page(), values, step, resolve_to=".submit-payer"
    )
    assert report.success is True, report.results[0].error
    assert actions == ["submit-payer"]


@pytest.mark.parametrize("swapped", [False, True])
def test_two_forms_never_click_the_other_forms_identical_icon(
    browser: Any, tmp_path: Path, swapped: bool
) -> None:
    """The visual ladder lands on the other form's identical icon (for
    example, because the forms swapped places): the binding refuses."""

    values = {"#payer-member": "610420233", "#payer-note": "Red skin"}
    _, _, step = _demonstrate(browser, two_forms_page(), values, ".submit-payer")

    report, actions = _replay(
        browser,
        tmp_path,
        two_forms_page(swapped=swapped),
        values,
        step,
        resolve_to=".submit-provider",
    )
    result = report.results[0]

    assert report.success is False
    assert result.safety_halt is True
    assert "different form" in (result.error or "")
    assert actions == []


# -- a second unlabeled icon --------------------------------------------------


def test_a_second_unlabeled_icon_at_record_time_leaves_the_save_unbound(
    browser: Any, tmp_path: Path
) -> None:
    html = claims_page(extra_icon_after_fields=True)
    binding, reason, step = _demonstrate(browser, html, DEMO, ".savefab")

    assert binding is None
    assert reason is not None and "2 unlabeled controls" in reason

    for target in (".savefab", ".tools .icon"):
        report, actions = _replay(browser, tmp_path, html, RUN, step, resolve_to=target)
        assert report.success is False
        assert report.results[0].safety_halt is True
        assert "could not be tied to the record" in (report.results[0].error or "")
        assert actions == []


@pytest.mark.parametrize("target", [".savefab", ".tools .icon"])
def test_a_second_unlabeled_icon_at_run_time_refuses(
    browser: Any, tmp_path: Path, target: str
) -> None:
    _, _, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")

    report, actions = _replay(
        browser,
        tmp_path,
        claims_page(extra_icon_after_fields=True),
        RUN,
        step,
        resolve_to=target,
    )

    assert report.success is False
    assert report.results[0].safety_halt is True
    assert actions == []


# -- a moved button -----------------------------------------------------------


@pytest.mark.parametrize(
    ("variant", "target", "reason"),
    [
        ({"save_at": "header"}, ".savefab", "does not follow the filled fields"),
        ({"save_at": "fab-left"}, ".savefab", "moved relative to the filled fields"),
        # A delete icon now sits where the Save was.
        ({"replace_icon": True}, ".fab.right button", "different icon"),
        # ... and the Save moved to the other side: two icons after the fields.
        ({"swap_icons": True}, ".fab.right button", "2 unlabeled controls"),
        ({"swap_icons": True}, ".fab.left button", "2 unlabeled controls"),
    ],
)
def test_a_moved_button_refuses(
    browser: Any,
    tmp_path: Path,
    variant: dict[str, Any],
    target: str,
    reason: str,
) -> None:
    _, _, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")

    report, actions = _replay(
        browser, tmp_path, claims_page(**variant), RUN, step, resolve_to=target
    )
    result = report.results[0]

    assert report.success is False
    assert result.safety_halt is True
    assert reason in (result.error or ""), result.error
    assert actions == []


# -- the delivery lease -------------------------------------------------------


@pytest.mark.parametrize("change", ["second_icon", "replace_field", "move"])
def test_a_change_after_the_binding_is_verified_refuses_delivery(
    browser: Any, change: str
) -> None:
    """The verified observation is leased: if the page changes between the
    check and the click, the click is refused."""

    import hashlib

    _, _, step = _demonstrate(browser, claims_page(), DEMO, ".savefab")
    assert step.submit_binding is not None
    page, backend = _open(browser, claims_page())
    try:
        _fill(page, backend, RUN)
        point, _ = _box(page, ".savefab")
        backend.arm_guarded_submit_coordinate(*point, binding=step.submit_binding)
        if change == "second_icon":
            page.evaluate(
                """() => {
                    const extra = document.createElement('button');
                    extra.type = 'button';
                    extra.innerHTML = '<svg width="24" height="24"></svg>';
                    extra.onclick = () => window.actions.push('discard');
                    document.querySelector('#claim').appendChild(extra);
                }"""
            )
        elif change == "replace_field":
            page.evaluate(
                """() => {
                    const field = document.querySelector('#cno');
                    field.replaceWith(field.cloneNode(true));
                }"""
            )
        else:
            page.evaluate(
                "() => { document.querySelector('.fab').style.bottom = '400px'; }"
            )
        with pytest.raises(StructuralResolutionRefused):
            backend.act_guarded_coordinate(
                *point,
                expected_frame_sha256=hashlib.sha256(b"unused").hexdigest(),
            )
        actions = page.evaluate("window.actions")
    finally:
        page.close()

    assert actions == []


def test_record_time_observation_retains_no_text(browser: Any) -> None:
    page, backend = _open(browser, claims_page())
    try:
        _fill(page, backend, DEMO)
        point, _ = _box(page, ".savefab")
        raw = backend.submit_binding_at(*point)
        labelled_point, _ = _box(page, "h1")
        labelled = backend.submit_binding_at(*labelled_point)
    finally:
        page.close()

    assert raw is not None
    text = repr(raw)
    for value in DEMO.values():
        assert value not in text
    assert "Claim" not in text and "Insurance" not in text
    assert set(raw) <= set(SubmitBindingObservation.model_fields)
    # No actionable control under a heading.
    assert labelled is None
