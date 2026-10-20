"""``openadapt-flow local-seal``: offline Seals and optional batched timestamps."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Optional


def _home(args: argparse.Namespace) -> Path:
    from openadapt_flow.offline_seal import seal_home

    return Path(args.home).expanduser() if args.home else seal_home()


def _signer(args: argparse.Namespace) -> Any:
    """The configured Seal key. Raises LocalSealError when it can't be opened."""
    from openadapt_flow.offline_seal import LocalSealError, signer_for
    from openadapt_flow.seal_keys import KeyUnavailable

    signer = signer_for(_home(args), spec=args.key)
    try:
        signer.public_raw()  # a token or keychain that can't answer stops here
    except KeyUnavailable as exc:
        raise LocalSealError(exc.code, str(exc)) from exc
    return signer


class InputError(Exception):
    """A file the user named can't be used. Reported in one line, exit code 2."""


def _guarded(func: Any) -> Any:
    """Turn any input problem into one clear line and exit code 2, never a traceback."""
    import functools

    @functools.wraps(func)
    def run(args: argparse.Namespace) -> int:
        try:
            return int(func(args))
        except InputError as exc:
            print(str(exc), file=sys.stderr)
            return 2

    return run


def _starts(raw: bytes) -> str:
    head = (
        raw.lstrip()[:40].decode("utf-8", "replace").splitlines()[0]
        if raw.strip()
        else ""
    )
    return head[:40]


def _input_bytes(path: Path) -> bytes:
    """The bytes of an input file; refuses a missing, unreadable or empty one."""
    path = Path(path)
    if not path.exists():
        raise InputError(f"{path}: no such file")
    if path.is_dir():
        raise InputError(f"{path} is a folder, not a file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"{path} can't be read ({exc.strerror or exc})") from exc
    if not raw.strip():
        raise InputError(f"{path} is empty")
    return raw


def _looks_like_web_page(raw: bytes) -> bool:
    head = raw.lstrip()[:200].lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<?xml")) or (
        head.startswith(b"<") and b"<html" in head
    )


def _input_json(path: Path) -> Any:
    """Parse an input file as JSON (repeated keys refused), or explain in one line."""
    from openadapt_flow.offline_seal import loads_strict

    raw = _input_bytes(path)
    try:
        return loads_strict(raw)
    except UnicodeDecodeError as exc:
        raise InputError(f"{path} is not JSON (it isn't UTF-8 text)") from exc
    except ValueError as exc:
        start = _starts(raw)
        if _looks_like_web_page(raw):
            raise InputError(
                f"{path} is not JSON (it starts with {start!r}: it looks like a web "
                "page, check the URL)"
            ) from exc
        if "duplicate JSON key" in str(exc):
            raise InputError(f"{path} is not valid: {exc}") from exc
        raise InputError(f"{path} is not JSON (it starts with {start!r})") from exc


def _input_pem_certs(path: Path, what: str) -> list[Any]:
    raw = _input_bytes(path)
    if _looks_like_web_page(raw):
        raise InputError(
            f"{path} is not a {what} (it looks like a web page, check the URL)"
        )
    return [raw]


def _registry(args: argparse.Namespace) -> Any:
    from openadapt_types.seal import parse_key_registry

    from openadapt_flow.offline_seal import load_registry

    if args.keys:
        _input_json(Path(args.keys))
        try:
            return load_registry(Path(args.keys))
        except ValueError as exc:
            raise InputError(f"{args.keys} is not a key registry ({exc})") from exc
    # No pinned file: trust only the configured Seal key, and say so.
    signer = _signer(args)
    print(
        f"keys: this machine's Seal key ({signer.describe()}); "
        "pass --keys to pin others",
        file=sys.stderr,
    )
    return parse_key_registry([signer.registry_entry()])


def _refused(exc: Any) -> int:
    print(f"not sealed: {exc}", file=sys.stderr)
    return 1


def _cmd_run(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError, seal_run
    from openadapt_flow.run_authorization import seal_authorized

    if bool(args.authorization) != bool(args.item):
        print("--authorization and --item go together", file=sys.stderr)
        return 2
    try:
        if args.authorization:
            seal = seal_authorized(
                Path(args.run_dir),
                Path(args.authorization),
                args.item,
                _signer(args),
                home=_home(args),
                replace_unanchored=args.replace_unanchored,
            )
        else:
            seal = seal_run(Path(args.run_dir), home=_home(args), signer=_signer(args))
    except LocalSealError as exc:
        return _refused(exc)
    print(json.dumps(seal.wire(), indent=2, sort_keys=True))
    return 0


def _cmd_file(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError, enqueue_for_batch, sign_file

    try:
        seal = sign_file(
            Path(args.path),
            subject_kind=args.kind,
            run_id=args.run_id,
            home=_home(args),
            signer=_signer(args),
        )
    except LocalSealError as exc:
        return _refused(exc)
    out = Path(args.out) if args.out else Path(str(args.path) + ".seal.json")
    out.write_text(
        json.dumps(seal.wire(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    enqueue_for_batch(seal, _home(args))
    print(str(out))
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    from openadapt_flow import no_outbound
    from openadapt_flow.offline_seal import (
        LocalSealError,
    )
    from openadapt_flow.seal_anchor import fetch_entry

    try:
        registry = _registry(args)
    except LocalSealError as exc:
        print(f"not valid: {exc}", file=sys.stderr)
        return 1
    log_keys = _log_keys(args)
    _tsa_trusted(args)
    fetch = None
    if args.live:
        if not (args.require_anchor or args.authorization):
            print("--live needs --require-anchor or --authorization", file=sys.stderr)
            return 2
        if no_outbound.enabled():
            print("not checked live: strict no-outbound mode is on", file=sys.stderr)
            return 1
        if args.require_anchor:
            fetch = fetch_entry
    targets = [Path(t) for t in args.target]
    if len(targets) > 1 and (args.seal or not all(t.is_dir() for t in targets)):
        print(
            "several targets must all be run directories (no --seal)", file=sys.stderr
        )
        return 2
    for target in targets:
        _check_verify_inputs(args, target)
    auth = None
    if args.authorization:
        if not all(t.is_dir() for t in targets):
            print("--authorization applies to a run directory", file=sys.stderr)
            return 2
        auth = _verify_authorization(args, registry, log_keys)
        if not auth.ok:
            print(f"not valid: {auth.reason} {auth.detail}".rstrip(), file=sys.stderr)
            return 1
    seals: dict[Path, Any] = {}
    for target in targets:
        code = _verify_one(args, target, registry, log_keys, fetch, auth)
        if code:
            return code
        if target.is_dir():
            seals[target] = _input_json(target / "seal.json")
    if auth is not None and len(seals) > 1:
        from openadapt_flow.offline_seal import seal_sha256
        from openadapt_flow.run_authorization import reused_items

        reused = reused_items(list(seals.values()), auth)
        for target, seal in seals.items():
            if seal_sha256(seal) in reused:
                print(
                    f"not valid: auth_item_reused ({target}: item "
                    f"{seal.get('scope_item')} was already sealed under this "
                    "single-use authorization)",
                    file=sys.stderr,
                )
                return 1
    return 0


def _verify_one(
    args: argparse.Namespace,
    target: Path,
    registry: Any,
    log_keys: Any,
    fetch: Any,
    auth: Any,
) -> int:
    from openadapt_flow.offline_seal import check_anchor, verify_local_seal, verify_run

    if target.is_dir():
        result = verify_run(
            target,
            registry,
            strict=args.strict,
            require_anchor=args.require_anchor,
            log_keys=log_keys,
            log_url=args.log_url,
            fetch=fetch,
            authorization=auth,
        )
    else:
        if not args.seal:
            print("verify FILE needs --seal FILE.seal.json", file=sys.stderr)
            return 2
        seal = _input_json(Path(args.seal))
        digest = hashlib.sha256(_input_bytes(target)).hexdigest()
        result = verify_local_seal(seal, digest, registry)
        if args.require_anchor:
            result = check_anchor(
                seal,
                Path(args.seal),
                result,
                registry,
                log_keys=log_keys,
                log_url=args.log_url,
                fetch=fetch,
            )
    stamp_lines: list[str] = []
    if result.ok and args.require_timestamp:
        seal_path = target / "seal.json" if target.is_dir() else Path(args.seal)
        problem, stamp_lines = _check_timestamps(args, seal_path, auth)
        if problem:
            print(f"not valid: {problem}", file=sys.stderr)
            return 1
    if result.ok:
        where = target / result.subject_file if result.subject_file else target
        print(f"valid offline Seal over {where}, key {result.key_id}")
        if result.subject_file == "report.pre-seal.json":
            print(
                "note: report.json differs from what was sealed; the Seal covers "
                "report.pre-seal.json, the copy attach-seal kept (--strict refuses this)"
            )
        if result.anchor is not None:
            when = _utc(result.anchor.integrated_time)
            how = "checked live and offline" if fetch else "checked offline"
            print(
                f"anchored in the transparency log at {when}: entry "
                f"{result.anchor.uuid}, log index {result.anchor.log_index} ({how})"
            )
        if auth is not None and auth.statement is not None:
            print(_authorized_line(auth))
            print(_timing_line(result, auth))
        for line in stamp_lines:
            print(line)
        return 0
    print(f"not valid: {result.reason}", file=sys.stderr)
    return 1


def _check_verify_inputs(args: argparse.Namespace, target: Path) -> None:
    """Every file verify will read: present, non-empty, and JSON where JSON is due."""
    from openadapt_flow.run_authorization import _paths
    from openadapt_flow.seal_anchor import anchor_path
    from openadapt_flow.seal_timestamp import timestamp_path

    seal: Optional[Path]
    if target.is_dir():
        _input_json(target / "report.json")
        seal = target / "seal.json"
    else:
        _input_bytes(target)
        if target.suffix == ".json":
            _input_json(target)
        seal = Path(args.seal) if args.seal else None
    if seal is not None:
        _input_json(seal)
        # Optional companions: a missing one is a verdict (exit 1), a broken one an input error.
        if args.require_anchor and anchor_path(seal).exists():
            _input_json(anchor_path(seal))
        if getattr(args, "require_timestamp", False) and timestamp_path(seal).exists():
            _input_json(timestamp_path(seal))
    if args.authorization:
        statement, sig, _, anchor_file = _paths(Path(args.authorization))
        _input_json(statement)
        if sig.exists():
            _input_bytes(sig)
        if args.allowed_signers:
            _input_bytes(Path(args.allowed_signers))
        if args.require_anchor and anchor_file.exists():
            _input_json(anchor_file)


def _tsa_trusted(args: argparse.Namespace) -> Any:
    if not getattr(args, "tsa_cert", None):
        return None  # the bundled Sigstore TSA root
    from openadapt_flow.countersign.tsa import load_pem_certificates

    certs: list[Any] = []
    for path in args.tsa_cert:
        raw = _input_bytes(Path(path))
        try:
            certs.extend(load_pem_certificates(raw))
        except ValueError as exc:
            hint = (
                ": it looks like a web page, check the URL"
                if _looks_like_web_page(raw)
                else ""
            )
            raise InputError(f"{path} is not a PEM certificate{hint}") from exc
    return certs


def _check_timestamps(
    args: argparse.Namespace, seal_path: Path, auth: Any
) -> tuple[Optional[str], list[str]]:
    """Each record's RFC 3161 token, checked offline against the pinned TSA."""
    from openadapt_flow.seal_timestamp import (
        authorization_fields,
        seal_fields,
        timestamp_path,
        verify_record_timestamp,
    )

    trusted = _tsa_trusted(args)
    records = [("Seal", seal_fields(seal_path), timestamp_path(seal_path))]
    if auth is not None:
        from openadapt_flow.run_authorization import _paths

        statement = _paths(Path(args.authorization))[0]
        records.append(
            (
                "authorization",
                authorization_fields(statement),
                timestamp_path(statement),
            )
        )
    lines = []
    for label, fields, path in records:
        result = verify_record_timestamp(fields, path, trusted)
        if not result.ok:
            return f"{label} {result.reason}", []
        lines.append(
            f"{label} timestamped at {result.gen_time} by {result.tsa_subject} "
            f"(RFC 3161, checked offline against {result.trust_anchor_subject})"
        )
    return None, lines


def _cmd_timestamp(args: argparse.Namespace) -> int:
    from openadapt_flow.countersign.tsa import SIGSTORE_TSA_URL, HttpTsa
    from openadapt_flow.run_authorization import STATEMENT_FILE
    from openadapt_flow.seal_timestamp import (
        RecordTimestampError,
        authorization_fields,
        seal_fields,
        timestamp_path,
        timestamp_record,
    )

    target = Path(args.target)
    if target.is_dir() and (target / STATEMENT_FILE).is_file():
        record = target / STATEMENT_FILE
    elif target.name == STATEMENT_FILE:
        record = target
    else:
        record = target / "seal.json" if target.is_dir() else target
    _input_json(record)
    if record.name == STATEMENT_FILE:
        _input_bytes(record.with_name(record.name + ".sig"))
    trusted = _tsa_trusted(args)
    try:
        if record.name == STATEMENT_FILE:
            fields = authorization_fields(record)
        else:
            fields = seal_fields(record)
        out = timestamp_path(record)
        stamp = timestamp_record(
            fields,
            out,
            HttpTsa(args.tsa_url or SIGSTORE_TSA_URL),
            trusted=trusted,
        )
    except (RecordTimestampError, OSError, ValueError) as exc:
        print(f"not timestamped: {exc}", file=sys.stderr)
        return 1
    print(f"timestamped: {out} at {stamp.get('gen_time')} by {stamp['tsa']}")
    return 0


def _verify_authorization(
    args: argparse.Namespace, registry: Any, log_keys: Any
) -> Any:
    from openadapt_flow import no_outbound
    from openadapt_flow.run_authorization import AuthorizationVerification, verify
    from openadapt_flow.ssh_sig import fetch_github_keys

    live = None
    if args.live:
        if no_outbound.enabled():
            return AuthorizationVerification(
                False, reason="auth_live_unavailable", detail="strict no-outbound mode"
            )
        live = fetch_github_keys
    return verify(
        Path(args.authorization),
        allowed_signers=Path(args.allowed_signers) if args.allowed_signers else None,
        live=live,
        registry=registry,
        require_anchor=args.require_anchor,
        log_keys=log_keys,
    )


def _timing_line(result: Any, auth: Any) -> str:
    who = (
        auth.statement.authorizer
        if auth.statement is not None and auth.identity_verified
        else "an unverified key"
    )
    when = _utc(auth.anchor.integrated_time) if auth.anchor is not None else None
    if result.authorization_timing == "before_run":
        return (
            f"timing: authorized by {who} before this run (logged {when}; the run's "
            "start time is its own report's claim)"
        )
    if result.authorization_timing == "after_run":
        return f"timing: authorization signed by {who} on {when}, after this run"
    return (
        "timing: no transparency-log time to compare with the run (anchor the "
        "authorization and pass --require-anchor)"
    )


def _authorized_line(auth: Any) -> str:
    st = auth.statement
    if auth.identity_verified:
        who = (
            f"authorized by {st.authorizer} (SSH key {auth.signer_fingerprint}, "
            f"identity checked against {auth.signers_source})"
        )
    else:
        who = (
            f"authorized by SSH key {auth.signer_fingerprint}: identity not verified "
            f"(self-asserted {st.authorizer}; pass --live or --allowed-signers)"
        )
    line = (
        f"{who} at {st.issued_at}, for {', '.join(st.scope.items)} under the "
        f"{st.scope.profile} profile"
        + ("" if st.scope.reusable else ", one run per item")
    )
    if auth.anchor is not None and auth.anchor.ok:
        line += (
            f"; authorization anchored at {_utc(auth.anchor.integrated_time)}, "
            f"log index {auth.anchor.log_index}"
        )
    return line


def _cmd_authorize_create(args: argparse.Namespace) -> int:
    from openadapt_flow.run_authorization import create, signing_command

    try:
        key_ids = args.seal_key_id or [_signer(args).key_id]
        path = create(
            Path(args.out),
            bundle_content_digest=args.bundle_digest,
            items=[i for group in args.item for i in group.split(",") if i],
            profile=args.profile,
            authorizer=args.authorizer,
            seal_key_ids=key_ids,
            signer_fingerprint=args.signer_fingerprint,
            reusable=args.reusable,
        )
    except Exception as exc:  # validation, network and file errors: one line
        code = getattr(exc, "code", type(exc).__name__)
        print(f"not created: {code}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {path} and {path.parent / 'run-authorization.allowed_signers'}")
    print("sign it with your GitHub SSH key:")
    print(signing_command(path))
    return 0


def _statement_input(target: str) -> None:
    from openadapt_flow.run_authorization import _paths

    _input_json(_paths(Path(target))[0])


def _cmd_authorize_sign(args: argparse.Namespace) -> int:
    from openadapt_flow.authorization_summary import sign

    _statement_input(args.statement)

    return sign(
        Path(args.statement),
        Path(args.key_file).expanduser(),
        [Path(b).expanduser() for b in args.bundle],
        dry_run=args.dry_run,
    )


def _cmd_authorize_verify(args: argparse.Namespace) -> int:
    _statement_input(args.authorization)
    try:
        registry = _registry(args) if args.require_anchor else []
        log_keys = _log_keys(args)
    except Exception as exc:
        print(f"not valid: {exc}", file=sys.stderr)
        return 1
    auth = _verify_authorization(args, registry, log_keys)
    if not auth.ok:
        print(f"not valid: {auth.reason} {auth.detail}".rstrip(), file=sys.stderr)
        return 1
    print(f"valid run authorization {auth.sha256}")
    print(_authorized_line(auth))
    return 0


def _cmd_authorize_anchor(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError
    from openadapt_flow.run_authorization import AuthorizationError, anchor
    from openadapt_flow.seal_anchor import AnchorError

    _statement_input(args.authorization)

    try:
        entry = anchor(
            Path(args.authorization),
            _signer(args),
            log_url=args.log_url,
            log_keys=_log_keys(args),
        )["entry"]
    except (
        AuthorizationError,
        AnchorError,
        LocalSealError,
        OSError,
        ValueError,
    ) as exc:
        print(f"not anchored: {exc}", file=sys.stderr)
        return 1
    print(
        f"anchored: entry {entry['uuid']}, log index {entry['logIndex']}, "
        f"at {_utc(entry['integratedTime'])}"
    )
    return 0


def _cmd_profile(args: argparse.Namespace) -> int:
    from openadapt_flow.operator_profile import PROFILE_FILE, build_profile

    try:
        registry = _registry(args)
        log_keys = _log_keys(args)
    except Exception as exc:
        print(f"no profile: {exc}", file=sys.stderr)
        return 1
    auths = []
    for path in args.authorization:
        args.authorization = path  # _verify_authorization reads one path
        auth = _verify_authorization(args, registry, log_keys)
        if not auth.ok:
            print(f"authorization {path} not valid: {auth.reason}", file=sys.stderr)
            return 1
        auths.append(auth)
    profile = build_profile(
        [Path(p) for p in args.run_dirs],
        registry,
        log_keys=log_keys,
        authorizations=auths,
    )
    out = Path(args.out) if args.out else Path(PROFILE_FILE)
    out.write_text(
        json.dumps(profile, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    counted = sum(op["runs"] for op in profile["operators"].values())
    print(
        f"wrote {out}: {counted} anchored Seals counted, "
        f"{profile['excluded']['unanchored']} unanchored and "
        f"{profile['excluded']['not_valid']} not valid excluded"
    )
    return 0


def _log_keys(args: argparse.Namespace) -> Any:
    from urllib.parse import urlparse

    from openadapt_flow.seal_anchor import LogKey, default_log_keys

    if not args.log_key:
        return default_log_keys()
    origin = args.log_origin or urlparse(args.log_url).hostname or ""
    keys = []
    for p in args.log_key:
        raw = _input_bytes(Path(p))
        try:
            keys.append(LogKey.from_pem(raw, origin=origin))
        except (TypeError, ValueError) as exc:
            hint = (
                ": it looks like a web page, check the URL"
                if _looks_like_web_page(raw)
                else ""
            )
            raise InputError(f"{p} is not a PEM public key{hint}") from exc
    return keys


def _utc(seconds: Any) -> str:
    from datetime import datetime, timezone

    return datetime.fromtimestamp(int(seconds), timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _cmd_anchor(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import SEAL_FILE, LocalSealError
    from openadapt_flow.seal_anchor import AnchorError, anchor_path, anchor_seal

    target = Path(args.target)
    seal_path = target / SEAL_FILE if target.is_dir() else target
    _input_json(seal_path)
    log_keys = _log_keys(args)
    try:
        signer = _signer(args)
        anchor = anchor_seal(seal_path, signer, log_url=args.log_url, log_keys=log_keys)
    except (LocalSealError, AnchorError, OSError, ValueError) as exc:
        print(f"not anchored: {exc}", file=sys.stderr)
        return 1
    entry = anchor["entry"]
    print(f"anchored: {anchor_path(seal_path)}")
    print(
        f"entry {entry['uuid']}, log index {entry['logIndex']}, at {_utc(entry['integratedTime'])}"
    )
    print(f"{args.log_url.rstrip('/')}/api/v1/log/entries/{entry['uuid']}")
    return 0


def _cmd_key(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError

    try:
        signer = _signer(args)
    except LocalSealError as exc:
        print(f"no key: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"keys": [signer.registry_entry()]}, indent=2))
    return 0


def _cmd_key_create(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_keys import KeyUnavailable, create_key

    if not args.key:
        print(
            "key create needs --key file:PATH or --key keychain:NAME", file=sys.stderr
        )
        return 2
    try:
        signer = create_key(args.key)
    except KeyUnavailable as exc:
        print(f"no key created: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"keys": [signer.registry_entry()]}, indent=2))
    return 0


def _read_registry_file(path: Path) -> list[dict[str, Any]]:
    from openadapt_flow.offline_seal import load_registry, loads_strict

    if not path.exists():
        return []
    load_registry(path)  # refuse to edit a registry that does not parse
    data = loads_strict(path.read_text(encoding="utf-8"))
    entries = data.get("keys") if isinstance(data, dict) else data
    return [dict(e) for e in entries or []]


def _write_registry_file(path: Path, entries: list[dict[str, Any]]) -> None:
    from openadapt_types.seal import parse_key_registry

    parse_key_registry(entries)
    path.write_text(json.dumps({"keys": entries}, indent=2) + "\n", encoding="utf-8")


def _cmd_key_pin(args: argparse.Namespace) -> int:
    from openadapt_flow.offline_seal import LocalSealError

    try:
        entry = _signer(args).registry_entry()
    except LocalSealError as exc:
        print(f"no key: {exc}", file=sys.stderr)
        return 1
    path = Path(args.registry)
    try:
        entries = _read_registry_file(path)
    except ValueError as exc:
        print(f"not pinned: {exc}", file=sys.stderr)
        return 1
    if any(e.get("key_id") == entry["key_id"] for e in entries):
        print(f"already pinned: {entry['key_id']}")
        return 0
    _write_registry_file(path, entries + [entry])
    print(f"pinned {entry['key_id']} in {path}")
    return 0


def _cmd_key_revoke(args: argparse.Namespace) -> int:
    from datetime import datetime, timezone

    path = Path(args.registry)
    try:
        entries = _read_registry_file(path)
    except ValueError as exc:
        print(f"not revoked: {exc}", file=sys.stderr)
        return 1
    at = args.at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    found = False
    for entry in entries:
        if entry.get("key_id") == args.key_id:
            entry["status"], entry["revoked_at"] = "revoked", at
            found = True
    if not found:
        print(f"{args.key_id} is not in {path}", file=sys.stderr)
        return 1
    try:
        _write_registry_file(path, entries)
    except ValueError as exc:
        print(f"not revoked: {exc}", file=sys.stderr)
        return 1
    print(
        f"revoked {args.key_id} at {at}; every Seal it signed is now refused, "
        "unless a transparency-log anchor shows the log recorded it before then "
        "(verify --require-anchor)"
    )
    return 0


def _cmd_batch(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_batch import build_batch

    path = build_batch(_home(args))
    print(str(path) if path else "nothing queued")
    return 0


def _authority(args: argparse.Namespace) -> Any:
    try:
        from openadapt_flow.countersign.tsa import SIGSTORE_TSA_URL, HttpTsa
    except ImportError:
        return None
    return HttpTsa(args.tsa_url or SIGSTORE_TSA_URL)


def _cmd_submit(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_batch import submit_all

    authority = _authority(args)
    if authority is None:
        print(
            "not submitted: countersign_unavailable (this build has no "
            "openadapt_flow.countersign; install the countersign extra)",
            file=sys.stderr,
        )
        return 1
    results = submit_all(authority, _home(args))
    for r in results:
        print(f"{r.batch.name}: {r.status}{(' ' + r.detail) if r.detail else ''}")
    return (
        0
        if all(r.status in ("timestamped", "already_timestamped") for r in results)
        else 1
    )


def _cmd_proof(args: argparse.Namespace) -> int:
    from openadapt_flow.seal_batch import proof_for

    seal = json.loads(Path(args.seal).read_text(encoding="utf-8"))
    found = proof_for(seal, _home(args))
    if not found:
        print("not in any batch yet", file=sys.stderr)
        return 1
    print(json.dumps(found, indent=2, sort_keys=True))
    return 0 if found["verified_inclusion"] else 1


def _auth_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--authorization",
        default=None,
        help="Also require this run authorization (run-authorization.json or its folder)",
    )
    p.add_argument(
        "--allowed-signers",
        default=None,
        help="Pinned ssh-keygen allowed-signers file (default: the stored GitHub snapshot)",
    )


def _log_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--log-url",
        default="https://rekor.sigstore.dev",
        help="The transparency log's URL (default the public Sigstore Rekor)",
    )
    p.add_argument(
        "--log-origin",
        default=None,
        help="Checkpoint origin name for --log-key (default the --log-url host)",
    )


def register(sub: Any) -> None:
    p = sub.add_parser(
        "local-seal",
        help="Offline Seals signed with this machine's key; optional batched timestamps",
    )
    p.add_argument(
        "--home",
        default=None,
        help="Seal home (or OPENADAPT_SEAL_HOME; default ~/.openadapt/seal)",
    )
    p.add_argument(
        "--key",
        default=None,
        help=(
            "Seal key: file:PATH, keychain:NAME or pkcs11:URI (or "
            "OPENADAPT_SEAL_KEY; default a key generated under the Seal home)"
        ),
    )
    s = p.add_subparsers(dest="local_seal_cmd", required=True)

    r = s.add_parser("run", help="Seal a run directory's report.json")
    r.add_argument("run_dir")
    r.add_argument(
        "--authorization",
        default=None,
        help="Seal under this signed run authorization (run-authorization.json or its folder)",
    )
    r.add_argument("--item", default=None, help="The scope item this run is, e.g. r3")
    r.add_argument(
        "--replace-unanchored",
        action="store_true",
        help="Re-sign an existing, never-anchored Seal over the same report",
    )
    r.set_defaults(func=_guarded(_cmd_run))

    f = s.add_parser("file", help="Seal any file: a record, an export, an audit report")
    f.add_argument("path")
    f.add_argument("--kind", choices=["record", "export", "file"], default="file")
    f.add_argument("--run-id", default=None)
    f.add_argument("--out", default=None, help="Default: PATH.seal.json")
    f.set_defaults(func=_guarded(_cmd_file))

    v = s.add_parser("verify", help="Verify a run directory, or a file with --seal")
    v.add_argument("target", nargs="+", help="Run directories, or one file with --seal")
    v.add_argument("--seal", default=None)
    v.add_argument("--keys", default=None, help="Pinned key registry JSON")
    v.add_argument(
        "--strict",
        action="store_true",
        help="Require the Seal to match report.json itself, not report.pre-seal.json",
    )
    v.add_argument(
        "--require-anchor",
        action="store_true",
        help="Also require a transparency-log anchor (seal.anchor.json), checked offline",
    )
    v.add_argument(
        "--log-key",
        action="append",
        default=[],
        help="Pinned log public key PEM (default: the public Sigstore Rekor key)",
    )
    v.add_argument(
        "--live",
        action="store_true",
        help="Also fetch the entry from the log at --log-url and compare (needs network)",
    )
    _log_args(v)
    _auth_args(v)
    v.add_argument(
        "--require-timestamp",
        action="store_true",
        help="Also require each record's RFC 3161 token (seal.tsa.json), checked offline",
    )
    v.add_argument(
        "--tsa-cert",
        action="append",
        default=[],
        help="Pinned TSA certificate PEM (default: the bundled Sigstore TSA root)",
    )
    v.set_defaults(func=_guarded(_cmd_verify))

    a = s.add_parser(
        "anchor",
        help=(
            "Log a Seal's digest in Sigstore Rekor (opt-in; sends a digest and "
            "the public key, never report content)"
        ),
    )
    a.add_argument("target", help="A run directory, or a .seal.json file")
    a.add_argument(
        "--log-key",
        action="append",
        default=[],
        help="The log's public key PEM, to check its answer (default: Sigstore Rekor)",
    )
    _log_args(a)
    a.set_defaults(func=_guarded(_cmd_anchor))

    ts = s.add_parser(
        "timestamp",
        help="One RFC 3161 token for a Seal or a run authorization (sends a 32-byte digest)",
    )
    ts.add_argument(
        "target", help="A run directory, a .seal.json, or an authorization folder"
    )
    ts.add_argument(
        "--tsa-url", default=None, help="RFC 3161 TSA URL (default Sigstore)"
    )
    ts.add_argument("--tsa-cert", action="append", default=[])
    ts.set_defaults(func=_guarded(_cmd_timestamp))

    z = s.add_parser(
        "authorize", help="Run authorizations signed with a person's GitHub SSH key"
    )
    zs = z.add_subparsers(dest="authorize_cmd", required=True)
    zc = zs.add_parser(
        "create", help="Write an unsigned authorization and snapshot GitHub's keys"
    )
    zc.add_argument("--out", default=".", help="Folder for run-authorization.json")
    zc.add_argument("--bundle-digest", required=True, help="bundle_content_digest")
    zc.add_argument(
        "--item", action="append", required=True, help="Scope item(s), e.g. r3,r4"
    )
    zc.add_argument("--profile", default="standard", help="Execution profile")
    zc.add_argument("--authorizer", required=True, help="github:<user>")
    zc.add_argument(
        "--seal-key-id",
        action="append",
        default=[],
        help="Seal key allowed to seal under it (default: the configured key)",
    )
    zc.add_argument(
        "--signer-fingerprint",
        default=None,
        help="SHA256:... of the GitHub key that will sign (needed when GitHub lists several)",
    )
    zc.add_argument(
        "--reusable",
        action="store_true",
        help="Allow more than one run per item (default: one run per item)",
    )
    zc.set_defaults(func=_guarded(_cmd_authorize_create))
    zg = zs.add_parser(
        "sign",
        help="Show what the authorization allows, ask, then sign with ssh-keygen",
    )
    zg.add_argument("statement", help="run-authorization.json (or its folder)")
    zg.add_argument(
        "--key",
        dest="key_file",
        required=True,
        help="Your SSH private key, the one GitHub lists (e.g. ~/.ssh/id_ed25519)",
    )
    zg.add_argument(
        "--bundle",
        action="append",
        default=[],
        help="The program bundle the statement names (default: ./bundle next to it)",
    )
    zg.add_argument("--dry-run", action="store_true", help="Print the summary only")
    zg.set_defaults(func=_guarded(_cmd_authorize_sign))
    zv = zs.add_parser("verify", help="Verify a signed run authorization")
    zv.add_argument("authorization")
    zv.add_argument("--allowed-signers", default=None)
    zv.add_argument("--keys", default=None, help="Pinned Seal keys (for the anchor)")
    zv.add_argument("--require-anchor", action="store_true")
    zv.add_argument(
        "--live", action="store_true", help="Re-read the keys from github.com"
    )
    zv.add_argument("--log-key", action="append", default=[])
    _log_args(zv)
    zv.set_defaults(func=_guarded(_cmd_authorize_verify))
    za = zs.add_parser(
        "anchor", help="Log the authorization's digest in Rekor with the Seal key"
    )
    za.add_argument("authorization")
    za.add_argument("--log-key", action="append", default=[])
    _log_args(za)
    za.set_defaults(func=_guarded(_cmd_authorize_anchor))

    pr = s.add_parser(
        "profile",
        help="Pseudonymous operator record from anchored Seals (operator-profile.json)",
    )
    pr.add_argument("run_dirs", nargs="+")
    pr.add_argument("--keys", default=None, help="Pinned Seal key registry")
    pr.add_argument(
        "--authorization",
        action="append",
        default=[],
        help="Run authorization(s) that may link a GitHub identity",
    )
    pr.add_argument("--allowed-signers", default=None)
    pr.add_argument("--log-key", action="append", default=[])
    pr.add_argument("--out", default=None, help="Default ./operator-profile.json")
    pr.add_argument(
        "--live",
        action="store_true",
        help="Check each authorization's key against github.com now (links an identity)",
    )
    _log_args(pr)
    pr.set_defaults(func=_guarded(_cmd_profile), require_anchor=False)

    k = s.add_parser("key", help="Print the Seal key's public half for pinning")
    k.set_defaults(func=_guarded(_cmd_key))
    ks = k.add_subparsers(dest="key_cmd")
    kc = ks.add_parser(
        "create", help="Make a new key at --key file:PATH or keychain:NAME"
    )
    kc.set_defaults(func=_guarded(_cmd_key_create))
    kp = ks.add_parser("pin", help="Add the Seal key to a pinned key registry")
    kp.add_argument("registry", help="Key registry JSON to create or extend")
    kp.set_defaults(func=_guarded(_cmd_key_pin))
    kr = ks.add_parser("revoke", help="Mark a pinned key revoked from a time on")
    kr.add_argument("registry")
    kr.add_argument("key_id")
    kr.add_argument("--at", default=None, help="UTC YYYY-MM-DDTHH:MM:SSZ (default now)")
    kr.set_defaults(func=_guarded(_cmd_key_revoke))

    b = s.add_parser("batch", help="Fold queued Seals into one Merkle batch (local)")
    b.set_defaults(func=_guarded(_cmd_batch))

    t = s.add_parser(
        "submit",
        help="Timestamp every unsent batch root (sends one 32-byte digest per batch)",
    )
    t.add_argument(
        "--tsa-url", default=None, help="RFC 3161 TSA URL (default Sigstore)"
    )
    t.set_defaults(func=_guarded(_cmd_submit))

    q = s.add_parser(
        "proof", help="Show the batch, root and inclusion proof for a Seal"
    )
    q.add_argument("seal")
    q.set_defaults(func=_guarded(_cmd_proof))
