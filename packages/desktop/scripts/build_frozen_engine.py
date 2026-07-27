#!/usr/bin/env python3
"""Build the self-contained Desktop engine + pinned Flow runtime.

The product runtime is frozen into the same executable as the Desktop engine,
then invoked in a separate process mode.  Deliberately do not use
``--collect-all openadapt_flow``: the public Flow wheel also carries research
and evaluation modules that are neither needed by Desktop nor permitted across
the open-core crown-jewel artifact boundary.
"""

from __future__ import annotations

import argparse
import os
import platform as platform_module
import re
import shutil
import subprocess
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

try:
    from scripts.frozen_notices import NOTICE_BUNDLE_MEMBER, prepare_notice_bundle
except ModuleNotFoundError:  # pragma: no cover - direct ``python scripts/...`` use
    from frozen_notices import NOTICE_BUNDLE_MEMBER, prepare_notice_bundle

ROOT = Path(__file__).resolve().parents[1]

# Defense in depth.  Static analysis of the product CLI should not pull these
# modules in, and the artifact audit independently refuses them if it ever does.
EXCLUDED_MODULES = (
    # NumPy, OpenCV, and RapidOCR are exact-hash, first-use runtime
    # components. They remain outside the MIT sidecar/installer and are loaded
    # from the verified user-data cache by engine.managed_vision.
    "numpy",
    "cv2",
    "rapidocr_onnxruntime",
    # Build tooling is neither required at runtime nor permitted in the frozen
    # application artifact.
    "setuptools",
    "_distutils_hack",
    "openadapt_flow.benchmark",
    "openadapt_flow.validation.adversary_corpus",
    "openadapt_flow.validation.adversary_corpus_v2",
    "openadapt_flow.validation.adversary_corpus_v3",
    "openadapt_flow.validation.identity_roc",
)

# The mobile decision portal supervises ``openadapt-flow console --attend`` in
# a second process image of this same executable, so the attended console must
# survive freezing.  PyInstaller's static analysis is not sufficient here for
# two independent reasons, both verified against the built archive rather than
# assumed:
#
# * Uvicorn resolves its event loop, HTTP protocol, websocket protocol, and
#   lifespan implementations by *string* import at run time
#   (``uvicorn.loops.auto`` -> ``uvicorn.loops.asyncio`` and friends), so those
#   submodules are unreachable from any import statement.  ``--collect-all``
#   brings the submodules, their data, and their distribution metadata.
# * FastAPI and Starlette read their own installed metadata, and Flow's console
#   only reaches ``openadapt_flow.console.*`` through a lazily imported command
#   handler, so the console package is named explicitly.
CONSOLE_COLLECTION: tuple[str, ...] = (
    "--collect-all",
    "uvicorn",
    "--collect-submodules",
    "openadapt_flow.console",
    "--hidden-import",
    "openadapt_flow.console.server",
    "--hidden-import",
    "openadapt_flow.console.app",
    "--hidden-import",
    "openadapt_flow.console.human_decisions",
    "--hidden-import",
    "fastapi",
    "--hidden-import",
    "starlette",
    "--copy-metadata",
    "fastapi",
    "--copy-metadata",
    "starlette",
    "--copy-metadata",
    "uvicorn",
)

RAPIDOCR_NOTICE_DIR = ROOT / "third_party" / "rapidocr"
LINUX_RUNNER_RUNTIME_EXCLUDE = r"libgcc_s\.so(\..*)?"
MACOS_X86_CRYPTOGRAPHY_VERSION = "48.0.0"
MACOS_OPENSSL_DYLIB = re.compile(r"(?:^|/)(?:libssl|libcrypto)(?:\.[0-9]+)*\.dylib$")


def configure_system_runtime_boundary(
    *,
    platform: str = sys.platform,
    dylib_module=None,
) -> bool:
    """Keep the Linux runner's unpinned libgcc outside the frozen artifact."""

    if not platform.startswith("linux"):
        return False
    if dylib_module is None:
        from PyInstaller.depend import dylib as dylib_module

    dylib_module._excludes.add(LINUX_RUNNER_RUNTIME_EXCLUDE)
    dylib_module.exclude_list = dylib_module.MatchList(dylib_module._excludes)
    return True


def verify_macos_intel_cryptography_boundary(
    *,
    platform: str = sys.platform,
    machine: str | None = None,
    distribution_lookup=distribution,
    run=subprocess.run,
) -> bool:
    """Refuse a mutable or externally linked macOS Intel crypto extension."""

    machine = machine or platform_module.machine()
    if platform != "darwin" or machine != "x86_64":
        return False

    installed = distribution_lookup("cryptography")
    if installed.version != MACOS_X86_CRYPTOGRAPHY_VERSION:
        raise RuntimeError(
            "macOS Intel frozen builds require the universal2 cryptography "
            f"{MACOS_X86_CRYPTOGRAPHY_VERSION} wheel, found {installed.version}"
        )
    extension = Path(installed.locate_file("cryptography/hazmat/bindings/_rust.abi3.so"))
    if not extension.is_file():
        raise RuntimeError(f"cryptography extension is missing: {extension}")
    result = run(
        ["otool", "-L", str(extension)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"could not inspect cryptography linkage: {result.stderr[-1000:]}")
    linked_openssl = [
        line.strip().split(" ", 1)[0]
        for line in result.stdout.splitlines()[1:]
        if MACOS_OPENSSL_DYLIB.search(line.strip().split(" ", 1)[0])
    ]
    if linked_openssl:
        raise RuntimeError(
            "macOS Intel cryptography must carry its own OpenSSL implementation; "
            "external libssl/libcrypto linkage can collide inside the frozen runtime: "
            + ", ".join(linked_openssl)
        )
    return True


def notice_data(
    onnxruntime_dir: Path | None = None,
) -> tuple[tuple[Path, str], ...]:
    """Return third-party notices that must accompany the frozen runtime."""

    if onnxruntime_dir is None:
        try:
            installed = distribution("onnxruntime")
        except PackageNotFoundError as exc:  # pragma: no cover - build environment guard
            raise RuntimeError(
                "onnxruntime is required to build the frozen Desktop runtime"
            ) from exc
        onnxruntime_dir = Path(installed.locate_file("onnxruntime"))

    data = (
        (onnxruntime_dir / "LICENSE", "third_party/onnxruntime"),
        (onnxruntime_dir / "ThirdPartyNotices.txt", "third_party/onnxruntime"),
        (RAPIDOCR_NOTICE_DIR / "LICENSE", "third_party/rapidocr"),
        (RAPIDOCR_NOTICE_DIR / "NOTICE", "third_party/rapidocr"),
    )
    missing = [str(source) for source, _ in data if not source.is_file()]
    if missing:
        raise RuntimeError("required third-party notice files are missing: " + ", ".join(missing))
    return data


def build_command(
    *,
    distpath: str = "dist",
    workpath: str = "build",
    specpath: str = ".",
    signing_identity: str = "",
    platform: str = sys.platform,
    onnxruntime_dir: Path | None = None,
    notice_bundle: Path | None = None,
) -> list[str]:
    """Return the deterministic PyInstaller command without importing it."""

    command = [
        "--clean",
        "--noconfirm",
        "--onefile",
        "--name",
        "openadapt-engine",
        "--distpath",
        str(Path(distpath)),
        "--workpath",
        str(Path(workpath)),
        "--specpath",
        str(Path(specpath)),
        # Run every process image of this executable unbuffered.  Desktop reads
        # its sidecar over pipes -- the Tauri IPC wire, and the decision
        # portal's supervised console, whose one-time capability banner is a
        # plain ``print`` in Flow's ``serve()``.  Block-buffered stdout holds
        # that banner until the process exits, so a frozen console that is
        # genuinely serving still looks dead to its supervisor.  Verified: the
        # banner never arrives without this, and ``PYTHONUNBUFFERED`` does not
        # reach a PyInstaller-frozen interpreter.
        "--python-option",
        "u",
        "--hidden-import",
        "openadapt_flow.__main__",
        "--collect-data",
        "openadapt_flow",
        "--collect-data",
        "engine",
        "--hidden-import",
        "onnxruntime",
        "--hidden-import",
        "shapely",
        "--hidden-import",
        "pyclipper",
        "--hidden-import",
        "six",
        "--hidden-import",
        "tqdm",
        "--copy-metadata",
        "openadapt-flow",
    ]
    command.extend(CONSOLE_COLLECTION)
    for source, destination in notice_data(onnxruntime_dir):
        command.extend(("--add-data", f"{source}:{destination}"))
    notice_bundle = notice_bundle or (ROOT / ".build-frozen-notices")
    command.extend(("--add-data", f"{notice_bundle}:{NOTICE_BUNDLE_MEMBER}"))
    for module in EXCLUDED_MODULES:
        command.extend(("--exclude-module", module))
    # A Developer ID build must sign the binaries *inside* PyInstaller's
    # one-file archive with the same identity Tauri later applies to the
    # launcher. Post-processing cannot reach those embedded binaries. Ad-hoc
    # packages deliberately omit hardened runtime via tauri.adhoc.conf.json;
    # passing "-" here would create hardened, identity-less libraries that
    # macOS library validation refuses to load.
    signing_identity = signing_identity.strip()
    if platform == "darwin" and signing_identity and signing_identity != "-":
        command.extend(("--codesign-identity", signing_identity))
        command.extend(
            (
                "--osx-entitlements-file",
                str(ROOT / "src-tauri" / "Entitlements.plist"),
            )
        )
    command.append(str(ROOT / "engine" / "__main__.py"))
    return command


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--distpath", default="dist")
    parser.add_argument("--workpath", default="build")
    parser.add_argument("--specpath", default=".")
    args = parser.parse_args()

    import PyInstaller.__main__

    workpath = Path(args.workpath)
    notice_bundle_path = workpath.parent / f".{workpath.name}-frozen-notices"
    notice_bundle = prepare_notice_bundle(
        notice_bundle_path,
        root_license=ROOT / "LICENSE",
    )
    try:
        configure_system_runtime_boundary()
        verify_macos_intel_cryptography_boundary()
        command = build_command(
            distpath=args.distpath,
            workpath=args.workpath,
            specpath=args.specpath,
            signing_identity=os.environ.get("APPLE_SIGNING_IDENTITY", ""),
            notice_bundle=notice_bundle,
        )
        PyInstaller.__main__.run(command)
    finally:
        shutil.rmtree(notice_bundle_path, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
