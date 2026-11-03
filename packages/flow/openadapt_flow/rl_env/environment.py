"""The generic OpenEnv browser environment.

One class drives every app. The agent acts through a headless Chromium page
with a small action set (click, fill, type, press, select, scroll, goto,
wait, done) and sees the page's visible text, its interactive elements and,
when asked, a screenshot. It never sees the task's fault, start URL, setup,
reference solution or the record itself: the manifest's ``blocked_paths`` and
every other origin are refused in the browser, and the observed URL carries
no query string.

The episode ends when the agent sends ``done`` with its claim, or when the
step budget runs out (no claim). Only then is the record read and the reward
set; every earlier step has reward 0.
"""

from __future__ import annotations

import base64
import importlib
import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Literal, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx
from openenv.core.env_server.interfaces import Environment
from openenv.core.env_server.types import (
    Action,
    EnvironmentMetadata,
    Observation,
    State,
)
from pydantic import Field

from openadapt_flow.rl_env import CONTRACT_VERSION
from openadapt_flow.rl_env.reward import EpisodeJudge, Judgement, render_verify_config
from openadapt_flow.rl_env.tasks import (
    PUBLIC_SPLIT,
    EnvDirectory,
    Task,
    TaskError,
    load_environment,
)

ActionKind = Literal[
    "click", "fill", "type", "press", "select", "scroll", "goto", "wait", "done"
]

#: Interactive elements listed in each observation, and the text budget.
MAX_ELEMENTS = 80
MAX_TEXT = 4000
MAX_WAIT_S = 5.0
#: Separates iframe hops in a selector: ``iframe[name="pat"] >>> #form_fname``.
FRAME_SEP = " >>> "
VIEWPORT = {"width": 1280, "height": 800}

_ELEMENTS_JS = """
(limit) => {
  const out = [];
  const nodes = document.querySelectorAll(
    'a[href], button, input, textarea, select, [role=button], [onclick]');
  for (const el of nodes) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 || r.height === 0) continue;
    const style = getComputedStyle(el);
    if (style.visibility === 'hidden' || style.display === 'none') continue;
    let selector = null;
    if (el.id) selector = '#' + CSS.escape(el.id);
    else if (el.name) selector = el.tagName.toLowerCase() + '[name="' + el.name + '"]';
    let label = (el.innerText || el.value || el.placeholder ||
                 el.getAttribute('aria-label') || '').trim();
    if (!label && el.id) {
      const l = document.querySelector('label[for="' + CSS.escape(el.id) + '"]');
      if (l) label = l.innerText.trim();
    }
    out.push({tag: el.tagName.toLowerCase(), type: el.type || null,
              selector, label: label.slice(0, 80),
              x: Math.round(r.x + r.width / 2), y: Math.round(r.y + r.height / 2)});
    if (out.length >= limit) break;
  }
  return out;
}
"""


class WebAction(Action):
    """One browser action. Coordinates are viewport pixels (1280x800).

    A selector is CSS on the page, or reaches into frames with ``>>>``:
    ``iframe[name="pat"] >>> #form_fname``. Observed elements carry the
    selector and the viewport centre that reach them.
    """

    kind: ActionKind
    selector: Optional[str] = None
    x: Optional[int] = None
    y: Optional[int] = None
    text: Optional[str] = Field(
        default=None, description="fill/type text, or the key for press"
    )
    value: Optional[str] = Field(default=None, description="option for select")
    path: Optional[str] = Field(
        default=None, description="goto: a path or #fragment on the app's origin"
    )
    dy: Optional[int] = Field(default=None, description="scroll: pixels")
    seconds: Optional[float] = Field(default=None, description="wait: <= 5")
    claim: Optional[Literal["success", "failure"]] = Field(
        default=None,
        description="done: did the task's change land in the record?",
    )


class WebObservation(Observation):
    contract: str = CONTRACT_VERSION
    task_id: str = ""
    instruction: str = ""
    url: str = ""
    title: str = ""
    text: str = ""
    elements: list[dict[str, Any]] = Field(default_factory=list)
    screenshot_png_b64: Optional[str] = None
    step: int = 0
    max_steps: int = 0
    last_action_error: Optional[str] = None
    #: Set on the final observation only.
    verdict: Optional[str] = None


class WebState(State):
    env_name: str = ""
    task_id: Optional[str] = None
    split: Optional[str] = None
    done: bool = False
    claim: Optional[str] = None
    verdict: Optional[str] = None
    reward: Optional[float] = None


def _import(ref: str) -> Callable[..., Any]:
    module, _, name = ref.partition(":")
    if not module or not name:
        raise TaskError(f"{ref!r} must be module:function")
    return getattr(importlib.import_module(module), name)


def _serve_records(
    read: Callable[[], list[dict[str, Any]]],
) -> tuple[str, Callable[[], None]]:
    """Serve ``read()`` as ``GET /records`` on an ephemeral loopback port.

    The port is not the app's origin, so the agent's browser cannot reach it.
    A failed read answers 503, which verify-run reads as unreadable.
    """
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802
            if self.path.split("?")[0] != "/records":
                self.send_error(404)
                return
            try:
                body = json.dumps({"records": read()}, default=str).encode()
                status = 200
            except Exception as exc:  # noqa: BLE001 - unreadable, never guessed
                body = json.dumps({"error": type(exc).__name__}).encode()
                status = 503
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def stop() -> None:
        server.shutdown()
        server.server_close()

    return f"http://127.0.0.1:{server.server_address[1]}", stop


def _scalars(body: str) -> set[str]:
    """Every scalar value in a JSON or form body, as text.

    A form field holding JSON (as Frappe sends ``doc=...``) is read through.
    """
    from urllib.parse import parse_qsl

    out: set[str] = set()
    stack: list[Any] = [body]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            stack.extend(item.values())
        elif isinstance(item, list):
            stack.extend(item)
        elif isinstance(item, str):
            try:
                parsed = json.loads(item)
            except ValueError:
                pairs = parse_qsl(item) if "=" in item else []
                if pairs and item is body:
                    stack.extend(value for _, value in pairs)
                else:
                    out.add(item)
                continue
            if isinstance(parsed, (dict, list)):
                stack.append(parsed)
            else:
                out.add(item if isinstance(parsed, str) else str(parsed))
                out.add(item)
        elif item is not None:
            out.add(str(item))
    return out


def _frame_selector(frame: Any) -> Optional[str]:
    """The iframe element's selector in its parent: by name, else by id."""
    if frame.name:
        return f'iframe[name="{frame.name}"]'
    try:
        element_id = frame.frame_element().get_attribute("id")
    except Exception:  # noqa: BLE001 - detached frame
        return None
    return f'iframe[id="{element_id}"]' if element_id else None


def _redact(url: str) -> str:
    """The URL the agent sees: no query string (it can carry the fault)."""
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))


class OpenAdaptWebEnvironment(Environment[WebAction, WebObservation, WebState]):
    """A record-checked browser task, served over the OpenEnv protocol."""

    SUPPORTS_CONCURRENT_SESSIONS = True
    # Each instance runs its browser on its own worker thread (sync Playwright
    # allows one instance per thread), so OpenEnv may call from any thread and
    # sessions run in parallel.
    REQUIRES_SINGLE_THREAD_EXECUTOR = False

    def __init__(self, env_dir: str | Path, *, screenshots: bool = False) -> None:
        super().__init__()
        self.env: EnvDirectory = load_environment(env_dir)
        self.screenshots = screenshots
        self._state = WebState(env_name=self.env.manifest.name)
        self._task: Optional[Task] = None
        self._judge: Optional[EpisodeJudge] = None
        self._app_url: Optional[str] = None
        self._app_stop: Optional[Callable[[], None]] = None
        self._pw: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None
        self._judgement: Optional[Judgement] = None
        self._worker: Optional[ThreadPoolExecutor] = None
        self._records_url: Optional[str] = None
        self._attempted = False
        self._records_stop: Optional[Callable[[], None]] = None

    def _on_worker(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._worker is None:
            self._worker = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="rl-env"
            )
        return self._worker.submit(fn, *args, **kwargs).result()

    # -- task discovery (OpenEnv TaskProvider) ------------------------------
    def list_splits(self) -> list[str]:
        return self.env.splits()

    def num_tasks(self, split: str = PUBLIC_SPLIT) -> int:
        return len(self.env.split(split))

    def get_task(self, split: str, index: int) -> dict[str, Any]:
        return {"index": index, **self.env.split(split)[index].public_view()}

    def list_tasks(self, split: str = PUBLIC_SPLIT) -> list[dict[str, Any]]:
        return [
            {"index": i, **task.public_view()}
            for i, task in enumerate(self.env.split(split))
        ]

    def get_metadata(self) -> EnvironmentMetadata:
        manifest = self.env.manifest
        return EnvironmentMetadata(
            name=manifest.name,
            description=manifest.description or manifest.title,
            version=CONTRACT_VERSION,
        )

    # -- app and browser ----------------------------------------------------
    def _ensure_app(self) -> str:
        if self._app_url is not None:
            return self._app_url
        app = self.env.manifest.app
        if app.kind == "inprocess":
            assert app.serve is not None
            url, _store, stop = _import(app.serve)(0)
            self._app_stop = stop
        else:
            import os

            url = os.environ.get(app.url_env or "", "") or app.url_default or ""
        if app.records:
            self._records_url, self._records_stop = _serve_records(_import(app.records))
        self._app_url = url.rstrip("/")
        return self._app_url

    def _reset_app(self, task: Task) -> None:
        spec = self.env.manifest.app.reset
        if "http_post" in spec:
            response = httpx.post(
                self._ensure_app() + spec["http_post"], json=task.setup, timeout=30
            )
            response.raise_for_status()
        elif "python" in spec:
            _import(spec["python"])(**task.setup)

    def _allowed(self, url: str, method: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme in ("data", "blob", "about"):
            return True
        app = urlsplit(self._ensure_app())
        if (parts.scheme, parts.netloc) != (app.scheme, app.netloc):
            return False
        for rule in self.env.manifest.agent.blocked_paths:
            verb, _, path = rule.rpartition(" ")
            if verb and verb.upper() != method.upper():
                continue
            if parts.path.startswith(path):
                return False
        return True

    def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.sync_api import sync_playwright

        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(headless=True)
        return self._new_context()

    def _new_context(self) -> Any:
        if self._context is not None:
            self._context.close()
        self._context = self._browser.new_context(viewport=VIEWPORT)

        def route(route: Any) -> None:
            request = route.request
            if self._allowed(request.url, request.method):
                self._note_write(request)
                route.continue_()
            else:
                route.abort("blockedbyclient")

        self._context.route("**/*", route)
        self._page = self._context.new_page()
        self._page.set_default_timeout(10_000)
        return self._page

    # -- OpenEnv API --------------------------------------------------------
    def reset(self, *args: Any, **kwargs: Any) -> WebObservation:
        return self._on_worker(self._reset, *args, **kwargs)

    def step(self, *args: Any, **kwargs: Any) -> WebObservation:
        return self._on_worker(self._step, *args, **kwargs)

    @property
    def state(self) -> WebState:
        return self._state

    def close(self) -> None:
        if self._worker is None:
            return
        try:
            self._on_worker(self._close)
        finally:
            self._worker.shutdown(wait=False)
            self._worker = None

    def _reset(
        self,
        seed: Optional[int] = None,
        episode_id: Optional[str] = None,
        task_id: Optional[str] = None,
        split: str = PUBLIC_SPLIT,
        index: Optional[int] = None,
        screenshots: Optional[bool] = None,
        **_: Any,
    ) -> WebObservation:
        if task_id is not None:
            task = self.env.task(task_id)
        else:
            pool = self.env.split(split)
            if not pool:
                raise TaskError(f"no tasks in split {split!r}")
            pick = (
                index if index is not None else random.Random(seed).randrange(len(pool))
            )
            task = pool[pick]
        if screenshots is not None:
            self.screenshots = screenshots
        if self._judge is not None:
            self._judge.close()
        self._task = task
        self._judgement = None
        self._attempted = False
        app_url = self._ensure_app()
        self._reset_app(task)
        cfg = render_verify_config(
            task.verify_text or self.env.verify_text, app_url, self._records_url
        )
        episode = episode_id or uuid.uuid4().hex
        self._judge = EpisodeJudge(cfg, task.params, episode_id=episode)
        self._ensure_page()
        page = self._new_context()
        page.goto(app_url + task.start_path, wait_until="load")
        if self.env.manifest.app.prepare:
            _import(self.env.manifest.app.prepare)(page, app_url)
        self._state = WebState(
            env_name=self.env.manifest.name,
            episode_id=episode,
            step_count=0,
            task_id=task.id,
            split=task.split,
        )
        return self._observe(reward=None, done=False)

    def _step(
        self, action: WebAction, timeout_s: Optional[float] = None, **_: Any
    ) -> WebObservation:
        if self._task is None or self._page is None:
            raise RuntimeError("call reset() before step()")
        if self._state.done:
            return self._observe(reward=0.0, done=True, error="episode is over")
        self._state.step_count += 1
        if action.kind == "done":
            return self._finish(action.claim)
        error = self._act(action)
        if self._state.step_count >= self.env.manifest.agent.max_steps:
            return self._finish(None, error=error)
        return self._observe(reward=0.0, done=False, error=error)

    def _close(self) -> None:
        for closer in (
            lambda: self._context and self._context.close(),
            lambda: self._browser and self._browser.close(),
            lambda: self._pw and self._pw.stop(),
            lambda: self._app_stop and self._app_stop(),
            lambda: self._records_stop and self._records_stop(),
            lambda: self._judge and self._judge.close(),
        ):
            try:
                closer()
            except Exception:  # noqa: BLE001 - best-effort teardown
                pass
        self._context = self._browser = self._pw = self._page = None
        self._app_stop = None
        self._records_stop = None
        self._records_url = None
        self._app_url = None
        self._judge = None

    # -- internals ----------------------------------------------------------
    def _act(self, action: WebAction) -> Optional[str]:
        page = self._page
        try:
            if action.kind == "click":
                if action.selector:
                    self._locator(action.selector).click()
                elif action.x is not None and action.y is not None:
                    page.mouse.click(action.x, action.y)
                else:
                    return "click needs a selector or x and y"
            elif action.kind == "fill":
                if not action.selector:
                    return "fill needs a selector"
                self._locator(action.selector).fill(action.text or "")
            elif action.kind == "type":
                page.keyboard.type(action.text or "")
            elif action.kind == "press":
                page.keyboard.press(action.text or "Enter")
            elif action.kind == "select":
                if not action.selector:
                    return "select needs a selector"
                self._locator(action.selector).select_option(action.value or "")
            elif action.kind == "scroll":
                page.mouse.wheel(0, action.dy or 400)
            elif action.kind == "goto":
                return self._goto(action.path or "/")
            elif action.kind == "wait":
                time.sleep(min(max(action.seconds or 1.0, 0.0), MAX_WAIT_S))
        except Exception as exc:  # noqa: BLE001 - the agent sees the failure
            return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        time.sleep(self.env.manifest.agent.settle_s)
        return None

    def _locator(self, selector: str) -> Any:
        """``#id`` on the page, or ``iframe[name="x"] >>> #id`` inside frames."""
        *frames, target = selector.split(FRAME_SEP)
        scope: Any = self._page
        for frame in frames:
            scope = scope.frame_locator(frame.strip())
        return scope.locator(target.strip()).first

    def _frames(self) -> list[tuple[str, Any]]:
        """Each frame with the selector prefix that reaches it from the page."""
        page = self._page
        out: list[tuple[str, Any]] = [("", page.main_frame)]
        for frame in page.frames:
            if frame is page.main_frame:
                continue
            chain: list[str] = []
            node = frame
            while node.parent_frame is not None:
                hop = _frame_selector(node)
                if hop is None:
                    break
                chain.append(hop)
                node = node.parent_frame
            else:
                out.append(("".join(c + FRAME_SEP for c in reversed(chain)), frame))
        return out

    def _page_content(self) -> tuple[str, list[dict[str, Any]]]:
        texts: list[str] = []
        elements: list[dict[str, Any]] = []
        for prefix, frame in self._frames():
            try:
                if prefix:
                    box = frame.frame_element().bounding_box()
                    if not box or box["width"] == 0 or box["height"] == 0:
                        continue
                    dx, dy = box["x"], box["y"]
                else:
                    dx = dy = 0.0
                body = frame.inner_text("body", timeout=2000).strip()
                found = frame.evaluate(_ELEMENTS_JS, MAX_ELEMENTS - len(elements))
            except Exception:  # noqa: BLE001 - a frame mid-navigation
                continue
            if body:
                texts.append(
                    f"[frame {prefix.strip(FRAME_SEP)}]\n{body}" if prefix else body
                )
            for item in found:
                if item["selector"]:
                    item["selector"] = prefix + item["selector"]
                item["x"] = round(item["x"] + dx)
                item["y"] = round(item["y"] + dy)
                elements.append(item)
            if len(elements) >= MAX_ELEMENTS:
                break
        return "\n\n".join(texts)[:MAX_TEXT], elements

    def _note_write(self, request: Any) -> None:
        """Record whether the browser sent the task's write with its values."""
        if self._task is None or self._attempted:
            return
        path = urlsplit(request.url).path
        for rule in self.env.manifest.agent.write_requests:
            verb, _, prefix = rule.rpartition(" ")
            if (verb and verb.upper() != request.method.upper()) or not path.startswith(
                prefix
            ):
                continue
            sent = _scalars(request.post_data or "")
            if all(str(v) in sent for v in self._task.params.values()):
                self._attempted = True
                return

    def _goto(self, path: str) -> Optional[str]:
        assert self._task is not None
        if "://" in path or path.startswith("//"):
            return "goto takes a path on the app's origin, not a URL"
        start = urlsplit(self._task.start_path)
        target = urlsplit(path)
        if target.path or target.query:
            # A new path keeps the task's query, which may carry the fault.
            url = urlunsplit(
                ("", "", target.path or start.path, start.query, target.fragment)
            )
        else:
            url = urlunsplit(("", "", start.path, start.query, target.fragment))
        try:
            self._page.goto(self._ensure_app() + url, wait_until="load")
        except Exception as exc:  # noqa: BLE001
            return f"{type(exc).__name__}: {str(exc).splitlines()[0][:200]}"
        return None

    def _finish(
        self, claim: Optional[str], error: Optional[str] = None
    ) -> WebObservation:
        assert self._task is not None and self._judge is not None
        # Let any write the page already sent reach the record first.
        time.sleep(max(self.env.manifest.agent.settle_s, 0.5))
        judgement = self._judge.judge(claim, self._task.feasible, self._attempted)
        self._judgement = judgement
        self._state.done = True
        self._state.claim = claim
        self._state.verdict = judgement.verdict
        self._state.reward = judgement.reward
        return self._observe(
            reward=judgement.reward,
            done=True,
            error=error,
            verdict=judgement.verdict,
            extra={
                "judgement": judgement.as_dict(),
                "outcome": judgement.vector(self._state.step_count, self._task.fault),
                "fault": self._task.fault,
            },
        )

    def _observe(
        self,
        *,
        reward: Optional[float],
        done: bool,
        error: Optional[str] = None,
        verdict: Optional[str] = None,
        extra: Optional[dict[str, Any]] = None,
    ) -> WebObservation:
        assert self._task is not None
        page = self._page
        text, title, url, elements, shot = "", "", "", [], None
        try:
            url = _redact(page.url)
            title = page.title()
            text, elements = self._page_content()
            if self.screenshots:
                shot = base64.b64encode(page.screenshot(type="png")).decode("ascii")
        except Exception as exc:  # noqa: BLE001 - a page mid-navigation
            error = error or f"observation incomplete: {type(exc).__name__}"
        return WebObservation(
            done=done,
            reward=reward,
            metadata={"env": self.env.manifest.name, **(extra or {})},
            task_id=self._task.id,
            instruction=self._task.instruction,
            url=url,
            title=title,
            text=text,
            elements=json.loads(json.dumps(elements)),
            screenshot_png_b64=shot,
            step=self._state.step_count,
            max_steps=self.env.manifest.agent.max_steps,
            last_action_error=error,
            verdict=verdict,
        )
