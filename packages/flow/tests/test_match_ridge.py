"""A self-similar template resolves to the stripe where it was recorded.

Fixture: OpenEMR's collapsed accordion headers rendered over RDP (no patient
data), cropped from a zero-DOM recording. The click on the 'Contact' bar
captured a textless template that also matches the gap below 'Choices'.
"""

from pathlib import Path

import cv2

from openadapt_flow.vision.match import find_template

FIXTURE = (
    Path(__file__).parent / "fixtures" / "match_ridge" / "openemr_accordion_rdp.png"
)
# The fixture is the frame region x 394..874, y 330..660.
OX, OY = 394, 330


def test_ridge_template_prefers_the_recorded_stripe():
    screen = cv2.imread(str(FIXTURE), cv2.IMREAD_GRAYSCALE)
    tx, ty = 554 - OX, 474 - OY
    template = screen[ty : ty + 64, tx : tx + 160].copy()
    png = lambda a: cv2.imencode(".png", a)[1].tobytes()  # noqa: E731
    match = find_template(
        png(screen),
        png(template),
        search_region=(474 - OX, 394 - OY, 320, 224),
        threshold=0.8,
        prefer_near=(tx, ty),
    )
    assert match is not None
    assert (match.region[0], match.region[1]) == (tx, ty)
