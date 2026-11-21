"""versions.judge: a positive control (correct on main)."""

from __future__ import annotations

from typing import Any

from openadapt_flow.runtime.effects.versions import (
    FrameVersions,
    judge_versions,
    seal_versions,
)
from tests.failopen.harness import Seed, SiteSpec, key, register

KEY = b"k" * 32
SPEC = FrameVersions(read={}, record_field="rid", version_field="v")


def _run(ev: dict[str, Any]) -> Any:
    stored = seal_versions(SPEC, ev["before"], KEY)
    targets = [tuple(t) for t in ev["targets"]]
    return judge_versions(SPEC, stored, ev["after"], targets, KEY)


def _good() -> dict[str, Any]:
    return {
        "before": [{"rid": "r1", "v": "1"}],
        "after": [{"rid": "r1", "v": "1"}, {"rid": "r1", "v": "2"}],
        "targets": [["r1", True, True]],
    }


def _raise(ev: dict[str, Any], exc: BaseException) -> dict[str, Any]:
    # The after-read failed: the caller passes None, as verify_run does.
    return {**ev, "after": None}


register(
    SiteSpec(
        id="versions.judge",
        classes="C1 C5",
        target="openadapt_flow.runtime.effects.versions:judge_versions",
        run=_run,
        is_pass=lambda out: out.status == "held",
        seeds=lambda: [
            Seed(_good(), True),
            Seed(
                {
                    **_good(),
                    "after": [{"rid": "r1", "v": "1"}, {"rid": "r1", "v": "3"}],
                },
                False,
            ),
            Seed({**_good(), "after": [{"rid": "r1", "v": "1"}]}, False),
        ],
        items=key("after"),
        plan=key("targets"),
        vacuous=(
            "no target existed before the write (all creates), so no earlier "
            "version can be overwritten"
        ),
        bad_item=lambda row: {**row, "v": "9"},
        probe_raise=_raise,
        values=(key("v", "after", 1),),
        equivalent=frozenset({"number for string"}),
        notes={"number for string": "a version id 2 and '2' name the same version"},
    )
)
