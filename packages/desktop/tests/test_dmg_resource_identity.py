"""Complete synthetic resource identity; never run native resource queries."""

import importlib.util
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "dmg_resource_identity", Path(__file__).resolve().parents[1] / "scripts/diagnose_dmg_mounts.py"
)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


SHAPES = [
    [("/dev/disk7", None)],
    [("/dev/disk7s1", "/Volumes/dmg.A")],
    [("/dev/disk7", None), ("/dev/disk7s1", "/Volumes/dmg.A")],
    [
        ("/dev/disk7", None),
        ("/dev/disk7s1", "/Volumes/dmg.A"),
        ("/dev/disk7s2", "/Volumes/dmg.B"),
    ],
]


def image(root, entities, *, name="rw.41.synthetic.dmg"):
    return {
        "image-path": str(root / name),
        "system-entities": [
            {"dev-entry": device, **({"mount-point": mount} if mount is not None else {})}
            for device, mount in entities
        ],
    }


class Probes:
    def __init__(self, before, after):
        self.snapshots = iter([before, after])
        self.calls = []

    def __call__(self, argv, *, timeout):
        assert 0 < timeout <= 3
        self.calls.append(argv)
        if argv == diag.INFO:
            return SimpleNamespace(
                returncode=0, stdout=plistlib.dumps({"images": next(self.snapshots)})
            )
        assert argv[:3] == diag.LSOF
        return SimpleNamespace(returncode=0, stdout=b"p123\ncScopedObserver\nu501\nnprivate-file\n")


@pytest.mark.parametrize("entities", SHAPES, ids=["unmounted", "mounted", "mixed", "slices"])
def test_all_entity_mutations_follow_one_stability_model(tmp_path, entities):
    # Equality is over the complete set, independent of entity presentation order.
    variants = [list(entities), list(reversed(entities))]
    for index, (device, mount) in enumerate(entities):
        changed_device = list(entities)
        changed_device[index] = (f"/dev/disk9{index}", mount)
        variants.append(changed_device)
        changed_mount = list(entities)
        changed_mount[index] = (device, f"/Volumes/dmg.CHANGED{index}")
        variants.append(changed_mount)
        if mount is not None:
            unmounted = list(entities)
            unmounted[index] = (device, None)
            variants.append(unmounted)
        if len(entities) > 1:
            variants.append(entities[:index] + entities[index + 1 :])
    variants.append([*entities, ("/dev/disk99", None)])
    for after in variants:
        probes = Probes([image(tmp_path, entities)], [image(tmp_path, after)])
        report = diag.collect(tmp_path, run=probes)
        stable = set(after) == set(entities)
        assert report["cleanup_authorized"] is False
        assert report["outcome"] == ("observed" if stable else "incomplete")
        if stable:
            handles = report["images"][0]["handles"]
            assert len(handles) == 1 + sum(mount is not None for _, mount in entities)
            assert all(handle["processes"][0]["command"] == "ScopedObserver" for handle in handles)
        else:
            assert report["reason"] == "resource_identity_changed"
            assert report["images"] == []
            assert "ScopedObserver" not in json.dumps(report)
        assert "private-file" not in json.dumps(report)
        # No unmounted-device query is invented; only backing path and mounts are queried.
        assert probes.calls[0] == probes.calls[-1] == diag.INFO
        assert len(probes.calls) == 3 + sum(mount is not None for _, mount in entities)
        expected = {
            tuple([*diag.LSOF, "--", str(tmp_path / "rw.41.synthetic.dmg")]),
            *(tuple([*diag.LSOF, "+f", "--", mount]) for _, mount in entities if mount),
        }
        assert {tuple(argv) for argv in probes.calls[1:-1]} == expected


INVALID_ENTITIES = [
    [],
    "not-a-list",
    [{}],
    [{"mount-point": "/Volumes/dmg.A"}],
    [{"dev-entry": 7}],
    [{"dev-entry": "/dev/disk7"}, {"dev-entry": "/dev/disk7"}],
    [
        {"dev-entry": "/dev/disk7s1", "mount-point": "/Volumes/dmg.A"},
        {"dev-entry": "/dev/disk7s1", "mount-point": "/Volumes/dmg.A"},
    ],
    [
        {"dev-entry": "/dev/disk7"},
        {"dev-entry": "/dev/disk7", "mount-point": "/Volumes/dmg.A"},
    ],
    [
        {"dev-entry": "/dev/disk7s1", "mount-point": "/Volumes/dmg.A"},
        {"dev-entry": "/dev/disk8s1", "mount-point": "/Volumes/dmg.A"},
    ],
    [{"dev-entry": f"/dev/disk{index}"} for index in range(9)],
]


@pytest.mark.parametrize("entities", INVALID_ENTITIES)
@pytest.mark.parametrize("invalid_after", [False, True], ids=["before", "after"])
def test_incomplete_or_duplicate_scoped_entities_never_credit_attribution(
    tmp_path, entities, invalid_after
):
    valid = image(tmp_path, SHAPES[2])
    invalid = {**valid, "system-entities": entities}
    probes = Probes([valid] if invalid_after else [invalid], [invalid])
    report = diag.collect(tmp_path, run=probes)
    assert report["outcome"] == "incomplete" and report["images"] == []
    assert report["cleanup_authorized"] is False
    assert "ScopedObserver" not in json.dumps(report)
    assert len(probes.calls) == (4 if invalid_after else 1)


@pytest.mark.parametrize("duplicate_mount", [False, True], ids=["device", "mount"])
@pytest.mark.parametrize("invalid_after", [False, True], ids=["before", "after"])
def test_ambiguous_identity_across_images_is_incomplete(tmp_path, duplicate_mount, invalid_after):
    first = image(tmp_path, SHAPES[1])
    distinct = image(tmp_path, [("/dev/disk8s1", "/Volumes/dmg.B")], name="rw.42.second.dmg")
    duplicate = image(
        tmp_path,
        [("/dev/disk8s1", "/Volumes/dmg.A")]
        if duplicate_mount
        else [("/dev/disk7s1", "/Volumes/dmg.B")],
        name="rw.42.second.dmg",
    )
    probes = Probes([first, distinct] if invalid_after else [first, duplicate], [first, duplicate])
    report = diag.collect(tmp_path, run=probes)
    assert report["outcome"] == "incomplete" and report["images"] == []
    assert "ScopedObserver" not in json.dumps(report)
    assert len(probes.calls) == (6 if invalid_after else 1)


@pytest.mark.parametrize("final_time", [7.0, 9.0], ids=["within-budget", "late-final-query"])
def test_final_snapshot_budget_is_checked_before_crediting_all_images(tmp_path, final_time):
    now = [0.0]
    images = [
        image(tmp_path, SHAPES[2]),
        image(tmp_path, [("/dev/disk8", None)], name="rw.42.second.dmg"),
    ]
    probes = Probes(images, images)

    def run(argv, *, timeout):
        result = probes(argv, timeout=timeout)
        if sum(call == diag.INFO for call in probes.calls) == 2:
            now[0] = final_time
        return result

    report = diag.collect(tmp_path, run=run, clock=lambda: now[0])
    assert report["cleanup_authorized"] is False
    if final_time < diag.BUDGET_SECONDS:
        assert report["outcome"] == "observed" and len(report["images"]) == 2
    else:
        assert report["reason"] == "diagnostic_budget_exhausted"
        assert report["images"] == [] and "ScopedObserver" not in json.dumps(report)
