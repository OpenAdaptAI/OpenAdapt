"""Clean reset and host-enforced egress for dedicated Windows pool workers."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

EGRESS_SCHEMA = "openadapt.windows-host-egress/v1"
RESET_SCHEMA = "openadapt.windows-pool-reset/v1"
EGRESS_PROOF_SCHEMA = "openadapt.windows-host-egress-proof/v1"
START_PROOF_SCHEMA = "openadapt.windows-qualified-start-proof/v1"
WORKER_IDENTITY_SCHEMA = "openadapt.windows-worker-instance-identity/v1"
EGRESS_DOMAIN = b"openadapt-windows-host-egress-v1\0"
WORKER_IDENTITY_DOMAIN = b"OpenAdapt Windows worker instance identity v1\0"
RESET_PROOF_DOMAIN = b"OpenAdapt Windows pool reset proof v1\0"
START_PROOF_DOMAIN = b"OpenAdapt Windows qualified start proof v1\0"
DISPATCH_BINDING_DOMAIN = b"OpenAdapt Windows qualified dispatch binding v1\0"
SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
HEX64 = re.compile(r"^[a-f0-9]{64}$")
SHA256 = re.compile(r"^sha256:[a-f0-9]{64}$")
SSH_PUBLIC_KEY = re.compile(r"^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa) ([A-Za-z0-9+/]+={0,2})$")
MAX_GATE_AGE = timedelta(minutes=5)
METADATA_IPS = frozenset(
    {
        "169.254.169.254",
        "168.63.129.16",
        "100.100.100.200",
        "fd00:ec2::254",
    }
)


class WindowsPoolIsolationError(RuntimeError):
    """The worker cannot prove a clean reset or an enforced egress policy."""


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def egress_policy_sha256(value: object) -> str:
    policy = validate_egress_policy(value)
    return hashlib.sha256(EGRESS_DOMAIN + canonical_json(policy)).hexdigest()


def validate_egress_policy(value: object) -> Mapping[str, Any]:
    keys = {
        "schema_version",
        "run_id",
        "allowed_ipv4",
        "allowed_ipv6",
        "host_bindings_sha256",
    }
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WindowsPoolIsolationError("egress policy is not a closed object")
    if value["schema_version"] != EGRESS_SCHEMA:
        raise WindowsPoolIsolationError("egress policy schema is invalid")
    run_id = value["run_id"]
    if not isinstance(run_id, str) or SAFE_NAME.fullmatch(run_id) is None:
        raise WindowsPoolIsolationError("egress run id is invalid")
    host_bindings = value["host_bindings_sha256"]
    if not isinstance(host_bindings, str) or HEX64.fullmatch(host_bindings) is None:
        raise WindowsPoolIsolationError("egress host binding digest is invalid")
    for key, family in (("allowed_ipv4", 4), ("allowed_ipv6", 6)):
        addresses = value[key]
        if (
            not isinstance(addresses, list)
            or addresses != sorted(addresses)
            or len(addresses) != len(set(addresses))
        ):
            raise WindowsPoolIsolationError(f"{key} must be unique and sorted")
        for raw in addresses:
            try:
                address = ipaddress.ip_address(raw)
            except ValueError as exc:
                raise WindowsPoolIsolationError(f"{key} contains an invalid address") from exc
            if (
                address.version != family
                or address.is_unspecified
                or address.is_multicast
                or address.is_loopback
                or address.is_link_local
                or str(address) in METADATA_IPS
            ):
                raise WindowsPoolIsolationError(f"{key} contains an unsafe address")
    return value


def load_egress_policy(path: Path) -> Mapping[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise WindowsPoolIsolationError("egress policy is not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsPoolIsolationError("egress policy cannot be read") from exc
    return validate_egress_policy(value)


@dataclass(frozen=True)
class WorkerIdentity:
    schema_version: str
    provider: str
    provider_account_id: str
    resource_id: str
    instance_id: str
    ssh_host: str
    ssh_host_public_key: str
    ssh_host_key_sha256: str
    baseline_manifest_sha256: str
    admitted_image_sha256: str
    admitted_runtime_sha256: str
    host_bindings_sha256: str
    tls_bindings_sha256: str


def validate_worker_identity(value: object) -> WorkerIdentity:
    keys = set(WorkerIdentity.__dataclass_fields__)
    if not isinstance(value, Mapping) or set(value) != keys:
        raise WindowsPoolIsolationError("worker identity is not a closed object")
    try:
        identity = WorkerIdentity(**value)
    except TypeError as exc:  # pragma: no cover - closed keys already prove this
        raise WindowsPoolIsolationError("worker identity is invalid") from exc
    if identity.schema_version != WORKER_IDENTITY_SCHEMA:
        raise WindowsPoolIsolationError("worker identity schema is invalid")
    if identity.provider not in {"azure", "aws", "customer-controlled"}:
        raise WindowsPoolIsolationError("worker identity provider is invalid")
    for field in ("provider_account_id", "resource_id", "ssh_host"):
        raw = getattr(identity, field)
        if not isinstance(raw, str) or not raw or len(raw) > 512 or "\x00" in raw:
            raise WindowsPoolIsolationError(f"worker identity {field} is invalid")
    if (
        not isinstance(identity.instance_id, str)
        or SAFE_NAME.fullmatch(identity.instance_id) is None
    ):
        raise WindowsPoolIsolationError("worker identity instance_id is invalid")
    if (
        not isinstance(identity.ssh_host_public_key, str)
        or SSH_PUBLIC_KEY.fullmatch(identity.ssh_host_public_key) is None
    ):
        raise WindowsPoolIsolationError("worker identity SSH public key is invalid")
    expected_host_key = (
        "sha256:" + hashlib.sha256(identity.ssh_host_public_key.encode("ascii")).hexdigest()
    )
    if identity.ssh_host_key_sha256 != expected_host_key:
        raise WindowsPoolIsolationError("worker identity SSH host key digest differs")
    for field in (
        "admitted_image_sha256",
        "admitted_runtime_sha256",
        "baseline_manifest_sha256",
        "host_bindings_sha256",
        "tls_bindings_sha256",
    ):
        if (
            not isinstance(getattr(identity, field), str)
            or SHA256.fullmatch(getattr(identity, field)) is None
        ):
            raise WindowsPoolIsolationError(f"worker identity {field} is invalid")
    return identity


def load_worker_identity(path: Path) -> WorkerIdentity:
    try:
        if path.is_symlink() or not path.is_file():
            raise WindowsPoolIsolationError("worker identity is not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsPoolIsolationError("worker identity cannot be read") from exc
    return validate_worker_identity(value)


def worker_identity_sha256(identity: WorkerIdentity | Mapping[str, Any]) -> str:
    normalized = validate_worker_identity(
        identity.__dict__ if isinstance(identity, WorkerIdentity) else identity
    )
    return (
        "sha256:"
        + hashlib.sha256(WORKER_IDENTITY_DOMAIN + canonical_json(normalized.__dict__)).hexdigest()
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: str, context: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise WindowsPoolIsolationError(f"{context} is invalid") from exc
    if parsed.tzinfo is None:
        raise WindowsPoolIsolationError(f"{context} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _safe_name(value: str, label: str) -> str:
    if SAFE_NAME.fullmatch(value) is None:
        raise WindowsPoolIsolationError(f"{label} is invalid")
    return value


def _nft_table(worker: str) -> str:
    _safe_name(worker, "worker name")
    # Each pool worker has one dedicated Linux VM host. A fixed name lets the
    # boot-time service restore the last fail-closed policy before Docker starts.
    return "oa_windows"


def build_nft_batch(
    worker: str,
    policy: Mapping[str, Any],
    *,
    bridge_interface: str = "docker0",
) -> str:
    """Return one atomic nftables transaction for a dedicated worker host."""

    _safe_name(worker, "worker name")
    _safe_name(bridge_interface, "bridge interface")
    parsed = validate_egress_policy(policy)
    table = _nft_table(worker)
    lines = [
        f"flush chain inet {table} forward",
    ]
    for address in parsed["allowed_ipv4"]:
        lines.append(
            f'add rule inet {table} forward iifname "{bridge_interface}" ip daddr {address} accept'
        )
        lines.append(
            f'add rule inet {table} forward oifname "{bridge_interface}" '
            f"ip saddr {address} ct state established,related accept"
        )
    for address in parsed["allowed_ipv6"]:
        lines.append(
            f'add rule inet {table} forward iifname "{bridge_interface}" ip6 daddr {address} accept'
        )
        lines.append(
            f'add rule inet {table} forward oifname "{bridge_interface}" '
            f"ip6 saddr {address} ct state established,related accept"
        )
    # Return rules bind the remote source to the current allow-list. A generic
    # established/related rule would keep a connection from an earlier run.
    lines.append(f'add rule inet {table} forward iifname "{bridge_interface}" counter drop')
    return "\n".join(lines) + "\n"


def build_block_all_batch(
    worker: str,
    run_id: str,
    *,
    host_bindings_sha256: str = "0" * 64,
    bridge_interface: str = "docker0",
) -> str:
    return build_nft_batch(
        worker,
        {
            "schema_version": EGRESS_SCHEMA,
            "run_id": run_id,
            "allowed_ipv4": [],
            "allowed_ipv6": [],
            "host_bindings_sha256": host_bindings_sha256,
        },
        bridge_interface=bridge_interface,
    )


RESET_SCRIPT = r"""
set -euo pipefail
worker="$1"
run_id="$2"
storage="$3"
baseline="$4"
expected="$5"
container="$6"
lock="/var/lock/openadapt-windows-${worker}.lock"
exec 9>"$lock"
flock -n 9
gate_root=/var/lib/openadapt/windows-run-gates
install -d -m 0700 "$gate_root"
test ! -e "$gate_root/${run_id}.reset"
test ! -e "$gate_root/${run_id}.reset-consumed"
test ! -e "$gate_root/${run_id}.egress"
test ! -e "$gate_root/${run_id}.consumed"
test -d "$baseline"
test ! -L "$baseline"
test ! -L "$storage"
manifest="$baseline/openadapt-windows-baseline-manifest.json"
test -f "$manifest"
test ! -L "$manifest"
actual="$(sha256sum "$manifest" | cut -d' ' -f1)"
test "$actual" = "$expected"
verify_baseline() {
python3 - "$1" <<'PY'
import hashlib
import json
import pathlib
import sys

root = pathlib.Path(sys.argv[1])
manifest_path = root / "openadapt-windows-baseline-manifest.json"
manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
if set(manifest) != {"schema_version", "artifacts"}:
    raise SystemExit("baseline manifest is not closed")
if manifest["schema_version"] != "openadapt.windows-baseline-manifest/v1":
    raise SystemExit("baseline manifest schema is invalid")
artifacts = manifest["artifacts"]
if not isinstance(artifacts, list) or not artifacts:
    raise SystemExit("baseline artifact inventory is empty")
prior = ""
declared = set()
for item in artifacts:
    if not isinstance(item, dict) or set(item) != {"path", "size_bytes", "sha256"}:
        raise SystemExit("baseline artifact is not closed")
    relative = item["path"]
    path = pathlib.PurePosixPath(relative) if isinstance(relative, str) else None
    if path is None or path.is_absolute() or ".." in path.parts or relative <= prior:
        raise SystemExit("baseline artifact path is invalid")
    candidate = root.joinpath(*path.parts)
    if candidate.is_symlink() or not candidate.is_file():
        raise SystemExit("baseline artifact is not a regular file")
    payload = candidate.read_bytes()
    if len(payload) != item["size_bytes"] or hashlib.sha256(payload).hexdigest() != item["sha256"]:
        raise SystemExit("baseline artifact digest differs")
    declared.add(relative)
    prior = relative
actual = set()
for path in root.rglob("*"):
    if path.is_symlink():
        raise SystemExit("baseline contains a symbolic link")
    if path.is_dir():
        continue
    if not path.is_file():
        raise SystemExit("baseline contains a non-regular artifact")
    if path != manifest_path:
        actual.add(path.relative_to(root).as_posix())
if actual != declared:
    raise SystemExit("baseline artifact inventory is incomplete")
PY
}
verify_baseline "$baseline"
address_file=/etc/openadapt/windows-last-guest-addresses
address_temporary="${address_file}.tmp"
docker inspect "$container" --format '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{"\n"}}{{.GlobalIPv6Address}}{{"\n"}}{{end}}' \
    2>/dev/null | sed '/^$/d' | sort -u > "$address_temporary" || true
if test -s "$address_temporary"; then
    python3 - "$address_temporary" <<'PY'
import ipaddress
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
for line in path.read_text(encoding="ascii").splitlines():
    ipaddress.ip_address(line)
PY
    chmod 0600 "$address_temporary"
    mv "$address_temporary" "$address_file"
else
    rm -f "$address_temporary"
fi
docker rm -f "$container" >/dev/null 2>&1 || true
parent="$(dirname "$storage")"
stage="${parent}/.openadapt-reset-${run_id}"
prior="${parent}/.openadapt-prior-${run_id}"
test ! -e "$stage"
test ! -e "$prior"
mkdir -m 0700 "$stage"
cp -a --reflink=auto "$baseline"/. "$stage"/
stage_actual="$(sha256sum "$stage/openadapt-windows-baseline-manifest.json" | cut -d' ' -f1)"
test "$stage_actual" = "$expected"
verify_baseline "$stage"
if test -e "$storage"; then mv "$storage" "$prior"; fi
mv "$stage" "$storage"
if test -e "$prior"; then rm -rf --one-file-system "$prior"; fi
printf '{"schema_version":"openadapt.windows-pool-reset/v1","worker":"%s","run_id":"%s","baseline_sha256":"%s","container_state":"stopped"}\n' "$worker" "$run_id" "$expected"
""".strip()


@dataclass(frozen=True)
class ResetProof:
    schema_version: str
    worker: str
    run_id: str
    baseline_sha256: str
    container_state: str
    worker_identity_sha256: str
    admitted_image_sha256: str
    admitted_runtime_sha256: str
    host_bindings_sha256: str
    tls_bindings_sha256: str
    issued_at: str
    expires_at: str


@dataclass(frozen=True)
class EgressProof:
    schema_version: str
    worker: str
    run_id: str
    policy_sha256: str
    reset_proof_sha256: str
    worker_identity_sha256: str
    host_bindings_sha256: str
    tls_bindings_sha256: str
    conntrack_drained: bool
    issued_at: str
    expires_at: str


@dataclass(frozen=True)
class StartProof:
    schema_version: str
    worker: str
    run_id: str
    worker_identity_sha256: str
    reset_proof_sha256: str
    egress_policy_sha256: str
    one_use_consumed: bool
    started_at: str


def _load_proof(path: Path, proof_type: type[ResetProof] | type[EgressProof]):
    try:
        if path.is_symlink() or not path.is_file():
            raise WindowsPoolIsolationError("run proof is not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WindowsPoolIsolationError("run proof cannot be read") from exc
    if not isinstance(value, Mapping) or set(value) != set(proof_type.__dataclass_fields__):
        raise WindowsPoolIsolationError("run proof is not a closed object")
    try:
        return proof_type(**value)
    except TypeError as exc:  # pragma: no cover - closed keys already prove this
        raise WindowsPoolIsolationError("run proof is invalid") from exc


def load_reset_proof(path: Path) -> ResetProof:
    return _load_proof(path, ResetProof)


def load_egress_proof(path: Path) -> EgressProof:
    return _load_proof(path, EgressProof)


def reset_proof_sha256(proof: ResetProof) -> str:
    return (
        "sha256:" + hashlib.sha256(RESET_PROOF_DOMAIN + canonical_json(proof.__dict__)).hexdigest()
    )


def start_proof_sha256(proof: StartProof) -> str:
    return (
        "sha256:" + hashlib.sha256(START_PROOF_DOMAIN + canonical_json(proof.__dict__)).hexdigest()
    )


def validate_reset_proof(
    proof: ResetProof,
    *,
    worker: str,
    run_id: str,
    identity: WorkerIdentity,
    now: datetime | None = None,
) -> None:
    current = (now or _now()).astimezone(timezone.utc)
    issued = _timestamp(proof.issued_at, "reset proof issued_at")
    expires = _timestamp(proof.expires_at, "reset proof expires_at")
    if not issued <= current < expires or expires - issued > MAX_GATE_AGE:
        raise WindowsPoolIsolationError("reset proof is stale")
    expected = {
        "schema_version": RESET_SCHEMA,
        "worker": worker,
        "run_id": run_id,
        "baseline_sha256": identity.baseline_manifest_sha256.removeprefix("sha256:"),
        "container_state": "stopped",
        "worker_identity_sha256": worker_identity_sha256(identity),
        "admitted_image_sha256": identity.admitted_image_sha256,
        "admitted_runtime_sha256": identity.admitted_runtime_sha256,
        "host_bindings_sha256": identity.host_bindings_sha256,
        "tls_bindings_sha256": identity.tls_bindings_sha256,
    }
    for field, value in expected.items():
        if getattr(proof, field) != value:
            raise WindowsPoolIsolationError(f"reset proof {field} differs")


def validate_egress_proof(
    proof: EgressProof,
    *,
    worker: str,
    run_id: str,
    policy: Mapping[str, Any],
    reset_proof: ResetProof,
    identity: WorkerIdentity,
    now: datetime | None = None,
) -> None:
    current = (now or _now()).astimezone(timezone.utc)
    issued = _timestamp(proof.issued_at, "egress proof issued_at")
    expires = _timestamp(proof.expires_at, "egress proof expires_at")
    if not issued <= current < expires or expires - issued > MAX_GATE_AGE:
        raise WindowsPoolIsolationError("egress proof is stale")
    parsed = validate_egress_policy(policy)
    expected = {
        "schema_version": EGRESS_PROOF_SCHEMA,
        "worker": worker,
        "run_id": run_id,
        "policy_sha256": egress_policy_sha256(parsed),
        "reset_proof_sha256": reset_proof_sha256(reset_proof),
        "worker_identity_sha256": worker_identity_sha256(identity),
        "host_bindings_sha256": identity.host_bindings_sha256,
        "tls_bindings_sha256": identity.tls_bindings_sha256,
        "conntrack_drained": True,
    }
    for field, value in expected.items():
        if getattr(proof, field) != value:
            raise WindowsPoolIsolationError(f"egress proof {field} differs")


class WindowsPoolIsolationManager:
    """Apply reset and egress controls through one worker's SSH boundary."""

    def __init__(
        self,
        *,
        ssh_host: str,
        ssh_user: str,
        worker: str,
        identity: WorkerIdentity,
        container: str = "winarena",
        storage_dir: str = "/home/azureuser/waa-storage",
        baseline_dir: str = "/home/azureuser/openadapt-windows-baseline",
        bridge_interface: str = "docker0",
    ) -> None:
        self.ssh_host = ssh_host
        self.ssh_user = _safe_name(ssh_user, "SSH user")
        self.worker = _safe_name(worker, "worker name")
        self.container = _safe_name(container, "container name")
        self.storage_dir = storage_dir
        self.baseline_dir = baseline_dir
        self.bridge_interface = _safe_name(bridge_interface, "bridge interface")
        self.identity = validate_worker_identity(identity.__dict__)
        if self.identity.ssh_host != ssh_host:
            raise WindowsPoolIsolationError("worker identity SSH host differs")
        for path in (storage_dir, baseline_dir):
            if not path.startswith("/") or ".." in Path(path).parts:
                raise WindowsPoolIsolationError("isolation path is invalid")

    def _ssh(
        self,
        remote_command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout_seconds: int = 300,
    ) -> subprocess.CompletedProcess[bytes]:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as known_hosts:
            known_hosts.write(f"{self.identity.instance_id} {self.identity.ssh_host_public_key}\n")
            known_hosts.flush()
            process = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={known_hosts.name}",
                    "-o",
                    f"HostKeyAlias={self.identity.instance_id}",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "ConnectTimeout=10",
                    f"{self.ssh_user}@{self.ssh_host}",
                    *remote_command,
                ],
                input=input_bytes,
                capture_output=True,
                timeout=timeout_seconds,
            )
        if process.returncode != 0:
            detail = process.stderr.decode("utf-8", errors="replace").strip()[-500:]
            raise WindowsPoolIsolationError(f"worker isolation command failed: {detail}")
        return process

    def _ensure_boundary(self) -> None:
        """Create the dedicated nftables hook before any run can start."""

        table = _nft_table(self.worker)
        try:
            self._ssh(["sudo", "nft", "list", "table", "inet", table])
        except WindowsPoolIsolationError:
            self._ssh(["sudo", "nft", "add", "table", "inet", table])
        try:
            self._ssh(["sudo", "nft", "list", "chain", "inet", table, "forward"])
        except WindowsPoolIsolationError:
            self._ssh(
                ["sudo", "nft", "-f", "-"],
                input_bytes=(
                    f"add chain inet {table} forward "
                    "{ type filter hook forward priority -50; policy drop; }\n"
                ).encode("ascii"),
            )
        self._ssh(
            ["sudo", "nft", "-f", "-"],
            input_bytes=f"chain inet {table} forward {{ policy drop; }}\n".encode("ascii"),
        )

    def _verify_remote_identity(self) -> None:
        expected = {
            "/etc/openadapt/worker-instance-identity.sha256": worker_identity_sha256(self.identity),
            "/etc/openadapt/admitted-image.sha256": self.identity.admitted_image_sha256,
            "/etc/openadapt/admitted-runtime.sha256": self.identity.admitted_runtime_sha256,
            "/etc/openadapt/baseline-manifest.sha256": self.identity.baseline_manifest_sha256,
            "/etc/openadapt/host-bindings.sha256": self.identity.host_bindings_sha256,
            "/etc/openadapt/tls-bindings.sha256": self.identity.tls_bindings_sha256,
        }
        program = """
set -euo pipefail
while test "$#" -gt 0; do
  path="$1"
  expected="$2"
  test -f "$path"
  test ! -L "$path"
  test "$(stat -c '%u' "$path")" = 0
  mode="$(stat -c '%a' "$path")"
  test "$mode" = 400 -o "$mode" = 600
  actual="$(tr -d '\\n' < "$path")"
  test "$actual" = "$expected"
  shift 2
done
""".strip()
        arguments = [item for pair in expected.items() for item in pair]
        self._ssh(["sudo", "bash", "-c", program, "--", *arguments])

    def _persist_and_apply(
        self,
        batch: str,
        policy_sha256: str,
        *,
        run_id: str | None = None,
        reset_sha256: str | None = None,
    ) -> None:
        """Apply the checked policy and retain its exact active bytes for readback."""

        program = """
import fcntl
import ipaddress
import os
import pathlib
import subprocess
import sys
import tempfile

target = pathlib.Path('/run/openadapt/windows-egress-active.nft')
digest_target = pathlib.Path('/run/openadapt/windows-egress.sha256')
target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
payload = sys.stdin.buffer.read()
if not payload or len(payload) > 1048576:
    raise SystemExit('invalid nft policy size')
worker, run_id, reset_sha256, policy_sha256, identity_sha256 = sys.argv[1:]
lock_path = pathlib.Path('/var/lock') / f'openadapt-windows-{worker}.lock'
lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o600)
fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
gate_root = pathlib.Path('/var/lib/openadapt/windows-run-gates')
gate_root.mkdir(mode=0o700, parents=True, exist_ok=True)
egress_gate = gate_root / f'{run_id}.egress'
consumed_gate = gate_root / f'{run_id}.consumed'
reset_gate = gate_root / f'{run_id}.reset'
reset_consumed_gate = gate_root / f'{run_id}.reset-consumed'
create_gate = reset_sha256 != '-'
gate_created = False
applied = False
temporary = None
try:
    if create_gate:
        if consumed_gate.exists():
            raise SystemExit('run gate already consumed')
        if reset_consumed_gate.exists():
            raise SystemExit('reset proof already consumed')
        reset_digest_path = reset_gate / 'proof.sha256'
        if not reset_digest_path.is_file() or reset_digest_path.is_symlink():
            raise SystemExit('remote reset proof is absent')
        if reset_digest_path.read_text(encoding='ascii').strip() != reset_sha256:
            raise SystemExit('remote reset proof differs')
        running = subprocess.run(
            ['docker', 'ps', '-aq', '--filter', 'name=^winarena$'],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if running:
            raise SystemExit('guest is not stopped')
        gate_fd = os.open(egress_gate, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        gate_created = True
        with os.fdopen(gate_fd, 'w', encoding='ascii') as gate_stream:
            gate_stream.write(f'{policy_sha256}\n{reset_sha256}\n{identity_sha256}\n')
            gate_stream.flush()
            os.fsync(gate_stream.fileno())
    fd, temporary = tempfile.mkstemp(prefix='.windows-egress.', dir=target.parent)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'wb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    subprocess.run(['nft', '-c', '-f', temporary], check=True)
    subprocess.run(['nft', '-f', temporary], check=True)
    subprocess.run(['conntrack', '-F'], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(['conntrack', '-F'], check=True, stdout=subprocess.DEVNULL)
    guest_addresses = pathlib.Path('/etc/openadapt/windows-last-guest-addresses')
    if guest_addresses.exists():
        if guest_addresses.is_symlink() or not guest_addresses.is_file():
            raise SystemExit('guest address evidence is invalid')
        for raw_address in guest_addresses.read_text(encoding='ascii').splitlines():
            address = str(ipaddress.ip_address(raw_address))
            remaining = subprocess.run(
                ['conntrack', '-L', '--orig-src', address],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip()
            if remaining:
                raise SystemExit('guest conntrack state remains after drain')
    os.replace(temporary, target)
    temporary = None
    digest_temporary = digest_target.with_name('.windows-egress.sha256.tmp')
    try:
        digest_temporary.unlink()
    except FileNotFoundError:
        pass
    digest_fd = os.open(
        digest_temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    with os.fdopen(digest_fd, 'w', encoding='ascii') as digest_stream:
        digest_stream.write(policy_sha256 + '\n')
        digest_stream.flush()
        os.fsync(digest_stream.fileno())
    os.replace(digest_temporary, digest_target)
    directory = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    if create_gate:
        os.replace(reset_gate, reset_consumed_gate)
    applied = True
finally:
    if temporary is not None:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    if gate_created and not applied:
        try:
            egress_gate.unlink()
        except FileNotFoundError:
            pass
    os.close(lock_fd)
""".strip()
        self._ensure_boundary()
        self._ssh(
            [
                "sudo",
                "python3",
                "-c",
                program,
                self.worker,
                run_id or "bootstrap",
                reset_sha256 or "-",
                policy_sha256,
                worker_identity_sha256(self.identity),
            ],
            input_bytes=batch.encode(),
        )

    def block_all(self, run_id: str) -> str:
        """Install the fail-closed boundary before reset or guest startup."""

        run_id = _safe_name(run_id, "run id")
        batch = build_block_all_batch(
            self.worker,
            run_id,
            host_bindings_sha256=self.identity.host_bindings_sha256.removeprefix("sha256:"),
            bridge_interface=self.bridge_interface,
        )
        policy = {
            "schema_version": EGRESS_SCHEMA,
            "run_id": run_id,
            "allowed_ipv4": [],
            "allowed_ipv6": [],
            "host_bindings_sha256": self.identity.host_bindings_sha256.removeprefix("sha256:"),
        }
        digest = egress_policy_sha256(policy)
        self._persist_and_apply(batch, digest)
        return digest

    def _create_reset_gate(self, proof: ResetProof) -> None:
        """Bind a completed reset proof to one remote, one-use gate."""

        program = r"""
set -euo pipefail
worker="$1"
run_id="$2"
container="$3"
proof_sha256="$4"
gate_root=/var/lib/openadapt/windows-run-gates
install -d -m 0700 "$gate_root"
exec 9>"/var/lock/openadapt-windows-${worker}.lock"
flock -n 9
test ! -e "$gate_root/${run_id}.reset"
test ! -e "$gate_root/${run_id}.reset-consumed"
test ! -e "$gate_root/${run_id}.egress"
test ! -e "$gate_root/${run_id}.consumed"
test -z "$(docker ps -aq --filter "name=^${container}$")"
reset_gate="$gate_root/${run_id}.reset"
mkdir -m 0700 "$reset_gate"
trap 'rm -rf -- "$reset_gate"' EXIT
printf '%s\n' "$proof_sha256" > "$reset_gate/proof.sha256"
chmod 0600 "$reset_gate/proof.sha256"
sync -f "$reset_gate/proof.sha256"
sync -f "$reset_gate"
trap - EXIT
""".strip()
        self._ssh(
            [
                "sudo",
                "bash",
                "-s",
                "--",
                self.worker,
                proof.run_id,
                self.container,
                reset_proof_sha256(proof),
            ],
            input_bytes=(program + "\n").encode(),
        )

    def reset(self, *, run_id: str) -> ResetProof:
        """Restore the exact baseline and leave the guest container stopped."""

        run_id = _safe_name(run_id, "run id")
        baseline_sha256 = self.identity.baseline_manifest_sha256.removeprefix("sha256:")
        self._verify_remote_identity()
        self.block_all(run_id)
        process = self._ssh(
            [
                "sudo",
                "bash",
                "-s",
                "--",
                self.worker,
                run_id,
                self.storage_dir,
                self.baseline_dir,
                baseline_sha256,
                self.container,
            ],
            input_bytes=(RESET_SCRIPT + "\n").encode(),
            timeout_seconds=15 * 60,
        )
        try:
            value = json.loads(process.stdout)
        except json.JSONDecodeError as exc:
            raise WindowsPoolIsolationError("reset proof is invalid") from exc
        expected_remote = {
            "schema_version": RESET_SCHEMA,
            "worker": self.worker,
            "run_id": run_id,
            "baseline_sha256": baseline_sha256,
            "container_state": "stopped",
        }
        if value != expected_remote:
            raise WindowsPoolIsolationError("reset proof differs from the request")
        issued = _now()
        proof = ResetProof(
            **expected_remote,
            worker_identity_sha256=worker_identity_sha256(self.identity),
            admitted_image_sha256=self.identity.admitted_image_sha256,
            admitted_runtime_sha256=self.identity.admitted_runtime_sha256,
            host_bindings_sha256=self.identity.host_bindings_sha256,
            tls_bindings_sha256=self.identity.tls_bindings_sha256,
            issued_at=_format_timestamp(issued),
            expires_at=_format_timestamp(issued + MAX_GATE_AGE),
        )
        self._create_reset_gate(proof)
        return proof

    def apply_egress(
        self,
        policy: Mapping[str, Any],
        *,
        reset_proof: ResetProof,
    ) -> EgressProof:
        """Atomically replace block-all with one exact IP allow-list."""

        parsed = validate_egress_policy(policy)
        run_id = parsed["run_id"]
        validate_reset_proof(
            reset_proof,
            worker=self.worker,
            run_id=run_id,
            identity=self.identity,
        )
        if parsed["host_bindings_sha256"] != self.identity.host_bindings_sha256.removeprefix(
            "sha256:"
        ):
            raise WindowsPoolIsolationError("egress host bindings differ from worker identity")
        self._verify_remote_identity()
        batch = build_nft_batch(
            self.worker,
            parsed,
            bridge_interface=self.bridge_interface,
        )
        digest = egress_policy_sha256(parsed)
        self._persist_and_apply(
            batch,
            digest,
            run_id=run_id,
            reset_sha256=reset_proof_sha256(reset_proof),
        )
        issued = _now()
        return EgressProof(
            schema_version=EGRESS_PROOF_SCHEMA,
            worker=self.worker,
            run_id=run_id,
            policy_sha256=digest,
            reset_proof_sha256=reset_proof_sha256(reset_proof),
            worker_identity_sha256=worker_identity_sha256(self.identity),
            host_bindings_sha256=self.identity.host_bindings_sha256,
            tls_bindings_sha256=self.identity.tls_bindings_sha256,
            conntrack_drained=True,
            issued_at=_format_timestamp(issued),
            expires_at=_format_timestamp(issued + MAX_GATE_AGE),
        )

    def start(
        self,
        *,
        policy: Mapping[str, Any],
        reset_proof: ResetProof,
        egress_proof: EgressProof,
        start_script: str,
    ) -> StartProof:
        """Consume one fresh reset/egress chain before guest startup."""

        parsed = validate_egress_policy(policy)
        run_id = parsed["run_id"]
        now = _now()
        validate_reset_proof(
            reset_proof,
            worker=self.worker,
            run_id=run_id,
            identity=self.identity,
            now=now,
        )
        validate_egress_proof(
            egress_proof,
            worker=self.worker,
            run_id=run_id,
            policy=parsed,
            reset_proof=reset_proof,
            identity=self.identity,
            now=now,
        )
        self._verify_remote_identity()
        gate_script = r"""
set -euo pipefail
run_id="$1"
policy_sha256="$2"
reset_sha256="$3"
identity_sha256="$4"
image_sha256="$5"
worker="$6"
gate_root=/var/lib/openadapt/windows-run-gates
install -d -m 0700 "$gate_root"
exec 9>"/var/lock/openadapt-windows-${worker}.lock"
flock -n 9
test -f "$gate_root/${run_id}.egress"
expected_gate="$(printf '%s\n%s\n%s' "$policy_sha256" "$reset_sha256" "$identity_sha256")"
test "$(cat "$gate_root/${run_id}.egress")" = "$expected_gate"
mkdir -m 0700 "$gate_root/${run_id}.consumed"
rm "$gate_root/${run_id}.egress"
test "$(tr -d '\n' < /run/openadapt/windows-egress.sha256)" = "$policy_sha256"
test "$(docker image inspect "$image_sha256" --format '{{.Id}}')" = "$image_sha256"
test -z "$(docker ps -aq --filter name='^winarena$')"
""".strip()
        self._ssh(
            [
                "sudo",
                "bash",
                "-s",
                "--",
                run_id,
                egress_proof.policy_sha256,
                reset_proof_sha256(reset_proof),
                worker_identity_sha256(self.identity),
                self.identity.admitted_image_sha256,
                self.worker,
            ],
            input_bytes=(gate_script + "\n" + start_script + "\n").encode(),
            timeout_seconds=15 * 60,
        )
        return StartProof(
            schema_version=START_PROOF_SCHEMA,
            worker=self.worker,
            run_id=run_id,
            worker_identity_sha256=worker_identity_sha256(self.identity),
            reset_proof_sha256=reset_proof_sha256(reset_proof),
            egress_policy_sha256=egress_proof.policy_sha256,
            one_use_consumed=True,
            started_at=_format_timestamp(now),
        )

    def verify_started(self, *, run_id: str, policy_sha256: str) -> None:
        """Revalidate one admitted guest and its consumed start gate."""

        run_id = _safe_name(run_id, "run id")
        if not isinstance(policy_sha256, str) or HEX64.fullmatch(policy_sha256) is None:
            raise WindowsPoolIsolationError("egress policy digest is invalid")
        self._verify_remote_identity()
        program = r"""
set -euo pipefail
run_id="$1"
policy_sha256="$2"
container="$3"
image_sha256="$4"
gate_root=/var/lib/openadapt/windows-run-gates
test -d "$gate_root/${run_id}.consumed"
test ! -L "$gate_root/${run_id}.consumed"
test -d "$gate_root/${run_id}.reset-consumed"
test ! -e "$gate_root/${run_id}.reset"
test ! -e "$gate_root/${run_id}.egress"
test -f /run/openadapt/windows-egress.sha256
test ! -L /run/openadapt/windows-egress.sha256
test "$(tr -d '\n' < /run/openadapt/windows-egress.sha256)" = "$policy_sha256"
test "$(docker inspect "$container" --format '{{.State.Running}}')" = true
test "$(docker inspect "$container" --format '{{.Image}}')" = "$image_sha256"
""".strip()
        self._ssh(
            [
                "sudo",
                "bash",
                "-s",
                "--",
                run_id,
                policy_sha256,
                self.container,
                self.identity.admitted_image_sha256,
            ],
            input_bytes=(program + "\n").encode(),
        )

    def consume_dispatch(
        self,
        *,
        run_id: str,
        policy_sha256: str,
        task_binding_sha256: str,
    ) -> None:
        """Consume one remote dispatch gate after a fresh start revalidation."""

        if (
            not isinstance(task_binding_sha256, str)
            or SHA256.fullmatch(task_binding_sha256) is None
        ):
            raise WindowsPoolIsolationError("task binding digest is invalid")
        self.verify_started(run_id=run_id, policy_sha256=policy_sha256)
        program = r"""
set -euo pipefail
run_id="$1"
task_binding_sha256="$2"
worker="$3"
gate_root=/var/lib/openadapt/windows-run-gates
exec 9>"/var/lock/openadapt-windows-${worker}.lock"
flock -n 9
test -d "$gate_root/${run_id}.consumed"
dispatch_gate="$gate_root/${run_id}.dispatch"
mkdir -m 0700 "$dispatch_gate"
printf '%s\n' "$task_binding_sha256" > "$dispatch_gate/task-binding.sha256"
chmod 0400 "$dispatch_gate/task-binding.sha256"
sync -f "$dispatch_gate/task-binding.sha256"
sync -f "$dispatch_gate"
""".strip()
        self._ssh(
            ["sudo", "bash", "-s", "--", run_id, task_binding_sha256, self.worker],
            input_bytes=(program + "\n").encode(),
        )

    def run_command(
        self,
        remote_command: Sequence[str],
        *,
        input_bytes: bytes | None = None,
        timeout_seconds: int = 300,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a command through the pinned identity boundary."""

        self._verify_remote_identity()
        return self._ssh(
            remote_command,
            input_bytes=input_bytes,
            timeout_seconds=timeout_seconds,
        )

    def run_qualified_command(
        self,
        remote_command: Sequence[str],
        *,
        run_id: str,
        policy_sha256: str,
        input_bytes: bytes | None = None,
        timeout_seconds: int = 300,
    ) -> subprocess.CompletedProcess[bytes]:
        """Run a command only after a fresh live start-gate check."""

        self.verify_started(run_id=run_id, policy_sha256=policy_sha256)
        return self._ssh(
            remote_command,
            input_bytes=input_bytes,
            timeout_seconds=timeout_seconds,
        )

    def probe_services(self, *, run_id: str, policy_sha256: str) -> tuple[bool, bool]:
        """Probe WAA and the evaluator after an exact live gate check."""

        self.verify_started(run_id=run_id, policy_sha256=policy_sha256)
        waa = self.run_qualified_command(
            ["curl", "-fsS", "--max-time", "5", "http://localhost:5000/probe"],
            run_id=run_id,
            policy_sha256=policy_sha256,
            timeout_seconds=15,
        )
        try:
            self.run_qualified_command(
                ["curl", "-fsS", "--max-time", "5", "http://localhost:5051/probe"],
                run_id=run_id,
                policy_sha256=policy_sha256,
                timeout_seconds=15,
            )
            evaluator_ready = True
        except WindowsPoolIsolationError:
            evaluator_ready = False
        return bool(waa.stdout.strip()), evaluator_ready


def dispatch_binding_sha256(value: Mapping[str, Any]) -> str:
    """Hash one closed task dispatch projection."""

    expected = {
        "schema_version",
        "worker",
        "qualified_run_id",
        "qualified_start_proof_sha256",
        "task_id",
        "agent",
        "model",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise WindowsPoolIsolationError("dispatch binding is not a closed object")
    for field in expected:
        item = value[field]
        if not isinstance(item, str) or not item or len(item) > 512 or "\x00" in item:
            raise WindowsPoolIsolationError(f"dispatch binding {field} is invalid")
    if value["schema_version"] != "openadapt.windows-qualified-dispatch/v1":
        raise WindowsPoolIsolationError("dispatch binding schema is invalid")
    if SHA256.fullmatch(value["qualified_start_proof_sha256"]) is None:
        raise WindowsPoolIsolationError("dispatch start proof digest is invalid")
    return "sha256:" + hashlib.sha256(
        DISPATCH_BINDING_DOMAIN + canonical_json(dict(value))
    ).hexdigest()
