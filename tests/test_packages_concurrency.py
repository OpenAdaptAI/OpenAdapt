"""Packages concurrency: only a pull request's own older run is ever cancelled.

desktop-release.yml releases from the Packages push run of the newest
first-parent commit that changed packages/desktop, so a push run on main must
never be cancelled. An earlier rule let a push without a Desktop change cancel
an older one, judging the change from the push payload's per-commit file lists.
Those lists did not show Desktop files on PR merge pushes: run 40210206361
(05a84be7e, #1574) was cancelled by the next merge, and Desktop 0.23.0 and
0.24.1 never released.

These tests evaluate the workflow's real `group` and `cancel-in-progress`
expressions against event contexts, with a small evaluator for the subset of
GitHub's expression syntax they use.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "packages.yml"


def _concurrency(workflow: Path = WORKFLOW) -> dict[str, str]:
    return yaml.safe_load(workflow.read_text(encoding="utf-8"))["concurrency"]


# --- a small evaluator for GitHub Actions expressions -------------------------

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+)|(?P<op>==|!=|&&|\|\||[!().,\[\]])"
    r"|(?P<name>[A-Za-z_][A-Za-z0-9_-]*))"
)


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos, text = [], 0, text.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"cannot parse {text[pos:]!r}")
        kind = match.lastgroup
        assert kind is not None
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


def _truthy(value: Any) -> bool:
    return value not in (None, False, 0, "")


def _contains(haystack: Any, needle: Any) -> bool:
    if isinstance(haystack, list):
        return any(str(item).lower() == str(needle).lower() for item in haystack)
    return str(needle).lower() in str(haystack if haystack is not None else "").lower()


_FUNCTIONS = {
    "contains": _contains,
    "toJSON": lambda value: json.dumps(value, indent=2),
}


class _Parser:
    def __init__(self, text: str, context: dict[str, Any]) -> None:
        self.tokens = _tokens(text)
        self.pos = 0
        self.context = context

    def _peek(self) -> str | None:
        return self.tokens[self.pos][1] if self.pos < len(self.tokens) else None

    def _take(self) -> tuple[str, str]:
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def parse(self) -> Any:
        value = self._or()
        assert self.pos == len(self.tokens), self.tokens[self.pos :]
        return value

    def _or(self) -> Any:
        left = self._and()
        while self._peek() == "||":
            self._take()
            right = self._and()
            left = left if _truthy(left) else right
        return left

    def _and(self) -> Any:
        left = self._compare()
        while self._peek() == "&&":
            self._take()
            right = self._compare()
            left = right if _truthy(left) else left
        return left

    def _compare(self) -> Any:
        left = self._unary()
        while self._peek() in ("==", "!="):
            op = self._take()[1]
            right = self._unary()
            left = (left == right) if op == "==" else (left != right)
        return left

    def _unary(self) -> Any:
        if self._peek() == "!":
            self._take()
            return not _truthy(self._unary())
        return self._primary()

    def _primary(self) -> Any:
        kind, text = self._take()
        if text == "(":
            value = self._or()
            assert self._take()[1] == ")"
            return value
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return int(text)
        if text in ("true", "false", "null"):
            return {"true": True, "false": False, "null": None}[text]
        if text in _FUNCTIONS and self._peek() == "(":
            self._take()
            args = [self._or()]
            while self._peek() == ",":
                self._take()
                args.append(self._or())
            assert self._take()[1] == ")"
            return _FUNCTIONS[text](*args)
        value: Any = self.context.get(text)
        while self._peek() in (".", "["):
            if self._take()[1] == ".":
                key = self._take()[1]
                value = value.get(key) if isinstance(value, dict) else None
            else:
                index = self._or()
                assert self._take()[1] == "]"
                if isinstance(value, list) and isinstance(index, int):
                    value = value[index] if index < len(value) else None
                elif isinstance(value, dict):
                    value = value.get(index)
                else:
                    value = None
        return value


def _evaluate(template: str, context: dict[str, Any]) -> str:
    """Render a value that may hold `${{ }}` expressions, as GitHub does."""

    def render(match: re.Match[str]) -> str:
        value = _Parser(match.group(1), context).parse()
        if value is None:
            return ""
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return re.sub(r"\$\{\{(.*?)\}\}", render, " ".join(template.split()), flags=re.S)


# --- event contexts -------------------------------------------------------------


def _push(run_id: int, sha: str, commits: int = 2) -> dict[str, Any]:
    # A PR merge push whose per-commit file lists name no Desktop files, as
    # GitHub sent them for #1574 although that merge changed packages/desktop.
    return {
        "github": {
            "event_name": "push",
            "sha": sha,
            "run_id": run_id,
            "ref": "refs/heads/main",
            "event": {
                "head_commit": {"id": sha},
                "commits": [{"id": f"{sha}{i}", "modified": []} for i in range(commits)],
            },
        }
    }


def _pull_request(number: int, run_id: int) -> dict[str, Any]:
    return {
        "github": {
            "event_name": "pull_request",
            "sha": f"merge{run_id}",
            "run_id": run_id,
            "event": {"pull_request": {"number": number}},
        }
    }


def _other(event_name: str, run_id: int, sha: str = "same") -> dict[str, Any]:
    return {"github": {"event_name": event_name, "sha": sha, "run_id": run_id, "event": {}}}


def _group_and_cancel(context: dict[str, Any], workflow: Path = WORKFLOW) -> tuple[str, bool]:
    concurrency = _concurrency(workflow)
    group = _evaluate(concurrency["group"], context)
    cancel = _evaluate(str(concurrency["cancel-in-progress"]), context)
    assert cancel in ("true", "false"), cancel
    return group, cancel == "true"


def _desktop_push_survives(workflow: Path = WORKFLOW) -> bool:
    # The #1574 sequence: a Desktop-changing merge, then an unrelated merge.
    desktop_group, _ = _group_and_cancel(_push(40210206361, "05a84be7e"), workflow)
    later_group, later_cancels = _group_and_cancel(_push(40210715609, "a016fd67e"), workflow)
    return desktop_group != later_group or not later_cancels


def test_a_desktop_push_is_never_cancelled_by_the_next_push() -> None:
    assert _desktop_push_survives()


@pytest.mark.parametrize("commits", [1, 2, 19, 20, 40])
def test_no_push_to_main_shares_a_group_or_cancels(commits: int) -> None:
    first = _group_and_cancel(_push(1, "aaa", commits=commits))
    second = _group_and_cancel(_push(2, "bbb", commits=commits))
    assert first[0] != second[0]
    assert first[1] is False and second[1] is False


def test_a_rerun_of_the_same_commit_has_its_own_group() -> None:
    first, _ = _group_and_cancel(_other("push", 10, "x"))
    second, _ = _group_and_cancel(_other("push", 11, "x"))
    assert first != second


def test_a_pull_requests_newer_push_cancels_its_older_run() -> None:
    older, _ = _group_and_cancel(_pull_request(7, 100))
    newer, cancels = _group_and_cancel(_pull_request(7, 101))
    other_pr, _ = _group_and_cancel(_pull_request(8, 102))
    assert older == newer and cancels
    assert other_pr != newer


@pytest.mark.parametrize("event_name", ["merge_group", "workflow_dispatch", "schedule"])
def test_queue_groups_and_dispatches_are_never_cancelled(event_name: str) -> None:
    first = _group_and_cancel(_other(event_name, 1))
    second = _group_and_cancel(_other(event_name, 2))
    assert first[0] != second[0]
    assert first[1] is False and second[1] is False


def test_the_rule_does_not_read_the_push_payloads_file_lists() -> None:
    concurrency = _concurrency()
    assert "github.event.commits" not in concurrency["group"]
    assert "github.event.commits" not in str(concurrency["cancel-in-progress"])


def test_the_old_payload_rule_would_have_cancelled_the_desktop_push(tmp_path: Path) -> None:
    # Guards the evaluator itself: the rule this replaced fails the #1574 case.
    old = tmp_path / "packages.yml"
    old.write_text(
        yaml.safe_dump(
            {
                "concurrency": {
                    "group": "packages-${{ github.event.pull_request.number || ("
                    "github.event_name == 'push' && github.event.head_commit && "
                    "!github.event.commits[19] && "
                    "!contains(toJSON(github.event.commits), 'packages/desktop/') && "
                    "'main-without-desktop') || github.sha }}",
                    "cancel-in-progress": "${{ github.event_name == 'pull_request' || ("
                    "github.event_name == 'push' && github.event.head_commit != null && "
                    "!github.event.commits[19] && "
                    "!contains(toJSON(github.event.commits), 'packages/desktop/')) }}",
                }
            }
        ),
        encoding="utf-8",
    )
    assert not _desktop_push_survives(old)


def test_the_evaluator_reads_github_semantics() -> None:
    # || and && return operands, missing properties are null, ! negates.
    context = {"github": {"a": 0, "b": "x", "event": {"commits": [{"m": ["a/b"]}]}}}
    assert _evaluate("${{ github.a || github.b }}", context) == "x"
    assert _evaluate("${{ github.event.pull_request.number || 'none' }}", context) == "none"
    assert _evaluate("${{ !github.event.commits[19] && 'short' }}", context) == "short"
    assert _evaluate("p-${{ github.b == 'x' }}", context) == "p-true"
    assert _evaluate("${{ contains(toJSON(github.event.commits), 'a/b') }}", context) == "true"
