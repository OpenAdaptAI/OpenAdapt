"""Run only in the exact historical release checkout, before current-loader tests."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw

import openadapt_flow
from openadapt_flow import bundle_validation as bundles
from openadapt_flow.ir import (
    ActionKind,
    Anchor,
    BundleManifest,
    BundleProvenance,
    ProgramGraph,
    State,
    StateKind,
    Step,
    StructuralLocator,
    Transition,
    Workflow,
)

FIXED_TIME = "2026-01-02T03:04:05+00:00"


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source_root.resolve()
    expected = json.loads(args.source_manifest.read_text())
    assert openadapt_flow.__version__ == expected["release"]
    assert (
        Path(openadapt_flow.__file__).resolve().is_relative_to(source / "packages/flow")
    )
    for relative, digest in expected["source_hashes"].items():
        assert sha(source / relative) == digest, relative
    args.output.mkdir(parents=True, exist_ok=False)
    summaries = []
    for shape in ("linear", "program"):
        bundle = args.output / shape
        (bundle / "templates").mkdir(parents=True)
        image = Image.new("RGB", (80, 40), "#f0f0f0")
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 79, 39), outline="#555555")
        draw.text((25, 12), "Apply", fill="black")
        image.save(bundle / "templates/button.png")
        step = Step(
            id="apply",
            intent="apply synthetic change",
            action=ActionKind.CLICK,
            anchor=Anchor(
                template="templates/button.png",
                region=(40, 50, 80, 40),
                click_point=(80, 70),
                ocr_text="Apply",
                structural=StructuralLocator(selector="#apply"),
            ),
        )
        values = {
            "name": "release-compatibility",
            "created_at": FIXED_TIME,
            "viewport": (320, 200),
            "manifest": BundleManifest(
                provenance=BundleProvenance(created_at=FIXED_TIME)
            ),
        }
        if shape == "linear":
            workflow = Workflow(steps=[step], **values)
        else:
            workflow = Workflow(
                program=ProgramGraph(
                    entry="apply",
                    states={
                        "apply": State(
                            id="apply",
                            kind=StateKind.ACTION,
                            step=step,
                            transitions=[Transition(target="done")],
                        ),
                        "done": State(
                            id="done", kind=StateKind.TERMINAL, outcome="success"
                        ),
                    },
                ),
                **values,
            )
        workflow.save(bundle)
        canonical = json.dumps(
            {
                "workflow": bundles._workflow_content(workflow),
                "files": dict(sorted(workflow.manifest.file_hashes.items())),
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        assert digest == workflow.manifest.content_digest
        # Validate with the producing release, not with the implementation under test.
        loaded = Workflow.load(bundle)
        bundles.verify_integrity(loaded, bundle, loaded.manifest)
        assert loaded.manifest.content_digest == digest
        (bundle / "canonical.json").write_bytes(canonical)
        summary = {
            "shape": shape,
            "content_digest": digest,
            "files": {
                path.relative_to(bundle).as_posix(): sha(path)
                for path in sorted(bundle.rglob("*"))
                if path.is_file()
            },
        }
        summaries.append(summary)
    (args.output / "provenance.json").write_text(
        json.dumps(
            {
                **expected,
                "generator_sha256": sha(Path(__file__)),
                "python": sys.version.split()[0],
                "pydantic": importlib.metadata.version("pydantic"),
                "original_release_load_verified": True,
                "bundles": summaries,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
