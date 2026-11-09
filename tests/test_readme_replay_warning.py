"""The README must not present `flow replay --url` as a dry run.

`openadapt flow replay <bundle> --url <app>` drives the app at that URL and
performs every recorded step, including irreversible saves, with no prompt.
A reader who follows the README literally against a real system of record
makes real writes, so the text next to that command has to say so.
"""

from __future__ import annotations

import re
from pathlib import Path

README = Path(__file__).resolve().parents[1] / "README.md"

# A sentence that says replay carries out the recorded actions for real.
WRITE_WARNING = re.compile(
    r"replay[^.]*\b(performs|makes|carries out)\b[^.]*\b(every|real)\b"
    r"[^.]*\b(action|write|save)",
    re.IGNORECASE,
)
FENCE = re.compile(r"```[^\n]*\n(.*?)```", re.DOTALL)


def _replay_url_blocks(text: str) -> list[re.Match[str]]:
    return [
        match
        for match in FENCE.finditer(text)
        if any(
            "flow replay" in line and "--url" in line
            for line in match.group(1).splitlines()
        )
    ]


def _prose_after(text: str, match: re.Match[str]) -> str:
    """Return the prose after a code block, up to the next block or heading."""
    rest = text[match.end() :]
    stop = re.search(r"^(```|#)", rest, re.MULTILINE)
    return rest[: stop.start()] if stop else rest


def test_readme_shows_replay_against_a_url() -> None:
    assert _replay_url_blocks(README.read_text(encoding="utf-8"))


def test_replay_against_a_url_is_not_called_a_rehearsal() -> None:
    text = README.read_text(encoding="utf-8")
    for block in _replay_url_blocks(text):
        prose = _prose_after(text, block)
        assert "rehearsal" not in prose.lower(), prose


def test_replay_against_a_url_says_it_performs_real_writes() -> None:
    text = README.read_text(encoding="utf-8")
    for block in _replay_url_blocks(text):
        prose = " ".join(_prose_after(text, block).split())
        assert WRITE_WARNING.search(prose), prose
        assert re.search(r"\b(test|staging)\b", prose, re.IGNORECASE), prose
