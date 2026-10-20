"""What every workflow in the pool provides.

A workflow is the task as the customer's engineer knows it: how to sign in and
reach the start screen, how a person does one item, which inputs vary, where
the record lands (what the engineer writes into verify.yaml), and an
independent oracle the product never sees.
"""

from __future__ import annotations

import inspect
import random
from dataclasses import dataclass, field
from typing import Any, Optional

FIRST, SECOND = "first", "second"


@dataclass
class Verdict:
    verdict: str  # VERIFIED | MISSING | WRONG
    detail: dict[str, Any] = field(default_factory=dict)


class Workflow:
    #: Pool id, e.g. "oe-allergy-add".
    id: str = ""
    app: str = ""
    #: Certification policy for qualify.
    policy: str = "permissive"
    #: single | multipage | conditional | loop_worklist | loop_search | paginated
    shape: str = "single"
    #: The inputs the engineer names when demonstrating (record --param).
    inputs: tuple[str, ...] = ()
    #: The task as a person would describe it.
    intent: str = ""
    #: The second installation: what changes, in one line.
    second_installation: str = ""
    #: Kind of change: version | theme | locale | required_field | role | screen
    second_kind: str = ""
    viewport: tuple[int, int] = (1280, 900)
    #: True when one run processes many items (a loop over the app's own list).
    batch_run: bool = False
    #: Reset the app before every held-out item (an app that dedupes per entity).
    heldout_reset_each: bool = False
    seed: int = 0

    # -- installation ---------------------------------------------------------
    def installation_viewport(self, installation: str) -> tuple[int, int]:
        return self.viewport

    def device_scale(self, installation: str) -> float:
        return 1.0

    def apply_installation(self, installation: str) -> None:
        """Change the running app to the named installation (idempotent)."""

    # -- app state --------------------------------------------------------------
    def reset(self, installation: str) -> None:
        raise NotImplementedError

    def start(self, page: Any, installation: str) -> None:
        """Unmeasured setup: sign in and open the task's start screen."""
        raise NotImplementedError

    def entry_url(self, installation: str) -> str:
        raise NotImplementedError

    # -- items ------------------------------------------------------------------
    def make_item(self, rng: random.Random, n: int, split: str) -> dict[str, str]:
        raise NotImplementedError

    def items(self, split: str, n: int, installation: str = FIRST) -> list[dict[str, str]]:
        """Deterministic items. ``demo`` items are never held out."""
        salt = {"demo": 1, "heldout": 2, "qual": 3}[split] * 100 + (
            0 if installation == FIRST else 50
        )
        rng = random.Random(f"{self.id}:{self.seed}:{salt}")
        return [self.make_item(rng, i, f"{split}-{installation}") for i in range(n)]

    def prepare_items(self, items: list[dict[str, str]], installation: str) -> None:
        """Create whatever the items need in the app (e.g. the rows a loop visits)."""

    def batch_params(self, items: list[dict[str, str]]) -> dict[str, str]:
        """The inputs of one loop run over all ``items`` (batch workflows)."""
        return {k: items[0][k] for k in self.inputs if k in items[0]}

    def worklist_row(self, item: dict[str, str]) -> dict[str, str]:
        """The row an engineer exports for ``for-each`` (keyed workflows)."""
        return {k: item[k] for k in (*self.inputs, *self.keys) if k in item}

    #: Columns that name the row a keyed demonstration acts on.
    keys: tuple[str, ...] = ()

    def branch_key(self, item: dict[str, str]) -> str:
        """Which branch an item takes (conditional workflows)."""
        return ""

    # -- demonstration ------------------------------------------------------------
    def demonstrate(self, rec: Any, page: Any, item: dict[str, str]) -> None:
        """A person doing one item, through the product recorder."""
        raise NotImplementedError

    def perform(self, page: Any, item: dict[str, str]) -> None:
        """The same task without OpenAdapt (spec validation before the freeze)."""
        self.demonstrate(_PlainRecorder(page), page, item)

    # -- engineer config --------------------------------------------------------
    def verify_yaml(self, installation: str) -> str:
        raise NotImplementedError

    def hook_lines(self) -> int:
        """Lines the engineer writes for sign-in (counted once per app)."""
        src = inspect.getsource(type(self).start)
        return sum(1 for line in src.splitlines() if line.strip() and not line.strip().startswith(("#", '"""')))

    # -- oracle -------------------------------------------------------------------
    def snapshot(self) -> Any:
        raise NotImplementedError

    def judge(self, item: dict[str, str], before: Any, after: Any) -> Verdict:
        raise NotImplementedError


class _PlainRecorder:
    """Recorder stand-in for spec validation: the same input, nothing recorded."""

    def __init__(self, page: Any) -> None:
        self.page = page

    def click(self, x: int, y: int) -> None:
        self.page.mouse.click(x, y)
        self.page.wait_for_timeout(150)

    def double_click(self, x: int, y: int) -> None:
        self.page.mouse.dblclick(x, y)

    def type_text(self, text: str, param: Optional[str] = None) -> None:
        self.page.keyboard.type(text)

    def press(self, key: str) -> None:
        self.page.keyboard.press(key)
        self.page.wait_for_timeout(150)

    def scroll(self, dx: int, dy: int) -> None:
        self.page.mouse.wheel(dx, dy)
        self.page.wait_for_timeout(150)

    def finish(self) -> None:
        return None


def center(page: Any, rec: Any, locator: Any) -> tuple[int, int]:
    """A control's center in top-page pixels, scrolling (recorded) until in view."""
    loc = page.locator(locator).first if isinstance(locator, str) else locator
    loc.wait_for(state="visible", timeout=30_000)
    for _ in range(10):
        box = loc.bounding_box()
        if box is None:
            raise RuntimeError(f"not rendered: {locator}")
        h = page.viewport_size["height"]
        cy = box["y"] + box["height"] / 2
        if 10 <= cy <= h - 10:
            return int(box["x"] + box["width"] / 2), int(cy)
        page.mouse.move(page.viewport_size["width"] // 2, h // 2)
        rec.scroll(0, 300 if cy > h / 2 else -300)
    raise RuntimeError(f"could not scroll to {locator}")


def click(page: Any, rec: Any, locator: Any) -> None:
    rec.click(*center(page, rec, locator))


def fill(page: Any, rec: Any, locator: Any, value: str, param: Optional[str]) -> None:
    rec.click(*center(page, rec, locator))
    if value:
        rec.type_text(value, param=param)


def choose(page: Any, rec: Any, locator: Any, label: str, param: Optional[str] = None) -> None:
    """A native <select>: focus it and type the option's label, as people do."""
    rec.click(*center(page, rec, locator))
    rec.press("Escape")
    rec.type_text(label, param=param)
