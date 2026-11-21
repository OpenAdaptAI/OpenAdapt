"""Claim published Seals into your Cloud workspace: ``openadapt seal claim``.

``openadapt seal publish`` and the quickstart upload Seals anonymously: Cloud
stores each one with no account. After ``openadapt login``, this module
claims the Seals this computer published into the signed-in workspace, so
they show on the dashboard.

The proof is a signature by the Seal key that signed them, over bytes that
name the Cloud origin, a five-minute challenge Cloud minted for this
workspace and ingest token, and each Seal's id and digest (Cloud's
``src/lib/localSealClaims.ts``). Nothing new is trusted: only the key in a
Seal can claim it. The delete token in ``seal.publish.json`` is never sent.

Which Seals: each upload is noted in ``published-seals.json`` next to the
Seal key (``~/.openadapt/seal``, or ``$OPENADAPT_SEAL_HOME``), mode 0600,
holding the id, the origin, the digest and the run folder. Only Seals
published to the Cloud you signed in to and signed by this computer's
current key are claimed. ``openadapt seal claim RUN_DIR...`` also takes run
folders (or a quickstart folder) published before that note existed.

Every network call goes through :data:`openadapt.seal_publish.Transport`.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from openadapt.seal_publish import (
    _ID,
    PUBLISH_FILE,
    STATUS_TIMEOUT_S,
    PublishError,
    Transport,
    host_of,
    read_record,
)

INDEX_FILE = "published-seals.json"
SCHEMA = "openadapt.local-seal-claim/v1"
_DOMAIN = (SCHEMA + "\0").encode("utf-8")
STATUS_PATH = "/api/local-seal-claims/status"
CHALLENGE_PATH = "/api/local-seal-claims/challenge"
CLAIM_PATH = "/api/local-seal-claims"
#: Cloud accepts at most this many ids in one claim.
MAX_PER_CLAIM = 8
_SHA256 = len(hashlib.sha256().hexdigest())


@dataclass(frozen=True)
class Claimable:
    id: str
    origin: str
    seal_sha256: str
    key_id: str
    run_dir: Optional[str] = None


def canonical(value: Any) -> bytes:
    """Sorted-key compact JSON, as Cloud's canonicalJson writes it."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def seal_sha256(seal: Mapping[str, Any]) -> str:
    """The digest Cloud stores for a Seal: SHA-256 of its canonical JSON."""
    return hashlib.sha256(canonical(dict(seal))).hexdigest()


def claim_payload(origin: str, challenge: str, seals: list[Claimable]) -> bytes:
    """The exact bytes the Seal key signs: a domain tag, then canonical JSON."""
    return _DOMAIN + canonical(
        {
            "schema": SCHEMA,
            "origin": origin,
            "challenge": challenge,
            "seals": [{"id": s.id, "seal_sha256": s.seal_sha256} for s in seals],
        }
    )


# -- the local note of published Seals -----------------------------------------


def _home(home: Optional[Path]) -> Path:
    if home is not None:
        return home
    from openadapt_flow.offline_seal import seal_home

    return seal_home()


def read_index(home: Optional[Path] = None) -> list[dict[str, Any]]:
    try:
        data = json.loads((_home(home) / INDEX_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError, ImportError):
        return []
    entries = data.get("seals") if isinstance(data, dict) else None
    return (
        [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []
    )


def remember(
    run_dir: Path,
    record: Mapping[str, Any],
    doc: Mapping[str, Any],
    origin: str,
    *,
    home: Optional[Path] = None,
) -> Optional[Path]:
    """Note an upload so ``login`` can claim it. Never raises: publishing
    already succeeded, and a missing note only means no automatic claim."""
    try:
        folder = _home(home)
        entry = {
            "id": str(record["id"]),
            "origin": origin,
            "seal_sha256": seal_sha256(doc["seal"]),
            "key_id": str(doc["seal"]["key_id"]),
            "run_dir": str(Path(run_dir).resolve()),
        }
        entries = [e for e in read_index(folder) if e.get("id") != entry["id"]]
        entries.append(entry)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / INDEX_FILE
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(
                json.dumps({"seals": entries}, indent=2, sort_keys=True) + "\n"
            )
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
        return path
    except Exception:  # noqa: BLE001 - see docstring
        return None


def _from_run_dir(run_dir: Path) -> Optional[Claimable]:
    """A published run folder's Seal, from seal.publish.json and public-seal.json."""
    record = read_record(run_dir)
    if not record or not _ID.match(str(record.get("id", ""))):
        return None
    try:
        doc = json.loads((run_dir / "public-seal.json").read_text(encoding="utf-8"))
        seal = doc["seal"]
        from urllib.parse import urlsplit

        url = urlsplit(str(record.get("url", "")))
        origin = f"{url.scheme}://{url.netloc}"
        return Claimable(
            id=str(record["id"]),
            origin=origin,
            seal_sha256=seal_sha256(seal),
            key_id=str(seal["key_id"]),
            run_dir=str(run_dir.resolve()),
        )
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _run_dirs(paths: list[Path]) -> list[Path]:
    """Each path, or the run folders inside it that were published."""
    out: list[Path] = []
    for path in paths:
        if (path / PUBLISH_FILE).is_file():
            out.append(path)
            continue
        try:
            out += sorted(p for p in path.iterdir() if (p / PUBLISH_FILE).is_file())
        except OSError:
            continue
    return out


def find_claimable(
    origin: str,
    key_id: str,
    *,
    run_dirs: Optional[list[Path]] = None,
    home: Optional[Path] = None,
) -> tuple[list[Claimable], int]:
    """Seals published to ``origin`` and signed by ``key_id``, oldest first.

    Returns the Seals and how many published to ``origin`` were skipped
    because another key signed them (a key replaced since)."""
    found: dict[str, Claimable] = {}
    for entry in read_index(home):
        try:
            item = Claimable(
                id=str(entry["id"]),
                origin=str(entry["origin"]).rstrip("/"),
                seal_sha256=str(entry["seal_sha256"]),
                key_id=str(entry["key_id"]),
                run_dir=entry.get("run_dir"),
            )
        except KeyError:
            continue
        if _ID.match(item.id) and len(item.seal_sha256) == _SHA256:
            found.setdefault(item.id, item)
    for run_dir in _run_dirs(run_dirs or []):
        item = _from_run_dir(run_dir)
        if item is not None:
            found.setdefault(item.id, item)
    here = [s for s in found.values() if s.origin == origin.rstrip("/")]
    mine = [s for s in here if s.key_id == key_id]
    return mine, len(here) - len(mine)


# -- talking to Cloud ----------------------------------------------------------


def claims_live(
    origin: str,
    *,
    transport: Transport,
    timeout_s: float = STATUS_TIMEOUT_S,
) -> bool:
    """True only when Cloud's claim status route answers 200 ``{"enabled": true}``
    within ``timeout_s``. The wait is bounded with a thread, since a socket
    timeout does not bound the DNS lookup."""
    import threading

    answer: list[Any] = []

    def ask() -> None:
        try:
            answer.append(
                transport(
                    "GET", origin + STATUS_PATH, None, {"Accept": "application/json"}
                )
            )
        except Exception:  # noqa: BLE001 - any failure means "no"
            pass

    worker = threading.Thread(target=ask, name="seal-claim-status", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if not answer or answer[0].status != 200:
        return False
    return _json(answer[0].body) == {"enabled": True}


def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def claim(
    seals: list[Claimable],
    *,
    origin: str,
    token: str,
    sign: Callable[[bytes], bytes],
    transport: Transport,
) -> int:
    """Claim ``seals`` (at most :data:`MAX_PER_CLAIM`, one key) in one request.

    Returns how many Cloud now holds for this workspace. Raises
    :class:`PublishError` with one plain line otherwise."""
    if not seals:
        return 0
    seals = seals[:MAX_PER_CLAIM]
    auth = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }
    try:
        answer = transport("POST", origin + CHALLENGE_PATH, b"{}", auth)
    except OSError as exc:
        raise PublishError(f"Not claimed: could not reach {host_of(origin)} ({exc}).")
    if answer.status == 401:
        raise PublishError("Not claimed: Cloud did not accept this computer's sign-in.")
    challenge = _json(answer.body).get("challenge")
    if answer.status != 200 or not isinstance(challenge, str):
        raise PublishError(f"Not claimed: Cloud answered HTTP {answer.status}.")
    signature = base64.b64encode(sign(claim_payload(origin, challenge, seals)))
    body = json.dumps(
        {
            "challenge": challenge,
            "seal_ids": [s.id for s in seals],
            "signature": signature.decode("ascii"),
        }
    ).encode("utf-8")
    try:
        answer = transport("POST", origin + CLAIM_PATH, body, auth)
    except OSError as exc:
        raise PublishError(f"Not claimed: could not reach {host_of(origin)} ({exc}).")
    if answer.status == 200:
        return len(seals)
    reasons = {
        404: "a Seal is no longer online",
        409: "another workspace already claimed one of these Seals",
        403: "the signature did not match the Seal key",
    }
    reason = reasons.get(answer.status, f"Cloud answered HTTP {answer.status}")
    raise PublishError(f"Not claimed: {reason}.")


def _signer() -> Any:
    from openadapt_flow.offline_seal import seal_home, signer_for

    return signer_for(seal_home())


def claim_published(
    origin: str,
    token: str,
    *,
    run_dirs: Optional[list[Path]] = None,
    transport: Transport,
    signer: Any = None,
    home: Optional[Path] = None,
    say: Callable[[str], None] = print,
    quiet_when_none: bool = False,
) -> int:
    """Find this computer's published Seals for ``origin`` and claim them.

    Returns 0 on success (including nothing to claim), 1 on a refusal."""
    signer = signer or _signer()
    seals, other_key = find_claimable(
        origin, signer.key_id, run_dirs=run_dirs, home=home
    )
    if other_key:
        say(
            f"Skipped {other_key} published Seal{'s' if other_key != 1 else ''} "
            "signed by a different Seal key than this computer's."
        )
    if not seals:
        if not quiet_when_none:
            say(f"No published Seals to claim on {host_of(origin)}.")
        return 0
    claimed = 0
    try:
        for start in range(0, len(seals), MAX_PER_CLAIM):
            claimed += claim(
                seals[start : start + MAX_PER_CLAIM],
                origin=origin,
                token=token,
                sign=signer.sign,
                transport=transport,
            )
    except PublishError as exc:
        say(str(exc))
        return 1
    what = (
        "Your published run is"
        if claimed == 1
        else f"Your {claimed} published runs are"
    )
    say(f"{what} in your workspace: {origin}/dashboard")
    return 0


def after_login(
    host: Optional[str] = None,
    *,
    transport: Optional[Transport] = None,
    say: Callable[[str], None] = print,
) -> None:
    """Run after ``openadapt login`` succeeds. Silent unless Cloud takes claims
    and there is something to claim; never raises, so login's result stands."""
    try:
        from openadapt_flow.hosted import resolve_host, resolve_token

        from openadapt.seal_publish import _module_transport

        origin = resolve_host(host)
        sender = transport or _module_transport()
        if not claims_live(origin, transport=sender):
            return
        signer = _signer()
        if not find_claimable(origin, signer.key_id)[0]:
            return
        claim_published(
            origin,
            resolve_token(None, host=origin),
            transport=sender,
            signer=signer,
            say=say,
            quiet_when_none=True,
        )
    except Exception:  # noqa: BLE001 - claiming is a convenience after login
        return


__all__ = [
    "Claimable",
    "after_login",
    "claim",
    "claim_payload",
    "claim_published",
    "claims_live",
    "find_claimable",
    "read_index",
    "remember",
    "seal_sha256",
]
