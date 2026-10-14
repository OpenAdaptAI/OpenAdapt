#!/usr/bin/env python3
"""Patch WAA server to add /evaluate endpoint.

This keeps WAA behavior vanilla while enabling programmatic evaluation over HTTP.
It copies evaluate_endpoint.py into the WAA server directory and registers the
blueprint in WAA's Flask app.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


def _default_waa_path() -> Path:
    cwd = Path.cwd()
    if (cwd / "vendor" / "WindowsAgentArena").exists():
        return cwd / "vendor" / "WindowsAgentArena"
    if (cwd / "WindowsAgentArena").exists():
        return cwd / "WindowsAgentArena"
    if (Path.home() / "WindowsAgentArena").exists():
        return Path.home() / "WindowsAgentArena"
    return cwd / "vendor" / "WindowsAgentArena"


def _patch_main(main_path: Path) -> None:
    marker = "# openadapt-evals: /evaluate endpoint"
    content = main_path.read_text()
    if marker in content:
        return

    patch_block = (
        "\n\n"
        f"{marker}\n"
        "try:\n"
        "    from evaluate_endpoint import create_evaluate_blueprint\n"
        "    evaluate_bp = create_evaluate_blueprint()\n"
        "    app.register_blueprint(evaluate_bp)\n"
        "except Exception as exc:\n"
        "    print(f\"WAA /evaluate endpoint disabled: {exc}\")\n"
    )

    if "if __name__ == \"__main__\":" in content:
        parts = content.split("if __name__ == \"__main__\":", 1)
        content = parts[0] + patch_block + "\nif __name__ == \"__main__\":" + parts[1]
    else:
        content += patch_block

    main_path.write_text(content)


def main() -> int:
    parser = argparse.ArgumentParser(description="Patch WAA server /evaluate endpoint")
    parser.add_argument("--waa-path", type=str, default=None, help="Path to WindowsAgentArena repo")
    args = parser.parse_args()

    waa_path = Path(args.waa_path) if args.waa_path else _default_waa_path()
    if not waa_path.exists():
        raise SystemExit(f"WAA repo not found at: {waa_path}")

    server_dir = waa_path / "src" / "win-arena-container" / "vm" / "setup" / "server"
    main_path = server_dir / "main.py"
    if not main_path.exists():
        raise SystemExit(f"WAA server main.py not found at: {main_path}")

    evaluate_src = Path(__file__).resolve().parents[1] / "openadapt_evals" / "server" / "evaluate_endpoint.py"
    if not evaluate_src.exists():
        raise SystemExit(f"evaluate_endpoint.py not found at: {evaluate_src}")

    server_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(evaluate_src, server_dir / "evaluate_endpoint.py")
    _patch_main(main_path)

    print(f"Patched WAA server: {main_path}")
    print("/evaluate endpoint enabled (restart WAA server if running).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
