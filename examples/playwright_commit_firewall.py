"""Minimal UI commit check example with an explicit Ed25519 signing key."""

from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from openadapt_verify import CommitBlockedError, ui_commit_check


class DemoPage:
    """Minimal duck-typed page object exposing input_value() and screenshot()."""

    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = dict(fields)

    def input_value(self, selector: str) -> str:
        return self._fields[selector]

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\ndemo"


def main() -> None:
    expected = {"Policy Holder": "Alice Novak", "Coverage Limit": "$250,000"}
    out = Path("/tmp/openadapt-ui-commit-check-demo")
    signing_key = Ed25519PrivateKey.generate()

    # 1. Clean commit -> verified + signed Ed25519 record/run/v1 receipt.
    with ui_commit_check(
        DemoPage(expected),
        intent="Bind Policy",
        expected=expected,
        record_id="quote-2026-0001",
        receipt_dir=out,
        signing_key=signing_key,
    ) as check:
        pass
    assert check.result is not None and check.result.receipt_id is not None
    print(
        f"VERIFIED | receipt_id={check.result.receipt_id[:16]} | path={check.result.receipt_path}"
    )

    # 2. Silent homoglyph + unbound control -> blocked & logged to Exception Inbox.
    corrupted = DemoPage({"Policy Holder": "\u0410lice Novak", "Coverage Limit": ""})
    try:
        with ui_commit_check(
            corrupted,
            intent="Bind Policy",
            expected=expected,
            inbox=out / "inbox.jsonl",
        ):
            pass
    except CommitBlockedError as err:
        print(f"STOPPED  | verdict={err.result.verdict} | reason={err.reason}")


if __name__ == "__main__":
    main()
