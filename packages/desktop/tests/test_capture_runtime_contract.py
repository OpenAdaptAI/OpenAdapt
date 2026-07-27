"""Contract between the Desktop engine and the installed Capture runtime.

This file used to assert only that ``openadapt_capture.Recorder`` exposed the
members Desktop calls. That is a *shape* contract, and it stayed green through
the exact skew it looks like it should have caught: Desktop's lock resolved
``openadapt-capture`` 1.1.1 while the Flow runtime frozen into the same
installer declared ``openadapt-capture>=1.2.0`` for its ``capture`` extra.
Nothing failed, because 1.1.1's ``Recorder`` has precisely the shape Desktop
uses, and because Desktop depends on ``openadapt-capture`` directly rather than
on ``openadapt-flow[capture]`` -- so no resolver ever compared the two numbers.

What actually breaks is a *behaviour* two layers down: below 1.2.0 the producer
never emits ``key.shortcut``, so Flow's capture adapter refuses every
demonstration containing a modifier chord ("keyboard shortcut 'ctrl+s' has no
flow equivalent") -- after the operator has already performed the workflow.
The tests below therefore pin the version floor from both sides *and* convert a
real Ctrl+S through both installed libraries.
"""

from __future__ import annotations

import inspect
import tomllib
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

import pytest
from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]

FLOW_DISTRIBUTION = "openadapt-flow"
CAPTURE_DISTRIBUTION = "openadapt-capture"
#: Flow's extra whose declared floor is the authoritative producer contract.
CAPTURE_CONSUMER_EXTRA = "capture"
#: The first capture release with no hosted transcription path at all. 1.2.0
#: and everything before it resolved ``capture transcribe``'s default ``auto``
#: backend to a hosted recognizer and uploaded the raw microphone waveform.
CAPTURE_ON_DEVICE_AUDIO_FLOOR = Version("1.2.1")


def _canonical(name: str) -> str:
    return name.replace("_", "-").lower()


def _requirements(distribution_name: str) -> list[Requirement]:
    dist = distribution(distribution_name)
    parsed: list[Requirement] = []
    for raw in dist.requires or ():
        try:
            parsed.append(Requirement(raw))
        except InvalidRequirement:  # pragma: no cover - defensive
            continue
    return parsed


def _lower_bound(requirement: Requirement) -> Version:
    bounds = [
        Version(specifier.version)
        for specifier in requirement.specifier
        if specifier.operator == ">="
    ]
    assert bounds, f"{requirement} declares no lower bound"
    return max(bounds)


def _flow_capture_floor() -> Version:
    """The capture floor the *installed* bundled Flow runtime declares."""

    try:
        requirements = _requirements(FLOW_DISTRIBUTION)
    except PackageNotFoundError:
        pytest.skip(
            "the bundled openadapt-flow runtime is not installed; the "
            "qualification-contract job runs this file with --extra build"
        )
    for requirement in requirements:
        if _canonical(requirement.name) != CAPTURE_DISTRIBUTION:
            continue
        if CAPTURE_CONSUMER_EXTRA not in str(requirement.marker or ""):
            continue
        return _lower_bound(requirement)
    pytest.fail(
        "the bundled openadapt-flow runtime declares no openadapt-capture "
        f"floor for its {CAPTURE_CONSUMER_EXTRA!r} extra; the contract this "
        "file guards has moved and these assertions need rewriting"
    )


def _desktop_declared_capture_floor() -> Version:
    """The floor Desktop's own metadata promises, read from pyproject."""

    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for raw in pyproject["project"]["dependencies"]:
        requirement = Requirement(raw)
        if _canonical(requirement.name) == CAPTURE_DISTRIBUTION:
            return _lower_bound(requirement)
    pytest.fail("Desktop no longer declares an openadapt-capture dependency")


def _installed_capture_version() -> Version:
    return Version(distribution(CAPTURE_DISTRIBUTION).version)


def test_installed_capture_exposes_native_recorder_contract() -> None:
    """The packaged dependency must expose the recorder Desktop invokes."""

    from openadapt_capture import Recorder

    assert Recorder is not None, "openadapt-capture installed without a usable Recorder"
    parameters = inspect.signature(Recorder).parameters
    assert "capture_dir" in parameters
    assert "task_description" in parameters
    for member in ("__enter__", "__exit__", "wait_for_ready", "stop", "event_count"):
        assert hasattr(Recorder, member), f"Recorder is missing {member}"


def test_declared_capture_floor_covers_the_bundled_flow_requirement() -> None:
    """Desktop's own floor may never sit below the bundled Flow's.

    Desktop installs ``openadapt-capture`` directly, so pip/uv will happily
    resolve a version the frozen Flow runtime considers insufficient. This is
    the assertion that makes raising Flow's floor a loud failure here rather
    than a silent one inside a signed installer.
    """

    flow_floor = _flow_capture_floor()
    declared = _desktop_declared_capture_floor()
    assert declared >= flow_floor, (
        f"Desktop declares openadapt-capture>={declared} but the bundled "
        f"openadapt-flow requires >={flow_floor} for its "
        f"{CAPTURE_CONSUMER_EXTRA!r} extra"
    )


def test_resolved_capture_satisfies_the_bundled_flow_requirement() -> None:
    """The version actually locked -- and therefore frozen -- clears the floor."""

    flow_floor = _flow_capture_floor()
    installed = _installed_capture_version()
    assert installed >= flow_floor, (
        f"openadapt-capture {installed} is resolved here, but the bundled "
        f"openadapt-flow requires >={flow_floor}; the installer would ship a "
        "capture runtime its own engine declares insufficient"
    )


def test_resolved_capture_has_no_hosted_transcription_path() -> None:
    """The installer must not carry a build that can upload a waveform.

    Every release before 1.2.1 resolved the default ``auto`` transcription
    backend to a hosted recognizer when no local engine was importable, which
    uploaded the raw microphone waveform. A voice is itself identifying, so
    there is no sanitized derivative and no way to make that upload safe after
    the fact -- the version floor is the control.
    """

    installed = _installed_capture_version()
    assert installed >= CAPTURE_ON_DEVICE_AUDIO_FLOOR, (
        f"openadapt-capture {installed} predates the on-device-only audio "
        f"contract added in {CAPTURE_ON_DEVICE_AUDIO_FLOOR}"
    )

    from openadapt_capture import audio

    assert audio._get_best_transcription_backend() in (
        None,
        *audio.LOCAL_TRANSCRIPTION_BACKENDS,
    )
    with pytest.raises(ValueError):
        audio.resolve_transcription_backend("api")


def test_demonstrated_modifier_chord_survives_conversion_to_flow() -> None:
    """The behaviour the floor exists for, exercised end to end.

    Runs the installed capture's own event-processing pipeline over a raw
    Ctrl+S key sequence and feeds the result to the installed Flow's capture
    adapter -- exactly the two hops Desktop's native record path performs once
    the operator stops recording. Below the floor this raises instead of
    converting, and it raises only after the demonstration is over.
    """

    pytest.importorskip(
        "openadapt_flow",
        reason="the bundled Flow runtime is installed by the build extra",
    )

    from openadapt_capture.capture import Action
    from openadapt_capture.events import KeyDownEvent, KeyUpEvent
    from openadapt_capture.processing import process_events
    from openadapt_flow.adapters.capture import _flow_events

    processed = process_events(
        [
            KeyDownEvent(timestamp=1.00, key_name="ctrl"),
            KeyDownEvent(timestamp=1.05, key_char="s"),
            KeyUpEvent(timestamp=1.10, key_char="s"),
            KeyUpEvent(timestamp=1.15, key_name="ctrl"),
        ]
    )
    actions = [Action(event=event, _capture=None) for event in processed]

    events = _flow_events(actions, 1.0, {}, include_structural=False)

    assert [event["kind"] for event in events] == ["hotkey"], (
        "the installed capture/Flow pair did not preserve a demonstrated "
        f"Ctrl+S as a hotkey: {events}"
    )
    assert events[0]["modifiers"] == ["ctrl"]
    assert events[0]["key"] == "s"
