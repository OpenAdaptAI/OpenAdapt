"""Hosted public Seals: ``openadapt seal publish``, ``unpublish`` and link checks.

A run's own Seal (``seal.json``) signs its whole ``report.json``, which stays on
this computer. Publishing builds the run's *public* Seal (``public-seal.json``,
:mod:`openadapt_flow.public_seal`): counts, outcomes, the step strip, digests
and the hour the run started, signed by the same key. The program name is in
it only with ``--names`` (or for the synthetic tutorial), because a program
name can be a person's name. Every public field is listed before consent; only
that file is uploaded, and only after the person says yes:

* in a terminal, a y/N prompt that defaults to No;
* anywhere else, ``--yes``; under CI, ``--yes --allow-ci``.

Cloud checks the signature, stores the file behind an unguessable id and
returns ``{id, url, delete_token}``. The token is saved to
``RUN_DIR/seal.publish.json`` (mode 0600) for ``openadapt seal unpublish``.

Every network call goes through a :data:`Transport`, so tests replace it.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import stat
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import urlsplit

DEFAULT_ENDPOINT = "https://app.openadapt.ai"
ENV_ENDPOINT = "OPENADAPT_SEAL_ENDPOINT"
PUBLISH_FILE = "seal.publish.json"
API_PATH = "/api/public/local-seals"
#: Answers ``{"enabled": true}`` only while Cloud accepts uploads.
STATUS_PATH = API_PATH + "/status"
TIMEOUT_S = 15.0
#: The quickstart waits at most this long to learn whether to offer.
STATUS_TIMEOUT_S = 1.0
_ID = re.compile(r"^[A-Za-z0-9_-]{43}\Z")
_QUICKSTART_ROOT = re.compile(r"openadapt-quickstart(-[0-9]+)?")

#: The exact consent question; ``{host}`` is the endpoint's host.
PROMPT = (
    "This uploads the signed Seal to {host} and gives you a link. Anyone "
    "with the link can see it. It does not upload your inputs, screenshots or "
    "records. Type show for the exact bytes. Publish? [y/N] "
)


@dataclass(frozen=True)
class Response:
    status: int
    headers: Mapping[str, str]
    body: bytes


#: ``transport(method, url, body, headers) -> Response``. Raises OSError when
#: the server can't be reached.
Transport = Callable[[str, str, Optional[bytes], Mapping[str, str]], Response]


class PublishError(RuntimeError):
    """A reason nothing was published (or unpublished), in one plain line."""

    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: a redirected POST would be re-sent as a GET elsewhere."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def urllib_transport(
    method: str,
    url: str,
    body: Optional[bytes],
    headers: Mapping[str, str],
    timeout: float = TIMEOUT_S,
) -> Response:
    """The real transport: one request to ``url``, no redirects followed."""
    request = urllib.request.Request(
        url, data=body, method=method, headers=dict(headers)
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as reply:
            return Response(reply.status, dict(reply.headers), reply.read())
    except urllib.error.HTTPError as reply:
        return Response(reply.code, dict(reply.headers or {}), reply.read() or b"")


def public_bytes(doc: Mapping[str, Any]) -> bytes:
    """The exact upload bytes: sorted-key compact JSON, as the engine writes it."""
    return json.dumps(
        dict(doc), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def available() -> bool:
    """True when the installed openadapt-flow can build public Seals."""
    import importlib.util

    try:
        return importlib.util.find_spec("openadapt_flow.public_seal") is not None
    except (ImportError, ValueError):
        return False


def endpoint(
    value: Optional[str] = None, env: Optional[Mapping[str, str]] = None
) -> str:
    """The Cloud origin: ``value``, else ``$OPENADAPT_SEAL_ENDPOINT``, else production."""
    env = os.environ if env is None else env
    chosen = (value or env.get(ENV_ENDPOINT) or DEFAULT_ENDPOINT).rstrip("/")
    parts = urlsplit(chosen)
    local = parts.hostname in ("localhost", "127.0.0.1", "::1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise PublishError(f"the Seal endpoint must be https: {chosen}", 2)
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise PublishError(f"the Seal endpoint is an origin only: {chosen}", 2)
    return chosen


def host_of(origin: str) -> str:
    return urlsplit(origin).netloc


def hosted_seals_live(
    origin: str,
    *,
    transport: Optional[Transport] = None,
    timeout_s: float = STATUS_TIMEOUT_S,
) -> bool:
    """True only when ``origin`` answers 200 ``{"enabled": true}`` within ``timeout_s``.

    Anything else (an error, a timeout, a 404 from a Cloud without the route,
    ``{"enabled": false}``) is False.
    """
    return _status_true(origin + STATUS_PATH, transport=transport, timeout_s=timeout_s)


def _status_true(
    url: str,
    *,
    transport: Optional[Transport] = None,
    timeout_s: float = STATUS_TIMEOUT_S,
) -> bool:
    """GET ``url``; True only for 200 ``{"enabled": true}`` within ``timeout_s``.

    The wait is bounded with a thread, since a socket timeout does not bound
    the DNS lookup.
    """
    import threading

    headers = {"Accept": "application/json"}
    if transport is None:
        real = _module_transport()

        def transport(method, url, body, headers):  # type: ignore[no-redef]
            return real(method, url, body, headers, timeout_s)

    answer: list[Response] = []

    def ask() -> None:
        try:
            answer.append(transport("GET", url, None, headers))
        except Exception:  # noqa: BLE001 - any failure means "no"
            pass

    worker = threading.Thread(target=ask, name="seal-status", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive() or not answer or answer[0].status != 200:
        return False
    try:
        data = json.loads(answer[0].body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return False
    return data == {"enabled": True}


def in_ci(env: Optional[Mapping[str, str]] = None) -> bool:
    env = os.environ if env is None else env
    value = str(env.get("CI", "")).strip().lower()
    return value not in ("", "0", "false", "no")


# -- consent ----------------------------------------------------------------


def consent(
    *,
    yes: bool,
    allow_ci: bool,
    interactive: bool,
    ask: Callable[[str], str],
    host: str,
    show: Optional[Callable[[], None]] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """True only on an explicit yes. Raises PublishError (exit 2) when none can be had.

    CI refuses even with ``--yes`` unless ``--allow-ci`` is passed too. A
    terminal gets the y/N prompt, default No. Anything else needs ``--yes``.
    """
    if in_ci(env):
        if yes and allow_ci:
            return True
        raise PublishError(
            "Not published: CI is set. Pass --yes --allow-ci to publish from CI.", 2
        )
    if yes:
        return True
    if not interactive:
        raise PublishError(
            "Not published: no terminal to ask. Pass --yes to publish.", 2
        )
    return _ask_consent(PROMPT.format(host=host), ask, show)


def _ask_consent(
    prompt: str, ask: Callable[[str], str], show: Optional[Callable[[], None]]
) -> bool:
    """Inspect locally as often as requested; only an explicit yes authorizes a send."""
    try:
        while True:
            answer = ask(prompt).strip().lower()
            if show is None or answer not in ("show", "s", "--show"):
                break
            show()
    except EOFError:
        return False
    return answer in ("y", "yes")


# -- building ---------------------------------------------------------------


def _engine() -> Any:
    try:
        from openadapt_flow import public_seal
    except ImportError as exc:
        raise PublishError(
            "This openadapt-flow cannot build public Seals. Upgrade with: "
            "pip install --upgrade openadapt"
        ) from exc
    return public_seal


def tutorial_provenance(run_dir: Path) -> Optional[str]:
    """``synthetic-tutorial`` when this run or a sibling run says it is the tutorial.

    The quickstart's stopped run has no receipt of its own, but it sits next
    to the checked runs, whose receipts say ``synthetic-tutorial``. Siblings
    count only inside a quickstart folder (``openadapt-quickstart[-N]``), so a
    real run kept next to a tutorial run is not called practice.
    """
    folders = [run_dir]
    if _QUICKSTART_ROOT.fullmatch(run_dir.parent.name):
        try:
            folders += [
                p for p in run_dir.parent.iterdir() if p.is_dir() and p != run_dir
            ]
        except OSError:
            pass
    for folder in folders:
        try:
            receipt = json.loads((folder / "receipt.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (
            isinstance(receipt, dict)
            and receipt.get("provenance") == "synthetic-tutorial"
        ):
            return "synthetic-tutorial"
    return None


def build(
    run_dir: Path,
    *,
    names: Optional[bool] = None,
    github: Optional[str] = None,
    provenance: Optional[str] = None,
) -> dict[str, Any]:
    """Build (or reuse) and write ``RUN_DIR/public-seal.json``. No network.

    ``names``: True includes the program name, False leaves it out, None
    (the default) includes it only for the synthetic tutorial.
    """
    public_seal = _engine()
    from openadapt_flow.offline_seal import LocalSealError, seal_home, signer_for

    try:
        signer = signer_for(seal_home())
        doc = public_seal.build_public_seal(
            run_dir,
            signer=signer,
            names=names,
            github=github,
            provenance=provenance or tutorial_provenance(run_dir),
        )
    except LocalSealError as exc:
        hint = ""
        if exc.code == "name_not_allowed":
            hint = " Run again without --names."
        raise PublishError(f"No public Seal for {run_dir}: {exc}.{hint}") from exc
    public_seal.write_public_seal(run_dir, doc)
    return doc


def card_text(doc: Mapping[str, Any]) -> str:
    """List the actual public fields. Repeated step fields share a line in run order."""
    lines = []
    for section, fields in doc.items():
        lines.append(section.title())
        values: dict[str, list[str]] = {}
        for name, value in fields.items():
            for label, shown in _preview_fields(name, value):
                values.setdefault(label, []).append(shown)
        for label, items in values.items():
            runs = []
            for item, group in itertools.groupby(items):
                count = sum(1 for _ in group)
                runs.append(item if count == 1 else f"{item} × {count}")
            label = label.replace("[]", " (in order)").replace(".", " / ")
            label = label.replace("_", " ").capitalize().replace("sha256", "SHA-256")
            if label.endswith(" id"):
                label = label[:-2] + "ID"
            lines.append(f"  {label}: {', '.join(runs)}")
    return "\n".join(lines)


def _preview_seals(
    docs: list[tuple[Path, dict[str, Any]]], say: Callable[[str], None]
) -> None:
    for run_dir, doc in docs:
        say(card_text(doc))
        say(f"Exact upload: {run_dir.resolve() / 'public-seal.json'}")
    say(
        "Type --show at the prompt to read the exact bytes. "
        "For the seal publish command, add --show to inspect without uploading."
    )
    say(
        "Anyone who sees two Seals can compare their key IDs to see "
        "if the same key signed both."
    )


def _preview_fields(name: str, value: Any) -> Iterator[tuple[str, str]]:
    """Yield a path and value for each leaf, including explicit nulls and empty lists."""
    if isinstance(value, dict) and value:
        for key, child in value.items():
            yield from _preview_fields(f"{name}.{key}", child)
    elif isinstance(value, list) and value:
        for child in value:
            yield from _preview_fields(f"{name}[]", child)
    else:
        if isinstance(value, str):
            shown = value
            if name.endswith(("_sha256", "_digest")) and re.fullmatch(
                "[0-9a-f]{64}", value
            ):
                shown = f"{value[:8]}…{value[-8:]}"
        else:
            shown = json.dumps(value)
        yield name, shown


# -- publishing ---------------------------------------------------------------


def read_record(run_dir: Path) -> Optional[dict[str, Any]]:
    try:
        data = json.loads((run_dir / PUBLISH_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_record(run_dir: Path, record: Mapping[str, Any]) -> Path:
    path = run_dir / PUBLISH_FILE
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(dict(record), indent=2, sort_keys=True) + "\n")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)
    return path


def _reason(response: Response) -> str:
    try:
        data = json.loads(response.body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return f"HTTP {response.status}"
    if isinstance(data, dict):
        for key in ("error", "reason", "message"):
            if isinstance(data.get(key), str):
                return data[key][:200]
    return f"HTTP {response.status}"


def _header(response: Response, name: str) -> Optional[str]:
    for key, value in response.headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _quote(path: Path) -> str:
    import shlex

    return shlex.quote(str(path))


def link_for(origin: str, seal_id: str) -> str:
    return f"{origin}/seal#{seal_id}"


def upload(
    run_dir: Path,
    doc: Mapping[str, Any],
    *,
    origin: str,
    transport: Transport,
) -> dict[str, Any]:
    """POST the exact bytes of ``public-seal.json``; save ``{id, url, delete_token}``."""
    body = public_bytes(doc)
    try:
        response = transport(
            "POST",
            origin + API_PATH,
            body,
            {"Content-Type": "application/json", "Accept": "application/json"},
        )
    except OSError as exc:
        raise PublishError(f"Not published: could not reach {host_of(origin)} ({exc}).")
    if response.status == 409:
        raise PublishError(
            "Not published: this Seal is already online. Its link was given to "
            "whoever published it first."
        )
    if response.status == 429:
        wait = _header(response, "Retry-After")
        later = f" Try again in {wait} seconds." if wait else " Try again later."
        raise PublishError("Not published: too many uploads." + later)
    if response.status == 413:
        raise PublishError("Not published: the public Seal is too large.")
    if response.status == 503:
        raise PublishError("Not published: the service is busy. Try again later.")
    if response.status != 201:
        raise PublishError(f"Not published: {_reason(response)}.")
    try:
        data = json.loads(response.body.decode("utf-8"))
        seal_id = str(data["id"])
        token = str(data["delete_token"])
    except (UnicodeDecodeError, ValueError, KeyError, TypeError) as exc:
        raise PublishError(
            "Not published: the server's answer was not readable."
        ) from exc
    if not _ID.match(seal_id) or not token:
        raise PublishError("Not published: the server's answer was not readable.")
    # The link is built here, from the endpoint the person chose, not taken
    # from the response.
    record = {
        "id": seal_id,
        "url": link_for(origin, seal_id),
        "delete_token": token,
    }
    _write_record(run_dir, record)
    return record


def unpublish(run_dir: Path, *, origin: Optional[str], transport: Transport) -> str:
    """DELETE the run's hosted Seal with the saved token. Returns a status line."""
    record = read_record(run_dir)
    if record is None or not _ID.match(str(record.get("id", ""))):
        raise PublishError(
            f"Nothing to unpublish: {run_dir / PUBLISH_FILE} is missing."
        )
    seal_id = str(record["id"])
    if origin is None:
        saved = urlsplit(str(record.get("url", "")))
        origin = endpoint(f"{saved.scheme}://{saved.netloc}" if saved.netloc else None)
    try:
        response = transport(
            "DELETE",
            f"{origin}{API_PATH}/{seal_id}",
            None,
            {"Authorization": f"Delete {record.get('delete_token', '')}"},
        )
    except OSError as exc:
        raise PublishError(
            f"Not unpublished: could not reach {host_of(origin)} ({exc})."
        )
    if response.status in (204, 410):
        (run_dir / PUBLISH_FILE).unlink(missing_ok=True)
        if response.status == 204:
            return f"Unpublished. {link_for(origin, seal_id)} no longer shows the Seal."
        return f"Already gone: {link_for(origin, seal_id)} shows no Seal."
    if response.status == 404:
        # Keep the token: a 404 can be the wrong server, not a missing Seal,
        # and the file is the only copy of it.
        raise PublishError(
            f"Not unpublished: {host_of(origin)} has no Seal {seal_id}. "
            f"The delete token stays in {run_dir / PUBLISH_FILE}."
        )
    if response.status == 403:
        raise PublishError("Not unpublished: the saved delete token was refused.")
    raise PublishError(f"Not unpublished: {_reason(response)}.")


# -- checking a link ------------------------------------------------------------


def parse_link(url: str, origin: Optional[str] = None) -> Optional[tuple[str, str]]:
    """``(origin, id)`` for a hosted Seal link (``<origin>/seal#<id>``), else None.

    Accepts the production host and the configured endpoint only.
    """
    parts = urlsplit(url)
    if parts.path.rstrip("/") != "/seal" or not _ID.match(parts.fragment or ""):
        return None
    link_origin = f"{parts.scheme}://{parts.netloc}"
    allowed = {DEFAULT_ENDPOINT}
    try:
        allowed.add(endpoint(origin))
    except PublishError:
        pass
    if link_origin not in allowed:
        return None
    return link_origin, parts.fragment


def check_link(
    url: str,
    *,
    transport: Transport,
    origin: Optional[str] = None,
    say: Callable[[str], None] = print,
) -> int:
    """Fetch a hosted Seal and check its signature here. 0 when valid."""
    parsed = parse_link(url, origin)
    if parsed is None:
        say(f"not valid: not a hosted Seal link: {url}")
        return 1
    link_origin, seal_id = parsed
    try:
        response = transport(
            "GET",
            f"{link_origin}{API_PATH}/{seal_id}",
            None,
            {"Accept": "application/json"},
        )
    except OSError as exc:
        say(f"not checked: could not reach {host_of(link_origin)} ({exc})")
        return 1
    if response.status == 410:
        say("not valid: this Seal was deleted by whoever published it")
        return 1
    if response.status == 404:
        say("not valid: no Seal at this link")
        return 1
    if response.status != 200:
        say(f"not checked: {_reason(response)}")
        return 1
    try:
        data = json.loads(response.body.decode("utf-8"))
        doc = {"card": data["card"], "seal": data["seal"]}
    except (UnicodeDecodeError, ValueError, KeyError, TypeError):
        say("not valid: the server's answer was not readable")
        return 1
    public_seal = _engine()
    # Checked here, from the card and Seal alone; the server's own
    # ``verified`` flag is not trusted.
    result = public_seal.verify_public_seal(doc)
    if not result.ok:
        say(f"not valid: {result.reason}")
        return 1
    from openadapt_flow.offline_seal import seal_sha256

    if data.get("seal_sha256") not in (None, seal_sha256(doc["seal"])):
        say("not valid: the server names a different Seal digest")
        return 1
    card = doc["card"]
    say(
        f"Signature: valid. This Seal was signed by key {result.key_id}, and the "
        "summary has not changed since."
    )
    identity = data.get("identity") if isinstance(data.get("identity"), dict) else {}
    if identity.get("status") == "github" and isinstance(identity.get("login"), str):
        when = identity.get("checked_at") or "an unrecorded date"
        say(
            f"Key holder: {host_of(link_origin)} reports this key was listed at "
            f"github.com/{identity['login']}.keys on {when}."
        )
    else:
        say("Key holder: not linked to a person or company. Anyone can make a key.")
    say(
        "Who ran it: the key holder reports these results. OpenAdapt did not watch this run."
    )
    if card.get("provenance") == "synthetic-tutorial":
        say("Practice run on a test app.")
    say(
        f"Outcome {card['outcome']}: {card['steps_ok']} of {card['steps_total']} "
        f"steps, {card['model_calls']} model calls."
    )
    return 0


# -- one publish, start to finish ----------------------------------------------


def _module_transport() -> Transport:
    # Looked up at call time, so a test can replace the module's transport.
    return globals()["urllib_transport"]


def publish(
    run_dir: Path,
    *,
    yes: bool = False,
    allow_ci: bool = False,
    dry_run: bool = False,
    names: Optional[bool] = None,
    github: Optional[str] = None,
    endpoint_value: Optional[str] = None,
    provenance: Optional[str] = None,
    interactive: Optional[bool] = None,
    ask: Callable[[str], str] = input,
    say: Callable[[str], None] = print,
    transport: Optional[Transport] = None,
    command: Callable[[str], str] = lambda rest: f"openadapt {rest}",
) -> Optional[str]:
    """Build, show, ask, upload. Returns the link, or None when nothing was sent.

    All public fields are shown before anything is asked or sent;
    ``yes`` skips the question, never the preview. Raises :class:`PublishError`
    with an exit code for each refusal.
    """
    run_dir = Path(run_dir)
    origin = endpoint(endpoint_value)
    existing = read_record(run_dir)
    if existing is not None and existing.get("url") and not dry_run:
        say(f"Already published: {existing['url']}")
        say(f"To take it down: {command(f'seal unpublish {_quote(run_dir)}')}")
        return str(existing["url"])
    doc = build(run_dir, names=names, github=github, provenance=provenance)
    _preview_seals([(run_dir, doc)], say)
    if dry_run:
        say(f"Dry run: wrote {run_dir / 'public-seal.json'}. Nothing was uploaded.")
        return None
    if interactive is None:
        interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if not consent(
        yes=yes,
        allow_ci=allow_ci,
        interactive=interactive,
        ask=ask,
        host=host_of(origin),
        show=lambda: say(public_bytes(doc).decode("utf-8")),
    ):
        say("Not published.")
        return None
    record = upload(
        run_dir, doc, origin=origin, transport=transport or _module_transport()
    )
    say(f"Published: {record['url']}")
    say(f"Delete token saved to {run_dir / PUBLISH_FILE} (only you have it).")
    say(f"To take it down: {command(f'seal unpublish {_quote(run_dir)}')}")
    return str(record["url"])


#: Answers ``{"enabled": true}`` only while Cloud shows two Seals on one page
#: (``/seal#<id>,<id>``). Older Clouds answer 404, and the CLI prints one link
#: per Seal instead.
PAIRS_STATUS_PATH = "/api/public/local-seal-pairs/status"

#: The quickstart's one question, for both of its Seals.
QUICKSTART_PROMPT = (
    "This uploads {subjects} to {host} and gives you a link. "
    "Anyone with the link can see the public fields above. They hold no inputs, screenshots or "
    "records, and the runs used synthetic data. Publish? [y/N] "
)


def pair_link(origin: str, seal_ids: list[str]) -> str:
    return f"{origin}/seal#{','.join(seal_ids)}"


def pairs_live(origin: str, *, transport: Optional[Transport] = None) -> bool:
    """True only when Cloud's pair status route answers ``{"enabled": true}``."""
    return _status_true(origin + PAIRS_STATUS_PATH, transport=transport)


def publish_quickstart(
    run_dirs: list[Path],
    *,
    ask: Callable[[str], str],
    say: Callable[[str], None] = print,
    endpoint_value: Optional[str] = None,
    transport: Optional[Transport] = None,
    command: Callable[[str], str] = lambda rest: f"openadapt {rest}",
) -> Optional[str]:
    """Offer the quickstart's Seals (the checked run, then the stopped run) at once.

    Each card is built and written to its run's ``public-seal.json`` first, and
    every public field is shown with the full path to its file, so the exact
    bytes can be read before answering. One y/N question, default No, covers
    both. Returns the link to open (one page for both when Cloud supports it),
    or None when nothing was sent.
    """
    origin = endpoint(endpoint_value)
    docs = []
    for run_dir in run_dirs:
        docs.append(
            (Path(run_dir), build(Path(run_dir), provenance="synthetic-tutorial"))
        )
    if not docs:
        return None
    _preview_seals(docs, say)

    def show() -> None:
        for _, doc in docs:
            say(public_bytes(doc).decode("utf-8"))

    subjects = "1 signed Seal" if len(docs) == 1 else f"{len(docs)} signed Seals"
    if not _ask_consent(
        QUICKSTART_PROMPT.format(subjects=subjects, host=host_of(origin)), ask, show
    ):
        say("Not published.")
        return None
    sender = transport or _module_transport()
    records = []
    for run_dir, doc in docs:
        existing = read_record(run_dir)
        if existing is not None and existing.get("id"):
            records.append(existing)
            continue
        records.append(upload(run_dir, doc, origin=origin, transport=sender))
    ids = [str(record["id"]) for record in records]
    if len(ids) > 1 and pairs_live(origin, transport=transport):
        link = pair_link(origin, ids)
        say(f"Published. Both runs on one page: {link}")
    else:
        link = str(records[-1]["url"])
        for (run_dir, doc), record in zip(docs, records):
            say(f"Published {doc['card'].get('outcome', '')}: {record['url']}")
    say("Each run folder keeps its delete token. To take one down:")
    for run_dir, _ in docs:
        say(f"  {command(f'seal unpublish {_quote(run_dir)}')}")
    return link


__all__ = [
    "DEFAULT_ENDPOINT",
    "ENV_ENDPOINT",
    "PROMPT",
    "PUBLISH_FILE",
    "PublishError",
    "Response",
    "Transport",
    "build",
    "card_text",
    "check_link",
    "consent",
    "endpoint",
    "hosted_seals_live",
    "in_ci",
    "parse_link",
    "pair_link",
    "pairs_live",
    "publish",
    "publish_quickstart",
    "unpublish",
    "upload",
]
