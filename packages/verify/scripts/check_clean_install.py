"""Install openadapt-verify into a clean venv with pip and enforce its budgets.

This is the user's path: a fresh ``python -m venv``, ``pip install
openadapt-verify`` with no cache, then the quickstart. In CI the in-tree wheels
are passed with ``--find-links`` so the commit under test is what installs;
everything else comes from PyPI. The check fails when:

- the install takes longer than ``--max-seconds``;
- the installed distributions exceed ``--max-mb`` of site-packages or
  ``--max-dists`` distributions (both measured over the empty venv);
- a heavy distribution (vision, OCR, browser, GUI, ML) is installed;
- a distribution's license metadata names GPL, AGPL, LGPL or SSPL;
- a heavy module is importable, or ``import openadapt_verify`` loads one;
- ``openadapt-verify quickstart`` does not give every expected outcome with a
  checked signed receipt.

Usage::

    python scripts/check_clean_install.py --find-links dist --json report.json \\
        [--postgres '{"host": ...}'] [--mysql '{"host": ...}']

PostgreSQL needs a driver the operator chooses; ``--operator-driver`` installs
it after the measured install (psycopg is LGPL-3.0, so it is never one of this
package's dependencies). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

#: Distributions that must never be installed by the base package.
HEAVY_DISTS = {
    "numpy",
    "opencv-python",
    "opencv-python-headless",
    "pillow",
    "rapidocr-onnxruntime",
    "onnxruntime",
    "playwright",
    "torch",
    "torchvision",
    "transformers",
    "scipy",
    "scikit-image",
    "matplotlib",
    "shapely",
    "pyautogui",
    "pynput",
    "mss",
    "pyobjc-core",
    "uiautomation",
}
HEAVY_MODULES = (
    "numpy",
    "cv2",
    "PIL",
    "rapidocr_onnxruntime",
    "onnxruntime",
    "playwright",
    "torch",
    "scipy",
    "matplotlib",
)
COPYLEFT = re.compile(r"\b(A?GPL|LGPL|SSPL|GNU (Affero |Lesser )?General Public)", re.I)

_LIST = """
import importlib.metadata as m, json
out = []
for d in m.distributions():
    md = d.metadata
    name = md.get("Name")
    if not name:
        continue
    lic = [md.get("License-Expression") or "", (md.get("License") or "")[:200]]
    lic += [c for c in (md.get_all("Classifier") or []) if c.startswith("License")]
    files = 0
    for f in d.files or []:
        try:
            files += d.locate_file(f).stat().st_size
        except OSError:
            pass
    out.append({"name": name, "version": md.get("Version"), "license": lic,
                "bytes": files})
print(json.dumps(out))
"""

_IMPORT = """
import importlib.util, json, sys
import openadapt_verify, openadapt_verify.cli, openadapt_verify.guard
import openadapt_verify.quickstart, openadapt_verify.shadow
heavy = json.loads(sys.argv[1])
print(json.dumps({
    "importable": [m for m in heavy if importlib.util.find_spec(m) is not None],
    "loaded": sorted(m for m in sys.modules if m.partition(".")[0] in heavy),
}))
"""


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def _python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _site_packages(venv: Path) -> Path:
    if os.name == "nt":
        return venv / "Lib" / "site-packages"
    return next((venv / "lib").glob("python*/site-packages"))


def _bytes(root: Path) -> int:
    total = 0
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = Path(dirpath, name)
            if not path.is_symlink():
                total += path.stat().st_size
    return total


def _dists(python: Path) -> list[dict]:
    done = subprocess.run(
        [str(python), "-I", "-c", _LIST], check=True, capture_output=True, text=True
    )
    return json.loads(done.stdout)


def _pip(python: Path, *args: str) -> None:
    subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--disable-pip-version-check",
            "--quiet",
            *args,
        ],
        check=True,
    )


def check(args: argparse.Namespace) -> dict:
    work = Path(tempfile.mkdtemp(prefix="verify-clean-"))
    venv = work / "venv"
    problems: list[str] = []
    try:
        subprocess.run([args.python, "-m", "venv", str(venv)], check=True)
        python = _python(venv)
        before = {_normalize(d["name"]) for d in _dists(python)}
        before_bytes = _bytes(_site_packages(venv))
        links = [arg for link in args.find_links for arg in ("--find-links", link)]

        started = time.perf_counter()
        _pip(python, *links, args.requirement)
        seconds = time.perf_counter() - started

        dists = [d for d in _dists(python) if _normalize(d["name"]) not in before]
        size = _bytes(_site_packages(venv)) - before_bytes
        names = sorted(_normalize(d["name"]) for d in dists)
        if seconds > args.max_seconds:
            problems.append(f"install took {seconds:.1f} s, over {args.max_seconds} s")
        if size / 1e6 > args.max_mb:
            problems.append(f"installs {size / 1e6:.1f} MB, over {args.max_mb} MB")
        if len(dists) > args.max_dists:
            problems.append(
                f"installs {len(dists)} distributions, over {args.max_dists}"
            )
        for name in sorted(set(names) & HEAVY_DISTS):
            problems.append(f"installs heavy distribution {name}")
        licenses = {}
        for dist in dists:
            text = " | ".join(part for part in dist["license"] if part)
            licenses[dist["name"]] = text or "UNKNOWN"
            if not text:
                problems.append(f"{dist['name']} declares no license")
            elif COPYLEFT.search(text):
                problems.append(f"{dist['name']} is copyleft-licensed: {text}")

        probe = subprocess.run(
            [str(python), "-I", "-c", _IMPORT, json.dumps(HEAVY_MODULES)],
            capture_output=True,
            text=True,
            cwd=work,
        )
        if probe.returncode != 0:
            problems.append("import failed: " + probe.stderr[-1000:])
        else:
            imported = json.loads(probe.stdout)
            if imported["importable"] or imported["loaded"]:
                problems.append(f"heavy modules present: {imported}")

        # The quickstart, after the measured install: extras for the server
        # databases are installed now so they never count against the budget.
        extras = []
        if args.mysql:
            extras.append(args.requirement + "[mysql]")
        extras += args.operator_driver
        if extras:
            _pip(python, *links, *extras)
        command = [
            str(venv / ("Scripts" if os.name == "nt" else "bin") / "openadapt-verify"),
            "quickstart",
            "--json",
        ]
        if args.postgres:
            command += ["--postgres", args.postgres]
        if args.mysql:
            command += ["--mysql", args.mysql]
        started = time.perf_counter()
        quick = subprocess.run(command, capture_output=True, text=True, cwd=work)
        quick_seconds = time.perf_counter() - started
        rows = json.loads(quick.stdout) if quick.stdout.strip().startswith("[") else []
        expected_paths = 3 + bool(args.postgres) + bool(args.mysql)
        if quick.returncode != 0 or len(rows) != 3 * expected_paths:
            problems.append(
                f"quickstart failed (exit {quick.returncode}, {len(rows)} rows): "
                + (quick.stderr or quick.stdout)[-2000:]
            )
        return {
            "requirement": args.requirement,
            "python": subprocess.run(
                [
                    str(python),
                    "-c",
                    "import platform; print(platform.python_version())",
                ],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "platform": sys.platform,
            "install_seconds": round(seconds, 2),
            "site_packages_mb": round(size / 1e6, 1),
            "dist_count": len(dists),
            "dists": sorted(f"{d['name']}=={d['version']}" for d in dists),
            "licenses": licenses,
            "budget": {
                "max_seconds": args.max_seconds,
                "max_mb": args.max_mb,
                "max_dists": args.max_dists,
            },
            "quickstart_seconds": round(quick_seconds, 2),
            "quickstart": [
                {k: r[k] for k in ("path", "scenario", "outcome", "receipt_checked")}
                for r in rows
            ],
            "problems": problems,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _markdown(report: dict) -> str:
    lines = [
        "## openadapt-verify clean install",
        "",
        f"`pip install {report['requirement']}` on Python {report['python']}: "
        f"{report['install_seconds']} s (budget {report['budget']['max_seconds']} s), "
        f"{report['site_packages_mb']} MB (budget {report['budget']['max_mb']} MB), "
        f"{report['dist_count']} distributions (budget {report['budget']['max_dists']}).",
        "",
        "| Read path | Agent write | Outcome | Signed receipt checked |",
        "|---|---|---|---|",
    ]
    for row in report["quickstart"]:
        lines.append(
            f"| {row['path']} | {row['scenario']} | {row['outcome']} | "
            f"{'yes' if row['receipt_checked'] else 'NO'} |"
        )
    lines += ["", "Problems: " + ("; ".join(report["problems"]) or "none"), ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--requirement", default="openadapt-verify")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--find-links", action="append", default=[])
    parser.add_argument("--max-seconds", type=float, default=60)
    parser.add_argument("--max-mb", type=float, default=110)
    parser.add_argument("--max-dists", type=int, default=26)
    parser.add_argument("--postgres", help="psycopg connect arguments (JSON)")
    parser.add_argument("--mysql", help="PyMySQL connect arguments (JSON)")
    parser.add_argument(
        "--operator-driver",
        action="append",
        default=[],
        help="a driver to install after the measured install, e.g. psycopg[binary]",
    )
    parser.add_argument("--json", help="write the report here")
    args = parser.parse_args(argv)
    report = check(args)
    text = _markdown(report)
    print(text)
    print(json.dumps({k: report[k] for k in ("dists", "licenses")}, indent=1))
    if args.json:
        Path(args.json).write_text(json.dumps(report, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(text + "\n")
    return 1 if report["problems"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
