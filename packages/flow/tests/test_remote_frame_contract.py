from __future__ import annotations

import hashlib
import io

import pytest
from PIL import Image, ImageDraw

from openadapt_flow.remote_frame_contract import RemoteFrameContract


def _png(clock: int, *, target: int = 0, identity: int = 0) -> bytes:
    image = Image.new("RGB", (100, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((80, 0, 99, 15), fill=(clock, 0, 0))
    draw.text((80, 0), str(clock), fill="white")
    draw.rectangle((0, 20, 40, 60), fill=(target, 0, 0))
    draw.rectangle((45, 20, 75, 60), fill=(0, identity, 0))
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def _contract() -> RemoteFrameContract:
    return RemoteFrameContract(
        frame_width=100,
        frame_height=80,
        volatile_regions=((80, 0, 20, 16),),
        protected_regions=((0, 20, 40, 40), (45, 20, 30, 40)),
    )


def test_clock_only_change_matches_only_in_derived_contract_input() -> None:
    contract = _contract()
    first, second = _png(1), _png(2)
    assert hashlib.sha256(first).digest() != hashlib.sha256(second).digest()
    assert contract.comparison_digest(first) == contract.comparison_digest(second)


@pytest.mark.parametrize("field", ["target", "identity"])
def test_protected_changes_do_not_match(field: str) -> None:
    kwargs = {field: 200}
    assert _contract().comparison_digest(_png(1)) != _contract().comparison_digest(
        _png(2, **kwargs)
    )


def test_overlap_and_geometry_mismatch_fail_closed() -> None:
    with pytest.raises(ValueError, match="overlaps"):
        RemoteFrameContract(
            frame_width=100,
            frame_height=80,
            volatile_regions=((0, 0, 10, 10),),
            protected_regions=((0, 0, 10, 10),),
        )
    with pytest.raises(ValueError, match="geometry"):
        _contract().require_geometry((99, 80))
    with pytest.raises(ValueError, match="schema_version"):
        RemoteFrameContract.model_validate(
            {**_contract().model_dump(), "schema_version": "unsupported/v1"}
        )


def test_runtime_target_or_identity_overlap_refuses_after_static_review() -> None:
    contract = _contract()
    with pytest.raises(ValueError, match="runtime protected"):
        contract.arm(((80, 0, 10, 10),))
