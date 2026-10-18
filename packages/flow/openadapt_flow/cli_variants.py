"""CLI handlers for ``variants``, ``route``, and ``run`` on a variant set."""

from __future__ import annotations

import argparse
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from openadapt_flow.variants import (
    VariantInput,
    VariantSet,
    VariantSetError,
    VariantWhen,
    add_variant,
    author_variant_set,
)


def _when_from_args(args: argparse.Namespace) -> VariantWhen:
    inputs: dict[str, list[str]] = {}
    for raw in getattr(args, "when_input", None) or []:
        if "=" not in raw:
            raise SystemExit(f"--when-input expects NAME=VALUE, got {raw!r}")
        name, value = raw.split("=", 1)
        inputs.setdefault(name.strip(), []).append(value)
    try:
        return VariantWhen(
            inputs=inputs,
            theme=getattr(args, "when_theme", None),
            text_present=list(getattr(args, "when_text", None) or []),
            text_absent=list(getattr(args, "when_no_text", None) or []),
        )
    except ValueError as exc:
        raise SystemExit(f"variant conditions are invalid: {exc}") from exc


def _variant_from_spec(entry: dict, base: Path) -> VariantInput:
    try:
        return VariantInput(
            name=entry["name"],
            bundle=(base / entry["bundle"]).resolve(),
            admission=(base / entry["admission"]).resolve(),
            when=VariantWhen.model_validate(entry.get("when") or {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise SystemExit(f"variant spec entry is invalid: {exc}") from exc


def cmd_variants(args: argparse.Namespace) -> int:
    action = getattr(args, "variants_command", None)
    if action == "author":
        spec_path = Path(args.spec)
        try:
            spec = json.loads(spec_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SystemExit(f"variant spec {spec_path} is unreadable: {exc}") from exc
        if not isinstance(spec, dict) or not isinstance(spec.get("variants"), list):
            raise SystemExit("variant spec needs a 'variants' list")
        base = spec_path.resolve().parent
        variants = [_variant_from_spec(entry, base) for entry in spec["variants"]]
        name = getattr(args, "name", None) or spec.get("name") or Path(args.out).name
        try:
            variant_set = author_variant_set(variants, name=name, out=Path(args.out))
        except VariantSetError as exc:
            raise SystemExit(f"variants REFUSED: {exc}") from exc
        print(
            f"Authored variant set {variant_set.name!r} at {args.out}: "
            f"{len(variant_set.variants)} admitted variant(s). Each keeps its own "
            "QualificationAdmissionEnvelope; `run` routes before it executes."
        )
        return 0
    if action == "add":
        if "=" not in args.variant:
            raise SystemExit("--variant expects NAME=BUNDLE")
        name, bundle = args.variant.split("=", 1)
        try:
            item = VariantInput(
                name=name.strip(),
                bundle=Path(bundle).resolve(),
                admission=Path(args.admission).resolve(),
                when=_when_from_args(args),
                derived_from=getattr(args, "derived_from", None),
            )
            variant_set = add_variant(Path(args.variant_set), item)
        except VariantSetError as exc:
            raise SystemExit(f"variants add REFUSED: {exc}") from exc
        except ValueError as exc:
            raise SystemExit(f"variants add REFUSED: {exc}") from exc
        print(
            f"Added variant {item.name!r} to {variant_set.name!r}: "
            f"{len(variant_set.variants)} admitted variant(s)."
        )
        return 0
    if action == "show":
        try:
            variant_set = VariantSet.load(Path(args.variant_set))
        except VariantSetError as exc:
            raise SystemExit(str(exc)) from exc
        print(variant_set.model_dump_json(indent=2, exclude_none=True))
        return 0
    raise SystemExit("variants needs a subcommand: author, add, or show")


def _launched_web_screen(url: str) -> bytes:
    from playwright.sync_api import sync_playwright

    from openadapt_flow._browser_setup import launch_chromium
    from openadapt_flow.backends.playwright_backend import PlaywrightBackend

    with sync_playwright() as p:
        browser = launch_chromium(p.chromium, headless=True)
        try:
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto(url)
            return PlaywrightBackend(page).screenshot()
        finally:
            browser.close()


def first_screen_probe(
    args: argparse.Namespace, variant_set: VariantSet, parent: Path
) -> Callable[[], Optional[bytes]]:
    """Return a callable that captures the first screen the run would see.

    ``--first-screen PNG`` wins. Otherwise the probe reads one frame through
    the same backend selection ``run`` uses: the attached signed-in browser
    tab, the native or remote-display window, or a freshly launched browser
    at ``--url`` (the bundled MockMed app when no URL is given). It only
    reads; it never clicks or types.
    """

    def capture() -> Optional[bytes]:
        explicit = getattr(args, "first_screen", None)
        if explicit:
            return Path(explicit).read_bytes()
        from openadapt_flow.__main__ import (
            _attached_web_page,
            _deployment_sections,
            _resolve_backend_config,
            _with_drift,
        )
        from openadapt_flow.admitted_composition import resolve_pointer
        from openadapt_flow.backends.factory import _normalize_kind, build_backend
        from openadapt_flow.ir import Workflow

        workflow = Workflow.load(
            resolve_pointer(parent, variant_set.variants[0].bundle)
        )
        cfg, _effects, _actuation = _deployment_sections(args)
        backend_cfg = _resolve_backend_config(args, cfg, workflow)
        if _normalize_kind(backend_cfg.kind) != "web":
            return build_backend(backend_cfg).screenshot()
        url = getattr(args, "url", None) or cfg.backend.url
        if backend_cfg.browser_cdp_endpoint or backend_cfg.browser_page_url:
            with _attached_web_page(backend_cfg, url=url, workflow=workflow) as page:
                return build_backend(backend_cfg, page=page).screenshot()
        if url is None:
            from openadapt_flow.mockmed.server import serve

            served, stop = serve(port=0)
            try:
                return _launched_web_screen(
                    _with_drift(served, getattr(args, "drift", None))
                )
            finally:
                stop()
        return _launched_web_screen(url)

    return capture


def _params(args: argparse.Namespace) -> dict[str, object]:
    from openadapt_flow.__main__ import _replay_params

    return dict(
        _replay_params(getattr(args, "param", None), getattr(args, "params_file", None))
    )


def cmd_route(args: argparse.Namespace) -> int:
    """Print the routing decision for one item. Executes nothing."""

    from openadapt_flow.runtime.variant_router import route_variant

    parent = Path(args.variant_set)
    try:
        variant_set = VariantSet.load(parent)
    except VariantSetError as exc:
        raise SystemExit(str(exc)) from exc
    decision = route_variant(
        variant_set,
        inputs=_params(args),
        first_screen=first_screen_probe(args, variant_set, parent),
    )
    print(decision.model_dump_json(indent=2, exclude_none=True))
    if decision.outcome == "ROUTED":
        return 0
    return 2


def _load_trust(parent: Path, args: argparse.Namespace) -> Optional[dict]:
    from openadapt_flow.qualification_admission import load_qualification_signer_trust

    explicit = getattr(args, "qualification_trust", None)
    env = os.environ.get("OPENADAPT_QUALIFICATION_TRUST")
    sibling = parent / "qualification-trust.json"
    path = Path(str(explicit)) if explicit else Path(env) if env else sibling
    if not path.is_file():
        print(
            "run REFUSED: a variant set needs a qualification signer trust "
            "registry to verify the chosen variant's admission: pass "
            "--qualification-trust, set OPENADAPT_QUALIFICATION_TRUST, or place "
            "qualification-trust.json beside variant-set.json. Nothing was executed."
        )
        return None
    try:
        return load_qualification_signer_trust(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(
            f"run REFUSED: qualification signer trust registry is invalid: {exc}. "
            "Nothing was executed."
        )
        return None


def cmd_run_variants(args: argparse.Namespace, *, run_child) -> int:
    """Route one item to a variant, verify its admission, run it governed."""

    from openadapt_flow.cli_process import bind_process_child_run
    from openadapt_flow.runtime.variant_router import (
        VARIANT_REPORT_FILENAME,
        execute_variant_set,
    )

    parent = Path(args.bundle)
    try:
        variant_set = VariantSet.load(parent)
    except VariantSetError as exc:
        print(f"run REFUSED: {exc}. Nothing was executed.")
        return 2
    run_dir = (
        Path(args.run_dir)
        if getattr(args, "run_dir", None)
        else Path("runs") / "variants"
    )
    revoked_raw = os.environ.get("OPENADAPT_REVOKED_ADMISSION_IDS", "")
    revoked = {item.strip() for item in revoked_raw.split(",") if item.strip()}
    trust = _load_trust(parent, args)
    if trust is None:
        return 2
    report = execute_variant_set(
        variant_set,
        parent_dir=parent,
        run_dir=run_dir,
        inputs=_params(args),
        first_screen=first_screen_probe(args, variant_set, parent),
        child_run=bind_process_child_run(args, run_child),
        trusted_signers=trust,
        revoked_admission_ids=revoked,
        now=datetime.now(timezone.utc),
    )
    report_path = run_dir / VARIANT_REPORT_FILENAME
    if report.child is None:
        print(f"Variant router HALTED ({report.reason}): {report.detail}")
        print(f"Nothing was executed. Decision: {report_path}")
        return 2
    print(
        f"Variant {report.decision.variant!r} ran: {report.outcome}. "
        f"Report: {report_path}"
    )
    if getattr(args, "profile", None) in {"standard", "regulated"}:
        return 0 if report.outcome == "VERIFIED" else 1
    return 0 if report.child.success else 1


def refuse_replay_variants(bundle: Path) -> None:
    from openadapt_flow.variants import is_variant_set_artifact

    if is_variant_set_artifact(bundle):
        raise SystemExit(
            "replay refuses a variant set; use `openadapt-flow run` so the "
            "router picks one admitted variant and verifies its admission"
        )
