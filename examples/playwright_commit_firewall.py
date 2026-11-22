"""60-second Commit Firewall example for Playwright / CUA agents."""

from pathlib import Path

from openadapt_verify import CommitBlockedError, commit_guard


class DemoPage:
    """Minimal duck-typed Playwright page (swap for a real sync/async Page)."""

    def __init__(self, fields: dict[str, str]) -> None:
        self._fields = dict(fields)

    def input_value(self, selector: str) -> str:
        return self._fields[selector]

    def screenshot(self) -> bytes:
        return b"\x89PNG\r\n\x1a\ndemo"


def main() -> None:
    expected = {"Policy Holder": "Alice Novak", "Coverage Limit": "$250,000"}
    out = Path("/tmp/openadapt-commit-firewall-demo")

    # 1. Clean commit -> verified + signed Ed25519 record/run/v1 receipt.
    with commit_guard(DemoPage(expected), intent="Bind Policy", expected=expected, receipt_dir=out) as g:
        pass  # page.get_by_role("button", name="Bind Policy").click()
    print(f"VERIFIED | receipt_id={g.result.receipt_id[:16]} | path={g.result.receipt_path}")

    # 2. Silent homoglyph + unbound control -> blocked & logged to Exception Inbox.
    corrupted = DemoPage({"Policy Holder": "\u0410lice Novak", "Coverage Limit": ""})
    try:
        with commit_guard(corrupted, intent="Bind Policy", expected=expected, inbox=out / "inbox.jsonl"):
            pass  # Cyrillic 'А' + empty unbound input
    except CommitBlockedError as err:
        print(f"STOPPED  | verdict={err.result.verdict} | reason={err.reason}")


if __name__ == "__main__":
    main()
