#!/usr/bin/env python3
"""Check WAA /evaluate endpoint health."""

from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description="Check WAA /evaluate endpoint")
    parser.add_argument("--server", required=True, help="WAA server URL (e.g., http://vm-ip:5000)")
    args = parser.parse_args()

    try:
        import requests
    except ImportError:
        print("ERROR: requests is required")
        return 1

    url = args.server.rstrip("/") + "/evaluate/health"
    try:
        resp = requests.get(url, timeout=5.0)
    except Exception as exc:
        print(f"ERROR: request failed: {exc}")
        return 1

    if resp.status_code != 200:
        print(f"ERROR: /evaluate not ready (HTTP {resp.status_code})")
        print(resp.text)
        return 1

    print("/evaluate endpoint ready")
    print(resp.text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
