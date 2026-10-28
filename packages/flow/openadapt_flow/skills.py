"""Skills pulled from the registry: bind them locally, qualify, then run.

``openadapt-flow pull`` puts a skill's bundle on disk with a
``skill.lock.json`` beside its files (:func:`mark_pulled`), and expires any
certification it carries. A registry certification is evidence about the
registry's test copy, never about the user's instance. From there:

1. ``openadapt-flow skill bind DIR --set NAME=VALUE ...`` records the user's
   values for the contract's variables in ``binding.json``. The values are
   addresses, environment variable names, local paths and local commands,
   each checked against the kind the entry declares. A credential is only
   ever the name of the variable that holds it, never its value.
2. ``openadapt-flow skill qualify DIR --certify POLICY`` runs the
   qualification cases with the skill's bundled ``verify.yaml``, resolved
   through the binding, against the user's instance. A passing campaign
   certifies the bundle in place and records the qualification in the lock.
3. ``run``, ``replay``, ``emit-skill`` and ``emit-mcp`` call
   :func:`pulled_skill_refusal` and refuse a pulled skill until then.

The bundled contract names no Python hooks: a pulled skill never runs code it
downloaded. Sign-in is a saved browser state (``storage_state``), and the
per-case reset is a command the user binds.

See docs/design/SKILLS_REGISTRY.md.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlsplit

LOCK = "skill.lock.json"
BINDING = "binding.json"
LOCK_SCHEMA = "openadapt.skill-lock/v1"
BINDING_SCHEMA = "openadapt.skill-binding/v1"
EXPIRED_REASON = "pulled from the registry; qualify it on your instance"

_ENV_NAME = re.compile(r"^[A-Z_][A-Z0-9_]{0,127}$")


class SkillError(RuntimeError):
    """A pulled skill's lock, binding or qualification is not usable."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _write_json(path: Path, value: Any, *, private: bool = False) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    if private:
        path.chmod(0o600)


def read_lock(bundle_dir: Path | str) -> Optional[dict[str, Any]]:
    path = Path(bundle_dir) / LOCK
    if not path.is_file():
        return None
    try:
        lock = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise SkillError(f"{path} is not JSON: {exc}") from exc
    if not isinstance(lock, dict) or lock.get("schema") != LOCK_SCHEMA:
        raise SkillError(f"{path} is not a skill lock ({LOCK_SCHEMA})")
    return lock


def read_binding(bundle_dir: Path | str) -> dict[str, str]:
    path = Path(bundle_dir) / BINDING
    if not path.is_file():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or data.get("schema") != BINDING_SCHEMA:
        raise SkillError(f"{path} is not a skill binding ({BINDING_SCHEMA})")
    return {str(k): str(v) for k, v in (data.get("values") or {}).items()}


def mark_pulled(bundle_dir: Path, entry: Any, *, index_url: str) -> None:
    """Write the lock and expire any certification the pulled bundle carries."""

    from openadapt_flow.bundle_sealing import _invalidate_prior_certification
    from openadapt_flow.ir import RegistryOrigin, Workflow
    from openadapt_flow.registry import entry_sha256

    workflow = Workflow.load(bundle_dir)
    published = workflow.manifest.content_digest if workflow.manifest else ""
    expired = _invalidate_prior_certification(workflow, reason=EXPIRED_REASON)
    raw = entry.raw
    signature = raw.get("signature")
    # The provenance is part of the workflow content, so the digest covers
    # it: removing it makes a different, unverified local bundle.
    workflow.registry = RegistryOrigin(
        id=entry.id,
        version=entry.version,
        index_url=index_url,
        entry_sha256=entry_sha256(raw),
        signed_by=signature.get("key_id") if isinstance(signature, dict) else None,
        published_digest=published,
        pulled_at=_now(),
    )
    workflow.save(bundle_dir)
    _write_json(
        bundle_dir / LOCK,
        {
            "schema": LOCK_SCHEMA,
            "id": entry.id,
            "version": entry.version,
            "index": index_url,
            "pulled_at": _now(),
            "files": entry.files,
            "certification_expired": expired,
            "contract": raw.get("contract"),
            "full_contract": raw.get("full_contract"),
            "contract_vars": raw.get("contract_vars") or [],
            "credentials": raw.get("credentials") or [],
            "qualified": None,
        },
    )


def _check_value(name: str, kind: str, value: str) -> str:
    if kind == "url":
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise SkillError(f"{name} must be an http(s) URL")
        if parts.username or parts.password:
            raise SkillError(f"{name} must not carry credentials in the URL")
        return value
    if kind == "env_var_name":
        if not _ENV_NAME.match(value):
            raise SkillError(
                f"{name} takes the NAME of an environment variable (like "
                "MY_EMR_TOKEN), never the secret itself"
            )
        return value
    if kind == "path":
        path = Path(value).expanduser().resolve()
        if not path.exists():
            raise SkillError(f"{name}: {path} does not exist")
        return str(path)
    if kind == "command":
        if not value.strip():
            raise SkillError(f"{name} needs a command")
        return value
    raise SkillError(f"{name}: unknown kind {kind!r}")


def bind(bundle_dir: Path | str, values: Mapping[str, str]) -> Path:
    """Record the user's values for the skill's contract variables."""

    bundle_dir = Path(bundle_dir)
    lock = read_lock(bundle_dir)
    if lock is None:
        raise SkillError(f"{bundle_dir} is not a pulled skill (no {LOCK})")
    declared = {v["name"]: v for v in lock.get("contract_vars") or []}
    merged = {**read_binding(bundle_dir)}
    for name, value in values.items():
        var = declared.get(name)
        if var is None:
            known = ", ".join(sorted(declared)) or "none"
            raise SkillError(f"{name} is not a variable of this skill (known: {known})")
        merged[name] = _check_value(name, var["kind"], value)
    path = bundle_dir / BINDING
    _write_json(
        path,
        {"schema": BINDING_SCHEMA, "bound_at": _now(), "values": merged},
        private=True,
    )
    return path


def missing_bindings(bundle_dir: Path | str) -> list[str]:
    lock = read_lock(bundle_dir) or {}
    bound = read_binding(bundle_dir)
    return [
        v["name"]
        for v in lock.get("contract_vars") or []
        if v.get("required", True) and v["name"] not in bound
    ]


def _origin(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc}"


def pulled_skill_refusal(
    bundle_dir: Path | str, *, target_url: Optional[str] = None
) -> Optional[str]:
    """Why a pulled skill may not run yet, or None (not pulled, or qualified).

    A bundle whose workflow carries registry provenance (``Workflow.registry``,
    set by ``pull`` and covered by the content digest) runs only with a
    current certification of this exact bundle, made on an instance it
    records. A run aimed at ``target_url`` must be on that instance. A lock
    file without provenance means the bundle was edited, and is refused. A
    bundle with neither is an ordinary local bundle and this says nothing.
    """

    bundle_dir = Path(bundle_dir)
    try:
        lock = read_lock(bundle_dir)
    except SkillError as exc:
        return f"pulled skill: {exc}"
    from openadapt_flow.ir import Workflow
    from openadapt_flow.qualification import current_certification_matches

    try:
        workflow = Workflow.load(bundle_dir)
    except Exception as exc:  # noqa: BLE001 - an unloadable bundle can't run
        if lock is None:
            return None
        return f"pulled skill {lock.get('id')} failed to load: {exc}"
    provenance = workflow.registry
    if provenance is None:
        if lock is not None:
            return (
                f"{bundle_dir} has a skill lock but no registry provenance; pull "
                f"{lock.get('id')} again"
            )
        return None
    hint = f"openadapt-flow skill qualify {bundle_dir} --certify <policy>"
    if not current_certification_matches(workflow):
        return (
            f"pulled skill {provenance.id} has no current certification on your "
            f"instance; run {hint}"
        )
    project = workflow.qualification
    qualified_origin = (
        project.environment.application_identity if project is not None else None
    )
    if not qualified_origin:
        return (
            f"pulled skill {provenance.id} was not qualified on a named instance; "
            f"run {hint}"
        )
    if target_url and _origin(target_url) != qualified_origin:
        return (
            f"pulled skill {provenance.id} was qualified on {qualified_origin}, "
            f"not {_origin(target_url)}; run {hint} for this one"
        )
    qualified = (lock or {}).get("qualified")
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if qualified and qualified.get("bundle_digest") != digest:
        return (
            f"pulled skill {provenance.id} changed since it was qualified; run {hint}"
        )
    return None


def refuse_unqualified_skill(
    bundle_dir: Path | str, *, target_url: Optional[str] = None
) -> None:
    """Raise ``SystemExit`` for a pulled skill not yet qualified here.

    For ``run``, ``replay``, ``emit-skill`` and ``emit-mcp``.
    """

    refusal = pulled_skill_refusal(bundle_dir, target_url=target_url)
    if refusal is not None:
        raise SystemExit(f"REFUSED: {refusal}")


#: Where the private tier's bearer token is read from by default.
DEFAULT_TOKEN_ENV = "OPENADAPT_REGISTRY_TOKEN"


def fetch_full_contract(
    bundle_dir: Path, lock: dict[str, Any], *, env: Mapping[str, str]
) -> Optional[Path]:
    """Fetch the private tier's full record check, if the entry has one.

    The signed entry pins its URL and sha256. It is fetched with a bearer
    token from the environment and stored beside the bundle, never inside it
    (``<bundle>.private/verify.full.yaml``). None when the entry has no full
    contract or no token is set: the public minimal contract is used then.
    """

    import hashlib

    full = lock.get("full_contract")
    if not full:
        return None
    token = env.get(str(full.get("token_env") or DEFAULT_TOKEN_ENV))
    if not token:
        return None
    import httpx

    resp = httpx.get(
        full["url"],
        headers={"Authorization": f"Bearer {token}"},
        timeout=30.0,
        follow_redirects=False,
    )
    if resp.status_code != 200:
        raise SkillError(f"the private record check returned HTTP {resp.status_code}")
    digest = hashlib.sha256(resp.content).hexdigest()
    if digest != full["sha256"]:
        raise SkillError("the private record check does not match the signed entry")
    private = bundle_dir.parent / f"{bundle_dir.name}.private"
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / "verify.full.yaml"
    path.write_bytes(resp.content)
    path.chmod(0o600)
    return path


def qualify_skill(
    bundle_dir: Path | str,
    *,
    certify: str,
    out: Optional[Path | str] = None,
    trials: Optional[int] = None,
    contract: Optional[Path | str] = None,
    log: Callable[[str], None] = print,
    run: Optional[Callable[..., Any]] = None,
) -> Any:
    """Qualify a pulled skill in place against the bound instance.

    ``contract`` is the user's own verify.yaml, for a skill that ships none
    (or to replace the shipped one). It is the user's file, so it may name
    Python hooks; the shipped contract may not.
    """

    from openadapt_flow.qualify.campaign import _default_hooks, qualify
    from openadapt_flow.qualify.config import load_verify_config

    bundle_dir = Path(bundle_dir)
    lock = read_lock(bundle_dir)
    if lock is None:
        raise SkillError(f"{bundle_dir} is not a pulled skill (no {LOCK})")
    if contract is None and not lock.get("contract"):
        raise SkillError(
            f"{lock.get('id')} ships no record check; pass --verify with your own"
        )
    missing = [] if contract is not None else missing_bindings(bundle_dir)
    if missing:
        raise SkillError(
            "bind these first: "
            + ", ".join(missing)
            + f" (openadapt-flow skill bind {bundle_dir} --set NAME=VALUE)"
        )
    env = {**os.environ, **read_binding(bundle_dir)}
    shipped = contract is None
    full_path = fetch_full_contract(bundle_dir, lock, env=env) if shipped else None
    if shipped and full_path is None and lock.get("full_contract"):
        token_env = lock["full_contract"].get("token_env") or DEFAULT_TOKEN_ENV
        log(
            f"The full record check is in the private tier (set {token_env}); "
            "qualifying with the public minimal check."
        )
    if contract is not None:
        contract_path = Path(contract)
    elif full_path is not None:
        contract_path = full_path
    else:
        contract_path = bundle_dir / lock["contract"]
    config = load_verify_config(contract_path, env=env)
    if shipped and (
        config.app.session is not None
        or getattr(config.app.reset, "python", None)
        or getattr(config.verifier, "session", None)
    ):
        raise SkillError(
            "a pulled skill's contract may not name Python hooks; it never runs "
            "code it downloaded"
        )
    out = Path(out) if out else bundle_dir.parent / f"{bundle_dir.name}-qualification"
    result = (run or qualify)(
        bundle_dir,
        config,
        out=out,
        trials=trials,
        certify=certify,
        hooks=_default_hooks(config, None),
        in_place=True,
        log=log,
    )
    if getattr(result, "certified", False):
        from openadapt_flow.ir import Workflow

        workflow = Workflow.load(bundle_dir)
        lock["qualified"] = {
            "at": _now(),
            "policy": certify,
            "bundle_digest": workflow.manifest.content_digest
            if workflow.manifest
            else None,
            "app_origin": config.app.origin,
            "contract": "full"
            if full_path is not None
            else ("yours" if contract is not None else "minimal"),
            "evidence": str(out),
        }
        _write_json(bundle_dir / LOCK, lock)
    return result


def cmd_skill(args: Any) -> int:
    import sys

    try:
        if args.skill_cmd == "bind":
            values = {}
            for item in args.set or []:
                name, eq, value = item.partition("=")
                if not eq:
                    raise SkillError(f"--set takes NAME=VALUE, got {item!r}")
                values[name] = value
            path = bind(args.bundle, values)
            missing = missing_bindings(args.bundle)
            print(f"Bound {len(values)} value(s) in {path}")
            if missing:
                print("Still to bind: " + ", ".join(missing))
            return 0
        if args.skill_cmd == "qualify":
            result = qualify_skill(
                args.bundle,
                certify=args.certify,
                out=args.out,
                trials=args.trials,
                contract=args.verify,
            )
            if getattr(result, "certified", False):
                print(f"Qualified on your instance and certified under {args.certify}.")
                return 0
            print("Qualification did not certify the skill; it stays refused.")
            return 1
        if args.skill_cmd == "status":
            lock = read_lock(args.bundle)
            if lock is None:
                print(f"{args.bundle} is not a pulled skill")
                return 1
            refusal = pulled_skill_refusal(args.bundle)
            print(
                f"{lock['id']}@{lock.get('version')}: " + (refusal or "qualified; runs")
            )
            missing = missing_bindings(args.bundle)
            if missing:
                print("Unbound: " + ", ".join(missing))
            return 0 if refusal is None else 1
    except SkillError as exc:
        print(f"skill: {exc}", file=sys.stderr)
        return 1
    return 2


def add_parser(sub: Any) -> None:
    p = sub.add_parser(
        "skill", help="Bind and qualify a skill pulled from the registry"
    )
    ssub = p.add_subparsers(dest="skill_cmd", required=True)
    b = ssub.add_parser("bind", help="Record your values for the skill's contract")
    b.add_argument("bundle")
    b.add_argument(
        "--set",
        action="append",
        metavar="NAME=VALUE",
        help="A contract variable: a URL, an environment variable NAME, a path or a command",
    )
    q = ssub.add_parser("qualify", help="Qualify the skill on your instance")
    q.add_argument("bundle")
    q.add_argument("--certify", required=True, help="Policy to certify under")
    q.add_argument("--out", default=None)
    q.add_argument("--trials", type=int, default=None)
    q.add_argument(
        "--verify",
        default=None,
        help="Your own verify.yaml, for a skill that ships no record check",
    )
    s = ssub.add_parser("status", help="Whether the pulled skill may run")
    s.add_argument("bundle")
    for parser in (b, q, s):
        parser.set_defaults(func=cmd_skill)
