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

A shipped contract (the bundled ``verify.yaml`` or the private tier's full
one) never runs code it downloaded and never reads the user's environment
(:func:`shipped_contract_problems`, checked on the raw YAML before any
``${}`` expansion):

- it names no Python hooks and no plugin;
- every address, credential name, path and the reset command is a
  ``${VAR}`` naming one of the entry's declared variables, so the user's
  binding decides where requests go and what runs;
- ``${}`` expands only against the binding, never ``os.environ``.

Sign-in is a saved browser state (``storage_state``), and the per-case reset
is a command the user binds. ``pull`` also records the pulled program outside
the bundle (``$OPENADAPT_HOME/pulled.json``), so stripping the registry
provenance from the workflow doesn't make an unqualified pull runnable.

See docs/design/SKILLS_REGISTRY.md.
"""

from __future__ import annotations

import hashlib
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

#: Where the private tier's bearer token is read from: this one variable,
#: never one an entry names.
TOKEN_ENV = "OPENADAPT_REGISTRY_TOKEN"


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


def mark_pulled(
    bundle_dir: Path,
    entry: Any,
    *,
    index_url: str,
    attested_by: Optional[str] = None,
    index_serial: Optional[int] = None,
    index_issued_at: Optional[str] = None,
) -> None:
    """Write the lock and expire any certification the pulled bundle carries."""

    from openadapt_flow.bundle_sealing import _invalidate_prior_certification
    from openadapt_flow.ir import RegistryOrigin, Workflow
    from openadapt_flow.registry import entry_sha256

    workflow = Workflow.load(bundle_dir)
    published = workflow.manifest.content_digest if workflow.manifest else ""
    expired = _invalidate_prior_certification(workflow, reason=EXPIRED_REASON)
    raw = entry.raw
    # The provenance is part of the workflow content, so the digest covers
    # it: removing it makes a different, unverified local bundle.
    workflow.registry = RegistryOrigin(
        id=entry.id,
        version=entry.version,
        index_url=index_url,
        entry_sha256=entry_sha256(raw),
        signed_by=attested_by,
        published_digest=published,
        pulled_at=_now(),
    )
    workflow.save(bundle_dir)
    _record_pull(workflow, workflow.registry)
    _write_json(
        bundle_dir / LOCK,
        {
            "schema": LOCK_SCHEMA,
            "id": entry.id,
            "version": entry.version,
            "index": index_url,
            "signed_by": workflow.registry.signed_by,
            "index_serial": index_serial,
            "index_issued_at": index_issued_at,
            "app_license": raw.get("app_license"),
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


def program_digest(workflow: Any) -> str:
    """SHA-256 of the workflow's steps alone, whatever provenance it carries."""

    steps = [step.model_dump(mode="json") for step in workflow.steps]
    return hashlib.sha256(
        json.dumps(steps, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _ledger_path() -> Path:
    home = Path(os.environ.get("OPENADAPT_HOME", Path.home() / ".openadapt"))
    return home / "pulled.json"


def _read_ledger() -> dict[str, Any]:
    try:
        ledger = json.loads(_ledger_path().read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return ledger if isinstance(ledger, dict) else {}


def _record_pull(workflow: Any, origin: Any) -> None:
    """Remember the pulled program by its steps, outside the bundle."""

    ledger = _read_ledger()
    ledger.setdefault("programs", {})[program_digest(workflow)] = origin.model_dump(
        mode="json"
    )
    path = _ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n")
    tmp.replace(path)


def _pulled_id(workflow: Any) -> Optional[str]:
    """The registry id of a workflow whose steps were pulled here, or None."""

    record = (_read_ledger().get("programs") or {}).get(program_digest(workflow))
    if not isinstance(record, dict):
        return None
    return str(record.get("id") or "a registry skill")


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
    set by ``pull`` and covered by the content digest) runs only when:

    - it has a current certification of this exact bundle, made on an
      instance it records;
    - ``skill qualify`` recorded that qualification in the lock, for this
      digest, with the shipped (or private full) contract, or with the user's
      own contract they opted into;
    - a run aimed at ``target_url`` is on the qualified instance.

    A lock file without provenance means the bundle was edited, and is
    refused. A bundle with neither whose steps match a pull recorded under
    ``$OPENADAPT_HOME`` had its provenance stripped: it is refused until it
    carries a current certification on a named instance. Anything else is an
    ordinary local bundle and this says nothing.
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
    if not isinstance(workflow, Workflow):
        # A stand-in loader (tests, adapters) returned no workflow to check.
        return None if lock is None else f"pulled skill {lock.get('id')} failed to load"
    provenance = workflow.registry
    hint = f"openadapt-flow skill qualify {bundle_dir} --certify <policy>"
    if provenance is None:
        if lock is not None:
            return (
                f"{bundle_dir} has a skill lock but no registry provenance; pull "
                f"{lock.get('id')} again"
            )
        stripped = _pulled_id(workflow)
        if stripped is None:
            return None
        if current_certification_matches(workflow) and _qualified_origin(workflow):
            # The user's own qualification of this program on a named
            # instance, under ordinary local-bundle rules.
            return _origin_refusal(stripped, workflow, target_url, hint)
        return (
            f"{bundle_dir} is pulled skill {stripped} with its registry "
            f"provenance removed, and it is not qualified; pull it again and run "
            "openadapt-flow skill qualify"
        )
    if not current_certification_matches(workflow):
        return (
            f"pulled skill {provenance.id} has no current certification on your "
            f"instance; run {hint}"
        )
    if not _qualified_origin(workflow):
        return (
            f"pulled skill {provenance.id} was not qualified on a named instance; "
            f"run {hint}"
        )
    if lock is None:
        return f"pulled skill {provenance.id} has lost its {LOCK}; pull it again"
    qualified = lock.get("qualified")
    digest = workflow.manifest.content_digest if workflow.manifest else None
    if not isinstance(qualified, dict):
        return f"pulled skill {provenance.id} was not qualified by skill qualify; run {hint}"
    if qualified.get("bundle_digest") != digest:
        return (
            f"pulled skill {provenance.id} changed since it was qualified; run {hint}"
        )
    if qualified.get("contract") not in ("full", "minimal") and not qualified.get(
        "contract_opt_in"
    ):
        return (
            f"pulled skill {provenance.id} was qualified with a contract it doesn't "
            f"ship; run {hint}"
        )
    return _origin_refusal(provenance.id, workflow, target_url, hint)


def _qualified_origin(workflow: Any) -> Optional[str]:
    project = workflow.qualification
    if project is None:
        return None
    return project.environment.application_identity or None


def _origin_refusal(
    skill_id: str, workflow: Any, target_url: Optional[str], hint: str
) -> Optional[str]:
    qualified_origin = _qualified_origin(workflow)
    if target_url and _origin(target_url) != qualified_origin:
        return (
            f"pulled skill {skill_id} was qualified on {qualified_origin}, "
            f"not {_origin(target_url)}; run {hint} for this one"
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


def fetch_full_contract(
    bundle_dir: Path,
    lock: dict[str, Any],
    *,
    env: Mapping[str, str],
    client: Optional[Any] = None,
) -> Optional[Path]:
    """Fetch the private tier's full record check, if the entry has one.

    The signed entry pins its URL and sha256. The URL must be https on a
    private-tier host the package names (``registry.PRIVATE_TIER_HOSTS``);
    only then is the bearer token in ``OPENADAPT_REGISTRY_TOKEN`` sent. The
    body is size-capped and checked against the sha256, and stored beside
    the bundle, never inside it (``<bundle>.private/verify.full.yaml``).
    None when the entry has no full contract or no token is set: the public
    minimal contract is used then.
    """

    from openadapt_flow.registry import (
        MAX_FULL_CONTRACT_BYTES,
        private_tier_url_problem,
    )

    full = lock.get("full_contract")
    if not full:
        return None
    token = env.get(TOKEN_ENV)
    if not token:
        return None
    url = str(full.get("url") or "")
    problem = private_tier_url_problem(url)
    if problem is not None:
        raise SkillError(f"the private record check's url {problem}; nothing was sent")
    import httpx

    owned = client is None
    http = client or httpx.Client(timeout=30.0, follow_redirects=False)
    try:
        with http.stream(
            "GET", url, headers={"Authorization": f"Bearer {token}"}
        ) as resp:
            if resp.status_code != 200:
                raise SkillError(
                    f"the private record check returned HTTP {resp.status_code}"
                )
            chunks, size = [], 0
            for chunk in resp.iter_bytes():
                size += len(chunk)
                if size > MAX_FULL_CONTRACT_BYTES:
                    raise SkillError(
                        "the private record check is larger than "
                        f"{MAX_FULL_CONTRACT_BYTES} bytes"
                    )
                chunks.append(chunk)
    finally:
        if owned:
            http.close()
    body = b"".join(chunks)
    if hashlib.sha256(body).hexdigest() != full.get("sha256"):
        raise SkillError("the private record check does not match the signed entry")
    private = bundle_dir.parent / f"{bundle_dir.name}.private"
    private.mkdir(mode=0o700, exist_ok=True)
    path = private / "verify.full.yaml"
    path.write_bytes(body)
    path.chmod(0o600)
    return path


_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")
_ONE_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
#: An address: one url variable, then at most a path, query or fragment.
_ADDRESS_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}(?:[/?#].*)?$")
_HOOK_KEYS = frozenset({"session", "python"})
_SECRET_KEYS = frozenset({"access_token", "client_secret"})
#: Sections that hold data (record fields, test inputs), not addresses or
#: settings: only their ``${}`` references are checked.
_DATA_SECTIONS = frozenset({"record", "inputs", "campaign", "save_step"})
_ADDRESS_KEYS = frozenset({"url", "entry_url", "jku", "host", "dsn"})
_PATH_KEYS = frozenset({"storage_state", "sqlite_database", "root"})
#: The read-back kinds a shipped contract may use: each reads a system the
#: user's binding names, and none loads a plugin.
SHIPPED_VERIFIER_KINDS = ("rest", "fhir", "sql", "graphql")
#: DB-API drivers a shipped SQL check may name (``sql_driver`` imports it):
#: those whose session the read-back can make read-only at the driver.
SHIPPED_SQL_DRIVERS = frozenset(
    {"psycopg", "psycopg2", "pymysql", "MySQLdb", "mysql.connector"}
)


#: Words a shipped read-back query may not contain: anything that writes,
#: changes the schema or session, calls out, or reads the server's files.
_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|merge|upsert|drop|alter|create|grant|revoke|"
    r"truncate|copy|call|do|execute|exec|prepare|set|reset|lock|vacuum|analyze|"
    r"into|notify|listen|load|import|attach|pragma|replace|dblink\w*|lo_\w+|"
    r"pg_\w+|sleep|benchmark|outfile|dumpfile)\b",
    re.IGNORECASE,
)
_SQL_PLACEHOLDER = re.compile(r"%\(([A-Za-z_][A-Za-z0-9_]*)\)s")
#: One select-list item: a plain column, optionally qualified, cast to text
#: and renamed. No function call, expression, literal or placeholder.
_SQL_COLUMN = re.compile(
    r"(?i)^(?:[a-z_][a-z0-9_]*\.)?[a-z_][a-z0-9_]*"
    r"(?:::(?:text|varchar))?(?:\s+as\s+[a-z_][a-z0-9_]*)?$"
)
_SQL_SHAPE = re.compile(r"(?is)^select\s+(.*?)(?:\s+from\s+(.*))?$")
_SQL_CALL = re.compile(r"[A-Za-z0-9_\"]\s*\(")


def _sql_shape_problems(text: str) -> list[str]:
    """The query's shape: plain columns, from tables, inputs only after WHERE.

    This judges what the query reads, not its words: a select list that
    echoes the inputs, or calls a function, would make any save match.
    """

    shape = _SQL_SHAPE.match(text)
    if shape is None:
        return ["verifier.sql_query must be SELECT <columns> FROM <table>"]
    columns, rest = shape.groups()
    problems = []
    if rest is None:
        problems.append("verifier.sql_query must be SELECT <columns> FROM <table>")
        rest = ""
    for item in columns.split(","):
        if not _SQL_COLUMN.match(item.strip()):
            problems.append(
                f"verifier.sql_query selects {item.strip()!r}: select only plain "
                "column names (optionally ::text and AS)"
            )
    where = re.search(r"(?i)\bwhere\b", rest)
    source = rest[: where.start()] if where else rest
    if _SQL_PLACEHOLDER.search(source):
        problems.append("verifier.sql_query binds inputs only after WHERE")
    if _SQL_CALL.search(source) or "(" in source:
        problems.append("verifier.sql_query reads tables, not functions or subqueries")
    return problems


def shipped_sql_problems(query: Any, params: Any) -> list[str]:
    """Why a shipped SQL read-back isn't one parameterized, read-only SELECT."""

    if not isinstance(query, str) or not query.strip():
        return ["verifier.sql_query must be a SELECT"]
    text = query.strip()
    if text.endswith(";"):
        text = text[:-1].rstrip()
    problems = []
    if ";" in text:
        problems.append("verifier.sql_query must be a single statement")
    if not re.match(r"(?is)^select\s", text):
        problems.append("verifier.sql_query must start with SELECT")
    if "--" in text or "/*" in text or "#" in text:
        problems.append("verifier.sql_query may not carry comments")
    if "'" in text or "$$" in text or "${" in text:
        problems.append(
            "verifier.sql_query may not carry literals or ${} text: bind every "
            "value with %(name)s"
        )
    forbidden = sorted({m.group(1).lower() for m in _SQL_FORBIDDEN.finditer(text)})
    if forbidden:
        problems.append(
            f"verifier.sql_query is read-only, and may not use {', '.join(forbidden)}"
        )
    problems.extend(_sql_shape_problems(text))
    bound = params if isinstance(params, dict) else {}
    for name in _SQL_PLACEHOLDER.findall(text):
        if name not in bound:
            problems.append(f"verifier.sql_query binds %({name})s, which has no value")
    if "%" in _SQL_PLACEHOLDER.sub("", text):
        problems.append("verifier.sql_query binds values only as %(name)s")
    for name, value in bound.items():
        if not (isinstance(value, dict) and set(value) == {"param"}):
            problems.append(
                f"verifier.sql_query_params.{name} must be {{param: ...}}, a typed input"
            )
    return problems


def shipped_contract_problems(data: Any, kinds: Mapping[str, str]) -> list[str]:
    """Why a contract that came with a pulled skill may not run, if it may not.

    Checked on the raw YAML, before any ``${}`` expansion. ``kinds`` maps each
    declared contract variable to its kind (``url``, ``env_var_name``,
    ``path``, ``command``).
    """

    problems: list[str] = []

    def undeclared(path: str, value: str) -> None:
        for match in _REF.finditer(value):
            if match.group(1) not in kinds:
                problems.append(
                    f"{path}: ${{{match.group(1)}}} is not a declared contract variable"
                )

    def need(path: str, value: Any, kind: str, *, address: bool = False) -> None:
        if value is None:
            return
        pattern = _ADDRESS_REF if address else _ONE_REF
        match = pattern.match(value) if isinstance(value, str) else None
        if match is None or kinds.get(match.group(1)) != kind:
            problems.append(
                f"{path} must be ${{VAR}} naming a declared {kind} variable"
                + (", then at most a path" if address else "")
            )
        if isinstance(value, str):
            undeclared(path, value)

    def walk(node: Any, path: str, keyed: bool = True) -> None:
        if isinstance(node, dict):
            for raw_key, value in node.items():
                key = str(raw_key)
                where = f"{path}.{key}" if path else key
                if not keyed or (not path and key in _DATA_SECTIONS):
                    walk(value, where, keyed=False)
                elif key in _HOOK_KEYS:
                    problems.append(
                        f"{where}: a shipped contract names no Python hooks"
                    )
                elif key in _SECRET_KEYS and isinstance(value, str):
                    problems.append(
                        f"{where}: a shipped contract carries no credential"
                    )
                elif key == "verify_tls" and value is False:
                    problems.append(
                        f"{where}: a shipped contract can't turn off TLS checks"
                    )
                elif key.endswith("_env"):
                    if isinstance(value, dict):
                        for name, item in value.items():
                            need(f"{where}.{name}", item, "env_var_name")
                    else:
                        need(where, value, "env_var_name")
                elif key in _ADDRESS_KEYS or key.endswith("_url"):
                    need(where, value, "url", address=True)
                elif key in _PATH_KEYS or key.endswith("_file"):
                    need(where, value, "path")
                elif key == "sql_connect_args" and isinstance(value, dict):
                    for name, item in value.items():
                        if isinstance(item, str) and (
                            not _ONE_REF.match(item) or item[2:-1] not in kinds
                        ):
                            problems.append(
                                f"{where}.{name} must be ${{VAR}} naming a declared variable"
                            )
                elif key == "sql_driver" and value not in SHIPPED_SQL_DRIVERS:
                    problems.append(
                        f"{where}: {value!r} is not a known SQL driver "
                        f"({', '.join(sorted(SHIPPED_SQL_DRIVERS))})"
                    )
                else:
                    walk(value, where)
        elif isinstance(node, list):
            for i, item in enumerate(node):
                walk(item, f"{path}[{i}]", keyed)
        elif isinstance(node, str):
            undeclared(path, node)

    if not isinstance(data, dict):
        return ["the contract is not a YAML mapping"]
    walk(data, "")
    app = data.get("app")
    reset: Any = app.get("reset") if isinstance(app, dict) else None
    if not isinstance(reset, dict):
        reset = {}
    if "command" in reset:
        need("app.reset.command", reset["command"], "command")
    http = reset.get("http")
    if isinstance(http, dict):
        # The whole reset address is the user's: no path the contract adds.
        need("app.reset.http.url", http.get("url"), "url")
    for section in ("verifier", "system_of_record"):
        node = data.get(section)
        while isinstance(node, dict):
            if "sql_query" in node or node.get("kind") == "sql":
                problems.extend(
                    shipped_sql_problems(
                        node.get("sql_query"), node.get("sql_query_params")
                    )
                )
            kind = node.get("kind")
            if kind is not None and kind not in SHIPPED_VERIFIER_KINDS:
                problems.append(
                    f"{section}.kind {kind!r}: a shipped contract reads with "
                    f"{', '.join(SHIPPED_VERIFIER_KINDS)}"
                )
            node = node.get("verifier")
    return sorted(set(problems))


def qualify_skill(
    bundle_dir: Path | str,
    *,
    certify: str,
    out: Optional[Path | str] = None,
    trials: Optional[int] = None,
    contract: Optional[Path | str] = None,
    replace_contract: bool = False,
    log: Callable[[str], None] = print,
    run: Optional[Callable[..., Any]] = None,
    http_client: Optional[Any] = None,
) -> Any:
    """Qualify a pulled skill in place against the bound instance.

    The skill's own contract is used: the private tier's full one when the
    registry token is set, else the bundled minimal one. ``contract`` is the
    user's own verify.yaml, for a skill that ships none; replacing a shipped
    contract with it takes ``replace_contract``, and the lock records that.
    The user's file may name Python hooks and read their environment; a
    shipped contract may not (:func:`shipped_contract_problems`).
    """

    import yaml

    from openadapt_flow.qualify.campaign import _default_hooks, qualify
    from openadapt_flow.qualify.config import (
        QualifyConfigError,
        load_verify_config,
        parse_verify_config,
    )

    bundle_dir = Path(bundle_dir)
    lock = read_lock(bundle_dir)
    if lock is None:
        raise SkillError(f"{bundle_dir} is not a pulled skill (no {LOCK})")
    signer = str(lock.get("signed_by") or "")
    if not signer or signer.startswith("test:"):
        log(
            f"{lock.get('id')} is not attested by the registry release workflow: "
            "it came from a local index, and nothing vouches for it but that file."
        )
    ships = bool(lock.get("contract"))
    if contract is None and not ships:
        raise SkillError(
            f"{lock.get('id')} ships no record check; pass --verify with your own"
        )
    if contract is not None and ships and not replace_contract:
        raise SkillError(
            f"{lock.get('id')} ships its own record check; pass --replace-contract "
            "to qualify it with yours instead"
        )
    shipped = contract is None
    missing = [] if not shipped else missing_bindings(bundle_dir)
    if missing:
        raise SkillError(
            "bind these first: "
            + ", ".join(missing)
            + f" (openadapt-flow skill bind {bundle_dir} --set NAME=VALUE)"
        )
    binding = read_binding(bundle_dir)
    full_path = (
        fetch_full_contract(bundle_dir, lock, env=os.environ, client=http_client)
        if shipped
        else None
    )
    if shipped and full_path is None and lock.get("full_contract"):
        log(
            f"The full record check is in the private tier (set {TOKEN_ENV}); "
            "qualifying with the public minimal check."
        )
    if not shipped:
        # The user's own file: their environment, their hooks.
        config = load_verify_config(Path(str(contract)), env={**os.environ, **binding})
    else:
        contract_path = full_path or bundle_dir / lock["contract"]
        try:
            data = yaml.safe_load(contract_path.read_text())
        except yaml.YAMLError as exc:
            raise SkillError(f"the skill's record check is not YAML: {exc}") from exc
        kinds = {
            str(v["name"]): str(v["kind"])
            for v in lock.get("contract_vars") or []
            if isinstance(v, dict) and v.get("name")
        }
        problems = shipped_contract_problems(data, kinds)
        if problems:
            raise SkillError(
                "the skill's record check may not run here (a pulled skill never "
                "runs code it downloaded or reads your environment):\n  "
                + "\n  ".join(problems)
            )
        try:
            # Only the binding: never os.environ.
            config = parse_verify_config(
                data, base_dir=contract_path.resolve().parent, env=binding
            )
        except QualifyConfigError as exc:
            raise SkillError(f"the skill's record check is invalid: {exc}") from exc
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
            else ("minimal" if shipped else "yours"),
            # The user chose their own contract: for a skill that ships none,
            # or by --replace-contract.
            "contract_opt_in": not shipped,
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
                replace_contract=args.replace_contract,
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
            signer = lock.get("signed_by")
            if not signer:
                print("UNSIGNED: pulled from a local index with --allow-unsigned")
            elif signer.startswith("test:"):
                print("TEST SIGNER: pulled from a local index with --allow-test-key")
            else:
                print(f"Attested by {signer}")
            if lock.get("index_issued_at"):
                print(
                    f"Index serial {lock.get('index_serial')}, issued "
                    f"{lock['index_issued_at']}"
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
    q.add_argument(
        "--replace-contract",
        action="store_true",
        help="Qualify with --verify even though the skill ships its own record check",
    )
    s = ssub.add_parser("status", help="Whether the pulled skill may run")
    s.add_argument("bundle")
    for parser in (b, q, s):
        parser.set_defaults(func=cmd_skill)
