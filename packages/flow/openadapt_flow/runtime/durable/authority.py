"""External monotonic authority for durable run and continuation state.

The run directory is evidence, not the only authority. A backup can restore
every file in that directory to an older approved pause. This registry lives
outside the run directory and records monotonic delivery/terminal state, so a
same-path run-directory restore cannot repeat a consequential continuation.

The authority store is a deployment trust boundary. Its storage and backups
must not be restored with the run directory or modified by an untrusted
principal. This module protects that trusted store from concurrent writers,
active path replacement, and accidental reuse. No local file format can remain
monotonic after a principal with write access replaces the complete authority
store. Customer-controlled deployments must therefore keep this store on the
runner's protected service volume and restrict its backup and restore policy.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import stat
from base64 import b64decode, b64encode
from binascii import Error as BinasciiError
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Literal, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from pydantic import BaseModel, ConfigDict

from openadapt_flow.runtime.durable.approval import (
    StateDiverged,
    approval_pause_digest,
)
from openadapt_flow.terminal_verification_v2 import (
    MAX_DELIVERY_ARTIFACT_BYTES,
    ProductionDeliveryPermit,
    ProductionDeliveryPermitArtifact,
    ProductionDeliveryPermitChain,
    ProductionDeliveryReceiptArtifact,
    ProductionPendingDeliveryPermit,
)

AUTHORITY_DB_ENV = "OPENADAPT_DURABLE_AUTHORITY_DB"
REMOTE_AUTHORITY_URL_ENV = "OPENADAPT_DURABLE_AUTHORITY_URL"
# The managed parent injects a run-scoped delivery-authority credential. Keep
# the established environment name for child-runtime compatibility.
REMOTE_AUTHORITY_TOKEN_ENV = "OPENADAPT_RUNNER_TOKEN"
REMOTE_DISPATCH_SESSION_ID_ENV = "OPENADAPT_DURABLE_DISPATCH_SESSION_ID"
# This observer exists only for the closed synthetic Execute acceptance run.
# A Modal launcher owns the fixed, pre-opened non-blocking pipe at descriptor
# three. A bundle, CLI invocation, or remote caller cannot select a path,
# endpoint, or file descriptor. A separate consumer reads and relays markers.
SYNTHETIC_DELIVERY_MARKER_ENABLED_ENV = "OPENADAPT_SYNTHETIC_DELIVERY_MARKER_ENABLED"
SYNTHETIC_DELIVERY_MARKER_RUN_ID_ENV = "OPENADAPT_SYNTHETIC_DELIVERY_MARKER_RUN_ID"
_SYNTHETIC_DELIVERY_MARKER_FD = 3
_SYNTHETIC_DELIVERY_MARKER_SCHEMA_VERSION = 1
_SYNTHETIC_DELIVERY_MARKER_DOMAIN = b"openadapt-synthetic-delivery-marker-permit-v1\0"
_MAX_SYNTHETIC_DELIVERY_MARKER_BYTES = 1024
MAX_REMOTE_AUTHORITY_RESPONSE_BYTES = 2 * 1024 * 1024
AUTHORITY_SCHEMA_VERSION = 1
JOURNAL_GENESIS_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()
JOURNAL_MAC_DOMAIN = b"openadapt-attended-journal-v1\0"
_REMOTE_UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}"
)
_REMOTE_AUTHORITY_ID_RE = _REMOTE_UUID_RE
_REMOTE_TOKEN_RE = re.compile(r"[a-f0-9]{32}")
_REMOTE_PATH_KEY_RE = re.compile(r"[a-f0-9]{64}")
_REMOTE_DIGEST_RE = re.compile(r"sha256:[a-f0-9]{64}")
_REMOTE_PERMIT_CURSOR_RE = re.compile(r"[a-f0-9]{64}")
_REMOTE_OPERATIONS = {
    "initial",
    "resume",
    "continue",
    "skip",
    "reject",
    "teach",
    "escalate",
}


class _RefuseRemoteAuthorityRedirects(HTTPRedirectHandler):
    """Keep the runner credential on the configured authority origin."""

    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        raise HTTPError(
            req.full_url,
            code,
            "remote delivery authority redirects are refused",
            headers,
            fp,
        )


def _is_windows() -> bool:
    return os.name == "nt"


def _windows_kernel32() -> Any:
    """Return a typed Win32 file API for active authority path pinning."""

    import ctypes
    from ctypes import wintypes

    win_dll = getattr(ctypes, "WinDLL", None)
    if win_dll is None:
        raise DurableAuthorityBusy("the Windows authority API is unavailable")
    kernel32 = win_dll("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    return kernel32


def _windows_handle_attributes(kernel32: Any, handle: int) -> int:
    """Read attributes from the exact open Win32 handle."""

    import ctypes
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("reparse_tag", wintypes.DWORD),
        ]

    info = FileAttributeTagInfo()
    # FILE_INFO_BY_HANDLE_CLASS.FileAttributeTagInfo
    if not kernel32.GetFileInformationByHandleEx(
        handle,
        9,
        ctypes.byref(info),
        ctypes.sizeof(info),
    ):
        raise DurableAuthorityBusy(
            "the external durable authority cannot inspect its Windows path"
        )
    return int(info.file_attributes)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _default_db_path() -> Path:
    configured = os.getenv(AUTHORITY_DB_ENV)
    if configured:
        # Keep the final path lexical. Resolving it here would follow an
        # attacker-controlled database symlink before `_connect` can reject it.
        return Path(os.path.abspath(os.fspath(Path(configured).expanduser())))
    return Path.home() / ".openadapt" / "durable-authority" / "authority.sqlite3"


def _path_key(run_dir: Path) -> str:
    canonical = str(run_dir.resolve()).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _validate_projection_failure_report(
    corrected_report_json: bytes,
    manifest: Any,
) -> None:
    """Accept only the one typed terminal downgrade this authority permits."""

    try:
        from openadapt_flow.ir import RunReport

        report = RunReport.model_validate_json(corrected_report_json)
    except ValueError as exc:
        raise DurableAuthorityBusy("the corrected terminal report is invalid") from exc
    expected_run_id_sha256 = hashlib.sha256(manifest.run_id.encode("utf-8")).hexdigest()
    if not (
        report.success is False
        and report.execution_outcome == "FAILED"
        and report.transaction_outcome == "RECONCILIATION_REQUIRED"
        and report.run_id_sha256 == expected_run_id_sha256
        and bool(report.idempotency_key)
        and report.results
        and report.results[-1].step_id == "<idempotency>"
        and report.results[-1].intent == "persist terminal idempotency outcome"
        and report.results[-1].failure_category == "runtime_failure"
        and report.results[-1].ok is False
        and (report.results[-1].error or "").startswith(
            "terminal idempotency outcome persistence failed "
        )
    ):
        raise DurableAuthorityBusy(
            "the corrected terminal report is not the required "
            "idempotency-projection failure form"
        )


class DurableAuthorityBusy(StateDiverged):
    """A different process or uncertain delivery owns the durable authority."""


@dataclass(frozen=True)
class _RemoteDeliveryPermit:
    """Exact pending Cloud authority for one not-yet-acknowledged input edge.

    The cursor, session, and one-use claim are private authority values. The
    signed permit bytes are retained exactly so the post-delivery receipt can
    bind the same artifact and terminal verification can rebuild the chain.
    """

    authority_id: str
    authority_origin: str
    next_sequence: int
    cursor_secret: str
    permit_digest: str
    authority_signer_sha256: str
    permit_id: str
    dispatch_session_id: str
    one_use_claim_id: str
    input_edge_sequence: int
    authority_sequence: int
    runtime_delivery_sequence: int
    permit_artifact_bytes: bytes
    permit_artifact: ProductionDeliveryPermitArtifact


def _fixed_synthetic_delivery_marker_sink() -> Callable[[bytes], None] | None:
    """Get the Modal-owned non-blocking observer pipe when it is enabled.

    The sink is best-effort. It never raises and does not make a delivery,
    permit, or durable transaction fail. The acceptance consumer must reject a
    missing or malformed marker separately.
    """

    if os.getenv(SYNTHETIC_DELIVERY_MARKER_ENABLED_ENV, "") != "1":
        return None
    try:
        mode = os.fstat(_SYNTHETIC_DELIVERY_MARKER_FD).st_mode
        if not (stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
            return None
        os.set_blocking(_SYNTHETIC_DELIVERY_MARKER_FD, False)
    except OSError:
        return None

    def sink(payload: bytes) -> None:
        try:
            # The payload is bounded well below PIPE_BUF. A short or would-block
            # write is intentionally dropped. The consumer owns durable I/O.
            os.write(_SYNTHETIC_DELIVERY_MARKER_FD, payload)
        except OSError:
            pass

    return sink


class DurableAuthorityRecord(BaseModel):
    """PHI-free monotonic authority for one canonical run path."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    path_key: str
    namespace_id: str
    run_id: str
    revision: int
    phase: Literal[
        "claimed",
        "active",
        "paused",
        "continuing",
        "reconciliation_required",
        "terminal_prepared",
        "completed",
        "rejected",
    ]
    progress_digest: str
    pause_binding_sha256: str = ""
    approval_digest: str = ""
    report_sha256: str = ""
    # A completed continuation report is not externally attestable until its
    # terminal idempotency projection either settles or records one bounded
    # fail-closed correction.  This closes the crash window between durable
    # completion and the ledger update.
    terminal_projection_state: Literal["none", "pending", "settled", "corrected"] = (
        "none"
    )
    terminal_original_report_sha256: str = ""
    terminal_correction_reason: str = ""
    attempt_id: str = ""
    operation: str = ""
    owner_nonce_sha256: str = ""
    attempt_phase: Literal[
        "none",
        "validating",
        "delivery_started",
        "reconciliation_required",
        "terminal_prepared",
    ] = "none"
    reject_requested: bool = False
    delivery_sequence: int = 0
    acquired_at: str = ""
    expires_at: str = ""
    journal_sequence: int = 0
    journal_head_digest: str = JOURNAL_GENESIS_DIGEST
    updated_at: str


class DurableAuthority:
    """SQLite-backed authority on one trusted, non-rollback service volume."""

    def __init__(
        self,
        run_dir: Path | str,
        store: Any,
        *,
        remote_transport: Callable[[str, dict[str, str], bytes], bytes] | None = None,
    ) -> None:
        self.run_dir = Path(run_dir).resolve()
        self.store = store
        self._configured_db_path = _default_db_path()
        # Tests can inject an in-memory transport. Production always uses HTTPS.
        self._remote_transport = remote_transport
        # Marker delivery is observation-only and uses a fixed inherited pipe.
        # It is not a runtime output path and has no effect on actuation.
        self._synthetic_delivery_marker_sink = _fixed_synthetic_delivery_marker_sink()
        self._assert_configured_ancestors_are_not_links()
        try:
            # Resolve the parent only. This catches an ancestor symlink that
            # redirects the database back into the rollback-capable run tree,
            # while preserving the final lexical path so `_connect` can reject
            # a database-file symlink without following it.
            effective_db_path = (
                self._configured_db_path.parent.resolve()
                / self._configured_db_path.name
            )
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable authority path is unavailable"
            ) from exc
        if (
            effective_db_path == self.run_dir
            or self.run_dir in effective_db_path.parents
        ):
            raise DurableAuthorityBusy(
                "the external durable authority must be outside the run directory"
            )
        # Retain the resolved parent selected during construction. The final
        # component stays lexical so `_connect` can still reject a database-file
        # symlink without following it.
        self.db_path = effective_db_path
        self._authority_parent_descriptor = self._prepare_authority_parent()
        self.path_key = _path_key(self.run_dir)

    def _assert_configured_ancestors_are_not_links(self) -> None:
        """Reject a lexical authority path that crosses a link or junction."""

        parent = self._configured_db_path.parent.absolute()
        current = Path(parent.anchor)
        for component in parent.parts[1:]:
            current /= component
            try:
                status = current.lstat()
            except FileNotFoundError:
                break
            if stat.S_ISLNK(status.st_mode) or bool(
                getattr(status, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise DurableAuthorityBusy(
                    "the external durable authority path must not traverse a link"
                )

    def __del__(self) -> None:
        """Release the retained POSIX directory descriptor."""

        descriptor = getattr(self, "_authority_parent_descriptor", None)
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass

    def _prepare_authority_parent(self) -> Optional[int]:
        """Retain the authority parent used for irreversible delivery fences.

        ``sqlite3`` only accepts a pathname, so it cannot open the database
        relative to this descriptor.  Delivery therefore also consumes a
        descriptor-relative exclusive claim.  A restored SQLite projection
        cannot recreate a consumed claim, even if the database pathname is
        replaced between its checks and SQLite's open.
        """

        parent_existed = self.db_path.parent.exists()
        self.db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            parent_stat = os.lstat(self.db_path.parent)
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable authority directory is unavailable"
            ) from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise DurableAuthorityBusy(
                "the external durable authority parent must be a directory"
            )
        if not parent_existed and os.name != "nt":
            os.chmod(self.db_path.parent, 0o700)
        if os.name == "nt":
            return None
        flags = (
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        try:
            descriptor = os.open(self.db_path.parent, flags)
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable authority directory is unavailable"
            ) from exc
        try:
            opened = os.fstat(descriptor)
            named = os.stat(self.db_path.parent, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                raise DurableAuthorityBusy(
                    "the external durable authority ancestor changed after admission"
                )
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _consume_delivery_fence(
        self, record: DurableAuthorityRecord, manifest: Any
    ) -> None:
        """Consume one non-reusable delivery sequence before an input edge."""

        if self._authority_parent_descriptor is None:
            # Windows pins the complete authority path with delete-denying
            # handles for the full SQLite transaction.
            return
        # ``delivery_sequence`` is scoped to one continuation attempt and is
        # reset when a later pause is approved.  The record revision is the
        # monotonic delivery identity across all attempts for this run.
        authority_revision = record.revision
        name = f".delivery-fence-{self.path_key}-{authority_revision}"
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(
                name,
                flags,
                0o600,
                dir_fd=self._authority_parent_descriptor,
            )
        except FileExistsError as exc:
            raise DurableAuthorityBusy(
                "the external durable delivery fence was already consumed"
            ) from exc
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable delivery fence is unavailable"
            ) from exc
        try:
            payload = self._canonical(
                {
                    "path_key": self.path_key,
                    "namespace_id": manifest.namespace_id,
                    "run_id": manifest.run_id,
                    "authority_revision": authority_revision,
                }
            )
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.fsync(self._authority_parent_descriptor)
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable delivery fence is unavailable"
            ) from exc

    @contextmanager
    def _windows_active_race_handles(self) -> Iterator[None]:
        """Hold Windows ancestors and the DB without delete sharing.

        SQLite still opens by pathname on Windows.  These handles deny delete
        sharing from the admission check until that SQLite connection closes,
        so neither an ancestor nor the final database can be renamed or
        replaced in that interval.
        """

        if not _is_windows():
            yield
            return
        import ctypes

        kernel32 = _windows_kernel32()
        invalid_handle = ctypes.c_void_p(-1).value
        file_read_attributes = 0x00000080
        generic_read = 0x80000000
        generic_write = 0x40000000
        share_read_write = 0x00000003
        open_existing = 3
        open_always = 4
        file_flag_backup_semantics = 0x02000000
        file_flag_open_reparse_point = 0x00200000
        file_attribute_directory = 0x00000010
        file_attribute_reparse_point = 0x00000400
        handles: list[int] = []

        def open_handle(
            path: Path,
            creation: int,
            flags: int,
            access: int,
            *,
            expect_directory: bool,
        ) -> int:
            handle = kernel32.CreateFileW(
                str(path),
                access,
                share_read_write,
                None,
                creation,
                flags,
                None,
            )
            raw_handle = getattr(handle, "value", handle)
            if raw_handle is None or int(raw_handle) == invalid_handle:
                raise DurableAuthorityBusy(
                    "the external durable authority cannot pin its Windows path"
                )
            opened_handle = int(raw_handle)
            attributes = _windows_handle_attributes(kernel32, opened_handle)
            if attributes & file_attribute_reparse_point:
                kernel32.CloseHandle(opened_handle)
                raise DurableAuthorityBusy(
                    "the external durable authority Windows path must not "
                    "traverse a reparse point"
                )
            is_directory = bool(attributes & file_attribute_directory)
            if is_directory != expect_directory:
                kernel32.CloseHandle(opened_handle)
                raise DurableAuthorityBusy(
                    "the external durable authority Windows path has the wrong type"
                )
            return opened_handle

        try:
            current = Path(self.db_path.parent.anchor)
            handles.append(
                open_handle(
                    current,
                    open_existing,
                    file_flag_backup_semantics | file_flag_open_reparse_point,
                    file_read_attributes,
                    expect_directory=True,
                )
            )
            for component in self.db_path.parent.parts[1:]:
                current /= component
                handles.append(
                    open_handle(
                        current,
                        open_existing,
                        file_flag_backup_semantics | file_flag_open_reparse_point,
                        file_read_attributes,
                        expect_directory=True,
                    )
                )
            handles.append(
                open_handle(
                    self.db_path,
                    open_always,
                    file_flag_open_reparse_point,
                    generic_read | generic_write,
                    expect_directory=False,
                )
            )
            yield
        finally:
            for handle in reversed(handles):
                kernel32.CloseHandle(handle)

    def _connect(self) -> sqlite3.Connection:
        self._assert_configured_ancestors_are_not_links()
        try:
            current_db_path = (
                self._configured_db_path.parent.resolve()
                / self._configured_db_path.name
            )
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable authority path is unavailable"
            ) from exc
        if current_db_path != self.db_path:
            raise DurableAuthorityBusy(
                "the external durable authority ancestor changed after admission"
            )
        try:
            parent_stat = os.lstat(self.db_path.parent)
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external durable authority directory is unavailable"
            ) from exc
        if not stat.S_ISDIR(parent_stat.st_mode):
            raise DurableAuthorityBusy(
                "the external durable authority parent must be a directory"
            )
        if self.db_path.is_symlink():
            raise DurableAuthorityBusy(
                "the external durable authority database must not be a symlink"
            )
        if self.db_path.exists():
            try:
                db_stat = os.lstat(self.db_path)
            except OSError as exc:
                raise DurableAuthorityBusy(
                    "the external durable authority database is unavailable"
                ) from exc
            if not stat.S_ISREG(db_stat.st_mode):
                raise DurableAuthorityBusy(
                    "the external durable authority database must be a regular file"
                )
            if os.name != "nt" and db_stat.st_mode & 0o077:
                raise DurableAuthorityBusy(
                    "the external durable authority database permissions are too broad"
                )
        connection: Optional[sqlite3.Connection] = None
        try:
            connection = sqlite3.connect(
                self.db_path.as_uri() + "?mode=rwc&nofollow=1",
                timeout=15.0,
                isolation_level=None,
                uri=True,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA busy_timeout=15000")
            connection.execute(
                """
            CREATE TABLE IF NOT EXISTS durable_authority (
                path_key TEXT PRIMARY KEY,
                namespace_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                schema_version INTEGER NOT NULL,
                revision INTEGER NOT NULL,
                phase TEXT NOT NULL,
                progress_digest TEXT NOT NULL,
                pause_binding_sha256 TEXT NOT NULL,
                approval_digest TEXT NOT NULL,
                report_sha256 TEXT NOT NULL,
                terminal_projection_state TEXT NOT NULL DEFAULT 'none',
                terminal_original_report_sha256 TEXT NOT NULL DEFAULT '',
                terminal_correction_reason TEXT NOT NULL DEFAULT '',
                attempt_id TEXT NOT NULL,
                operation TEXT NOT NULL,
                owner_nonce_sha256 TEXT NOT NULL,
                attempt_phase TEXT NOT NULL,
                reject_requested INTEGER NOT NULL,
                delivery_sequence INTEGER NOT NULL,
                acquired_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                journal_sequence INTEGER NOT NULL DEFAULT 0,
                journal_head_digest TEXT NOT NULL DEFAULT
                    'sha256:e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855',
                updated_at TEXT NOT NULL
            )
            """
            )
            columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(durable_authority)"
                ).fetchall()
            }
            old_columns = {
                "path_key",
                "namespace_id",
                "run_id",
                "schema_version",
                "revision",
                "phase",
                "progress_digest",
                "pause_binding_sha256",
                "approval_digest",
                "report_sha256",
                "attempt_id",
                "operation",
                "owner_nonce_sha256",
                "attempt_phase",
                "reject_requested",
                "delivery_sequence",
                "acquired_at",
                "expires_at",
                "updated_at",
            }
            expected_columns = old_columns | {"journal_sequence", "journal_head_digest"}
            if columns == old_columns:
                connection.execute(
                    "ALTER TABLE durable_authority ADD COLUMN "
                    "journal_sequence INTEGER NOT NULL DEFAULT 0"
                )
                connection.execute(
                    "ALTER TABLE durable_authority ADD COLUMN "
                    "journal_head_digest TEXT NOT NULL DEFAULT "
                    f"'{JOURNAL_GENESIS_DIGEST}'"
                )
                columns = expected_columns
            correction_columns = {
                "terminal_projection_state",
                "terminal_original_report_sha256",
                "terminal_correction_reason",
            }
            corrected_expected_columns = expected_columns | correction_columns
            if columns == expected_columns:
                connection.execute(
                    "ALTER TABLE durable_authority ADD COLUMN "
                    "terminal_projection_state TEXT NOT NULL DEFAULT 'none'"
                )
                connection.execute(
                    "ALTER TABLE durable_authority ADD COLUMN "
                    "terminal_original_report_sha256 TEXT NOT NULL DEFAULT ''"
                )
                connection.execute(
                    "ALTER TABLE durable_authority ADD COLUMN "
                    "terminal_correction_reason TEXT NOT NULL DEFAULT ''"
                )
                columns = corrected_expected_columns
            if columns != corrected_expected_columns:
                raise DurableAuthorityBusy(
                    "the external durable authority schema is incompatible"
                )
            # Keep the bearer-like remote cursor outside the generic authority
            # record.  Record snapshots and the attended journal can then never
            # serialize it into an evidence artifact.
            connection.execute(
                """
            CREATE TABLE IF NOT EXISTS remote_delivery_cursor (
                path_key TEXT PRIMARY KEY,
                authority_id TEXT NOT NULL,
                next_sequence INTEGER NOT NULL,
                cursor_secret TEXT NOT NULL
            )
            """
            )
            remote_cursor_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(remote_delivery_cursor)"
                ).fetchall()
            }
            if remote_cursor_columns != {
                "path_key",
                "authority_id",
                "next_sequence",
                "cursor_secret",
            }:
                raise DurableAuthorityBusy(
                    "the remote delivery cursor schema is incompatible"
                )
            # A permit is durable before the backend call. It remains pending
            # until the exact post-delivery receipt is acknowledged. A crash or
            # network failure can therefore never permit a second input edge.
            connection.execute(
                """
            CREATE TABLE IF NOT EXISTS remote_delivery_pending (
                path_key TEXT PRIMARY KEY,
                authority_id TEXT NOT NULL,
                authority_origin TEXT NOT NULL,
                authority_signer_sha256 TEXT NOT NULL,
                permit_id TEXT NOT NULL,
                dispatch_session_id TEXT NOT NULL,
                one_use_claim_id TEXT NOT NULL,
                next_sequence INTEGER NOT NULL,
                cursor_secret TEXT NOT NULL,
                input_edge_sequence INTEGER NOT NULL,
                authority_sequence INTEGER NOT NULL,
                runtime_delivery_sequence INTEGER NOT NULL,
                permit_artifact_sha256 TEXT NOT NULL,
                permit_artifact_bytes BLOB NOT NULL
            )
            """
            )
            pending_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(remote_delivery_pending)"
                ).fetchall()
            }
            expected_pending_columns = {
                "path_key",
                "authority_id",
                "authority_origin",
                "authority_signer_sha256",
                "permit_id",
                "dispatch_session_id",
                "one_use_claim_id",
                "next_sequence",
                "cursor_secret",
                "input_edge_sequence",
                "authority_sequence",
                "runtime_delivery_sequence",
                "permit_artifact_sha256",
                "permit_artifact_bytes",
            }
            legacy_pending_columns = expected_pending_columns - {"authority_origin"}
            if pending_columns == legacy_pending_columns:
                pending_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM remote_delivery_pending"
                    ).fetchone()[0]
                )
                if pending_count:
                    raise DurableAuthorityBusy(
                        "the legacy pending remote delivery cannot be migrated"
                    )
                connection.execute(
                    "ALTER TABLE remote_delivery_pending ADD COLUMN "
                    "authority_origin TEXT NOT NULL DEFAULT ''"
                )
                pending_columns = expected_pending_columns
            if pending_columns != expected_pending_columns:
                raise DurableAuthorityBusy(
                    "the pending remote delivery schema is incompatible"
                )
            connection.execute(
                """
            CREATE TABLE IF NOT EXISTS remote_delivery_artifacts (
                path_key TEXT NOT NULL,
                runtime_delivery_sequence INTEGER NOT NULL,
                permit_artifact_sha256 TEXT NOT NULL,
                permit_artifact_bytes BLOB NOT NULL,
                receipt_artifact_sha256 TEXT NOT NULL,
                receipt_artifact_bytes BLOB NOT NULL,
                PRIMARY KEY (path_key, runtime_delivery_sequence),
                UNIQUE (path_key, permit_artifact_sha256),
                UNIQUE (path_key, receipt_artifact_sha256)
            )
            """
            )
            artifact_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(remote_delivery_artifacts)"
                ).fetchall()
            }
            if artifact_columns != {
                "path_key",
                "runtime_delivery_sequence",
                "permit_artifact_sha256",
                "permit_artifact_bytes",
                "receipt_artifact_sha256",
                "receipt_artifact_bytes",
            }:
                raise DurableAuthorityBusy(
                    "the retained remote delivery artifact schema is incompatible"
                )
            connection.execute(
                """
            CREATE TABLE IF NOT EXISTS attended_journal (
                path_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                run_id TEXT NOT NULL,
                previous_record_digest TEXT NOT NULL,
                snapshot_digest TEXT NOT NULL,
                snapshot_json TEXT NOT NULL,
                record_digest TEXT NOT NULL,
                record_mac TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (path_key, sequence),
                UNIQUE (path_key, record_digest)
            )
            """
            )
            journal_columns = {
                str(row[1])
                for row in connection.execute(
                    "PRAGMA table_info(attended_journal)"
                ).fetchall()
            }
            if journal_columns != {
                "path_key",
                "sequence",
                "run_id",
                "previous_record_digest",
                "snapshot_digest",
                "snapshot_json",
                "record_digest",
                "record_mac",
                "created_at",
            }:
                raise DurableAuthorityBusy(
                    "the external attended-journal schema is incompatible"
                )
            if self.db_path.exists() and os.name != "nt":
                os.chmod(self.db_path, 0o600)
            return connection
        except Exception:
            if connection is not None:
                connection.close()
            raise

    def _journal_key(self, *, create: bool) -> bytes:
        """Return the authority-owned HMAC key outside the run directory."""

        key_path = self.db_path.with_name(self.db_path.name + ".journal-key")
        descriptor: Optional[int] = None
        try:
            descriptor = os.open(
                key_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            if not create:
                raise DurableAuthorityBusy(
                    "the external attended-journal key is missing"
                ) from None
            key = secrets.token_bytes(32)
            try:
                descriptor = os.open(
                    key_path,
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                    0o600,
                )
            except FileExistsError:
                return self._journal_key(create=False)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(key)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                directory = os.open(key_path.parent, os.O_RDONLY)
            except OSError:
                directory = -1
            if directory >= 0:
                try:
                    os.fsync(directory)
                except OSError:
                    pass
                finally:
                    os.close(directory)
            descriptor = os.open(
                key_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except OSError as exc:
            raise DurableAuthorityBusy(
                "the external attended-journal key is unavailable"
            ) from exc
        assert descriptor is not None
        try:
            key_stat = os.fstat(descriptor)
            key = os.read(descriptor, 33)
        finally:
            os.close(descriptor)
        if not stat.S_ISREG(key_stat.st_mode):
            raise DurableAuthorityBusy(
                "the external attended-journal key must be a regular file"
            )
        if len(key) != 32:
            raise DurableAuthorityBusy("the external attended-journal key is invalid")
        if os.name != "nt":
            try:
                if key_stat.st_mode & 0o077:
                    raise DurableAuthorityBusy(
                        "the external attended-journal key permissions are too broad"
                    )
            except OSError as exc:
                raise DurableAuthorityBusy(
                    "the external attended-journal key is unavailable"
                ) from exc
        return key

    @staticmethod
    def _journal_unsigned(
        *,
        path_key: str,
        sequence: int,
        run_id: str,
        previous_record_digest: str,
        snapshot_digest: str,
        snapshot_json: str,
        created_at: str,
    ) -> dict[str, Any]:
        return {
            "path_key": path_key,
            "sequence": sequence,
            "run_id": run_id,
            "previous_record_digest": previous_record_digest,
            "snapshot_digest": snapshot_digest,
            "snapshot_json": snapshot_json,
            "created_at": created_at,
        }

    @staticmethod
    def _canonical(value: Any) -> bytes:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def _validated_journal_head(
        self,
        connection: sqlite3.Connection,
        *,
        expected_run_id: str,
        authority_record: DurableAuthorityRecord,
    ) -> tuple[Optional[str], str]:
        """Verify the full external hash chain and return its latest snapshot."""

        rows = connection.execute(
            "SELECT * FROM attended_journal WHERE path_key = ? ORDER BY sequence",
            (self.path_key,),
        ).fetchall()
        if not rows:
            if (
                authority_record.journal_sequence != 0
                or authority_record.journal_head_digest != JOURNAL_GENESIS_DIGEST
            ):
                raise DurableAuthorityBusy(
                    "the external attended-decision journal tail was restored"
                )
            return None, JOURNAL_GENESIS_DIGEST
        key = self._journal_key(create=False)
        previous = JOURNAL_GENESIS_DIGEST
        latest_snapshot: Optional[str] = None
        for expected_sequence, row in enumerate(rows, start=1):
            values = dict(row)
            if (
                values["sequence"] != expected_sequence
                or values["run_id"] != expected_run_id
                or values["previous_record_digest"] != previous
            ):
                raise DurableAuthorityBusy(
                    "the external attended-decision journal chain is invalid"
                )
            snapshot_json = str(values["snapshot_json"])
            snapshot_digest = (
                "sha256:" + hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            )
            if snapshot_digest != values["snapshot_digest"]:
                raise DurableAuthorityBusy(
                    "the external attended-decision snapshot changed"
                )
            unsigned = self._journal_unsigned(
                path_key=self.path_key,
                sequence=expected_sequence,
                run_id=expected_run_id,
                previous_record_digest=previous,
                snapshot_digest=snapshot_digest,
                snapshot_json=snapshot_json,
                created_at=str(values["created_at"]),
            )
            record_digest = (
                "sha256:" + hashlib.sha256(self._canonical(unsigned)).hexdigest()
            )
            record_mac = (
                "hmac-sha256:"
                + hmac.new(
                    key,
                    JOURNAL_MAC_DOMAIN + record_digest.encode("ascii"),
                    hashlib.sha256,
                ).hexdigest()
            )
            if not hmac.compare_digest(
                record_digest, values["record_digest"]
            ) or not hmac.compare_digest(record_mac, values["record_mac"]):
                raise DurableAuthorityBusy(
                    "the external attended-decision journal HMAC does not verify"
                )
            previous = record_digest
            latest_snapshot = snapshot_json
        if (
            len(rows) != authority_record.journal_sequence
            or previous != authority_record.journal_head_digest
        ):
            raise DurableAuthorityBusy(
                "the external attended-decision journal tail was restored"
            )
        return latest_snapshot, previous

    @staticmethod
    def _validate_snapshot_extension(
        previous_json: Optional[str],
        snapshot_json: str,
    ) -> None:
        """Require one decision-log snapshot to extend the prior snapshot."""

        try:
            current = json.loads(snapshot_json)
            previous = json.loads(previous_json) if previous_json is not None else None
        except ValueError as exc:
            raise DurableAuthorityBusy(
                "the attended-decision snapshot is not valid JSON"
            ) from exc
        if not isinstance(current, dict) or set(current) != {
            "schema_version",
            "decisions",
            "relay_acknowledgements",
        }:
            raise DurableAuthorityBusy(
                "the attended-decision snapshot has an invalid schema"
            )
        if (
            current.get("schema_version") != 1
            or not isinstance(current.get("decisions"), list)
            or not isinstance(current.get("relay_acknowledgements"), list)
        ):
            raise DurableAuthorityBusy(
                "the attended-decision snapshot has an invalid schema"
            )
        if previous is None:
            return
        if not isinstance(previous, dict):
            raise DurableAuthorityBusy(
                "the prior attended-decision snapshot is invalid"
            )
        old_decisions = previous.get("decisions")
        new_decisions = current["decisions"]
        old_acks = previous.get("relay_acknowledgements")
        new_acks = current["relay_acknowledgements"]
        if (
            not isinstance(old_decisions, list)
            or not isinstance(old_acks, list)
            or len(new_decisions) < len(old_decisions)
            or new_decisions[: len(old_decisions)] != old_decisions
            or len(new_acks) < len(old_acks)
        ):
            raise DurableAuthorityBusy(
                "the attended-decision snapshot is not append-only"
            )
        confirmations = 0
        for old, new in zip(old_acks, new_acks):
            if old == new:
                continue
            if not isinstance(old, dict) or not isinstance(new, dict):
                raise DurableAuthorityBusy(
                    "the attended-decision acknowledgement changed"
                )
            immutable_old = {
                key: value
                for key, value in old.items()
                if key not in {"confirmed", "confirmed_at", "record_mac"}
            }
            immutable_new = {
                key: value
                for key, value in new.items()
                if key not in {"confirmed", "confirmed_at", "record_mac"}
            }
            if (
                immutable_old != immutable_new
                or old.get("confirmed") is not False
                or old.get("confirmed_at") is not None
                or new.get("confirmed") is not True
                or not isinstance(new.get("confirmed_at"), str)
                or not new.get("confirmed_at")
            ):
                raise DurableAuthorityBusy(
                    "the attended-decision acknowledgement changed"
                )
            confirmations += 1
        if confirmations > 1:
            raise DurableAuthorityBusy(
                "one journal revision confirmed multiple relay acknowledgements"
            )

    def read_attended_snapshot(
        self, *, expected_run_id: str
    ) -> tuple[Optional[str], str]:
        """Read the authenticated journal snapshot and its monotonic head."""

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or record.run_id != expected_run_id:
                raise DurableAuthorityBusy(
                    "the attended journal does not match the durable run"
                )
            return self._validated_journal_head(
                connection,
                expected_run_id=expected_run_id,
                authority_record=record,
            )

    def append_attended_snapshot(
        self,
        *,
        expected_run_id: str,
        expected_head_digest: str,
        snapshot_json: str,
    ) -> str:
        """Append one authenticated journal revision in the external authority."""

        # Parse before the transaction so invalid JSON never enters authority.
        self._validate_snapshot_extension(None, snapshot_json)
        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or record.run_id != expected_run_id:
                raise DurableAuthorityBusy(
                    "the attended journal does not match the durable run"
                )
            latest, head = self._validated_journal_head(
                connection,
                expected_run_id=expected_run_id,
                authority_record=record,
            )
            if head != expected_head_digest:
                raise DurableAuthorityBusy(
                    "the attended-decision journal changed before append"
                )
            self._validate_snapshot_extension(latest, snapshot_json)
            sequence = record.journal_sequence + 1
            created_at = _now()
            snapshot_digest = (
                "sha256:" + hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest()
            )
            unsigned = self._journal_unsigned(
                path_key=self.path_key,
                sequence=sequence,
                run_id=expected_run_id,
                previous_record_digest=head,
                snapshot_digest=snapshot_digest,
                snapshot_json=snapshot_json,
                created_at=created_at,
            )
            record_digest = (
                "sha256:" + hashlib.sha256(self._canonical(unsigned)).hexdigest()
            )
            key = self._journal_key(create=True)
            record_mac = (
                "hmac-sha256:"
                + hmac.new(
                    key,
                    JOURNAL_MAC_DOMAIN + record_digest.encode("ascii"),
                    hashlib.sha256,
                ).hexdigest()
            )
            connection.execute(
                """
                INSERT INTO attended_journal (
                    path_key, sequence, run_id, previous_record_digest,
                    snapshot_digest, snapshot_json, record_digest, record_mac,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    self.path_key,
                    sequence,
                    expected_run_id,
                    head,
                    snapshot_digest,
                    snapshot_json,
                    record_digest,
                    record_mac,
                    created_at,
                ),
            )
            self._update(
                connection,
                record,
                journal_sequence=sequence,
                journal_head_digest=record_digest,
            )
            return record_digest

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection: Optional[sqlite3.Connection] = None
        with self._windows_active_race_handles():
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
            except (OSError, sqlite3.Error) as exc:
                if connection is not None:
                    connection.close()
                raise DurableAuthorityBusy(
                    "the external durable authority is unavailable"
                ) from exc
            try:
                yield connection
                connection.execute("COMMIT")
            except sqlite3.Error as exc:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise DurableAuthorityBusy(
                    "the external durable authority transaction failed"
                ) from exc
            except Exception:
                try:
                    connection.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                raise
            finally:
                connection.close()

    @staticmethod
    def _record(row: Optional[sqlite3.Row]) -> Optional[DurableAuthorityRecord]:
        if row is None:
            return None
        try:
            return DurableAuthorityRecord.model_validate(dict(row))
        except ValueError as exc:
            raise DurableAuthorityBusy(
                "the external durable authority record is invalid"
            ) from exc

    def _read(self, connection: sqlite3.Connection) -> Optional[DurableAuthorityRecord]:
        return self._record(
            connection.execute(
                "SELECT * FROM durable_authority WHERE path_key = ?",
                (self.path_key,),
            ).fetchone()
        )

    def _remote_cursor(
        self, connection: sqlite3.Connection
    ) -> tuple[str | None, int, str | None]:
        """Read the private server cursor without adding it to a record dump."""

        row = connection.execute(
            "SELECT authority_id, next_sequence, cursor_secret "
            "FROM remote_delivery_cursor WHERE path_key = ?",
            (self.path_key,),
        ).fetchone()
        if row is None:
            return None, 0, None
        authority_id = row["authority_id"]
        sequence = row["next_sequence"]
        cursor = row["cursor_secret"]
        if not (
            isinstance(authority_id, str)
            and authority_id
            and type(sequence) is int
            and sequence >= 0
            and isinstance(cursor, str)
            and _REMOTE_PERMIT_CURSOR_RE.fullmatch(cursor)
        ):
            raise DurableAuthorityBusy("the remote delivery cursor is invalid")
        return authority_id, sequence, cursor

    def _advance_remote_cursor(
        self,
        connection: sqlite3.Connection,
        *,
        authority_id: str,
        next_sequence: int,
        cursor_secret: str,
    ) -> None:
        if not (
            authority_id
            and _REMOTE_AUTHORITY_ID_RE.fullmatch(authority_id)
            and type(next_sequence) is int
            and next_sequence > 0
            and _REMOTE_PERMIT_CURSOR_RE.fullmatch(cursor_secret)
        ):
            raise DurableAuthorityBusy("the remote delivery permit is invalid")
        existing_id, existing_sequence, existing_cursor = self._remote_cursor(
            connection
        )
        if existing_id is not None and (
            existing_id != authority_id
            or existing_sequence + 1 != next_sequence
            or existing_cursor is None
        ):
            raise DurableAuthorityBusy("the remote delivery cursor changed")
        if existing_id is None:
            if next_sequence != 1:
                raise DurableAuthorityBusy(
                    "the first remote delivery sequence is invalid"
                )
            connection.execute(
                "INSERT INTO remote_delivery_cursor "
                "(path_key, authority_id, next_sequence, cursor_secret) "
                "VALUES (?, ?, ?, ?)",
                (self.path_key, authority_id, next_sequence, cursor_secret),
            )
            return
        cursor = connection.execute(
            "UPDATE remote_delivery_cursor SET next_sequence = ?, cursor_secret = ? "
            "WHERE path_key = ? AND authority_id = ? AND next_sequence = ? "
            "AND cursor_secret = ?",
            (
                next_sequence,
                cursor_secret,
                self.path_key,
                existing_id,
                existing_sequence,
                existing_cursor,
            ),
        )
        if cursor.rowcount != 1:
            raise DurableAuthorityBusy("the remote delivery cursor changed")

    def _pending_remote_delivery(
        self, connection: sqlite3.Connection
    ) -> _RemoteDeliveryPermit | None:
        row = connection.execute(
            "SELECT * FROM remote_delivery_pending WHERE path_key = ?",
            (self.path_key,),
        ).fetchone()
        if row is None:
            return None
        raw = bytes(row["permit_artifact_bytes"])
        artifact = self._parse_permit_artifact_bytes(raw)
        permit = _RemoteDeliveryPermit(
            authority_id=str(row["authority_id"]),
            authority_origin=str(row["authority_origin"]),
            authority_signer_sha256=str(row["authority_signer_sha256"]),
            permit_id=str(row["permit_id"]),
            dispatch_session_id=str(row["dispatch_session_id"]),
            one_use_claim_id=str(row["one_use_claim_id"]),
            next_sequence=int(row["next_sequence"]),
            cursor_secret=str(row["cursor_secret"]),
            input_edge_sequence=int(row["input_edge_sequence"]),
            authority_sequence=int(row["authority_sequence"]),
            runtime_delivery_sequence=int(row["runtime_delivery_sequence"]),
            permit_digest="sha256:" + str(row["permit_artifact_sha256"]),
            permit_artifact_bytes=raw,
            permit_artifact=artifact,
        )
        if not (
            permit.authority_id == artifact.payload.execution_authority_id
            and permit.authority_signer_sha256 == artifact.signer.signer_sha256()
            and urlparse(permit.authority_origin).scheme in {"http", "https"}
            and bool(urlparse(permit.authority_origin).netloc)
            and urlparse(permit.authority_origin).path == ""
            and permit.permit_id == artifact.payload.permit_id
            and permit.input_edge_sequence == artifact.payload.input_edge_sequence
            and permit.authority_sequence == artifact.payload.authority_sequence
            and permit.runtime_delivery_sequence == permit.authority_sequence
            and permit.next_sequence == permit.authority_sequence + 1
            and permit.permit_digest == "sha256:" + hashlib.sha256(raw).hexdigest()
            and _REMOTE_UUID_RE.fullmatch(permit.dispatch_session_id)
            and _REMOTE_UUID_RE.fullmatch(permit.one_use_claim_id)
            and _REMOTE_PERMIT_CURSOR_RE.fullmatch(permit.cursor_secret)
        ):
            raise DurableAuthorityBusy(
                "the pending remote delivery authority is invalid"
            )
        return permit

    def _retain_pending_remote_delivery(
        self,
        connection: sqlite3.Connection,
        permit: _RemoteDeliveryPermit,
    ) -> None:
        if self._pending_remote_delivery(connection) is not None:
            raise DurableAuthorityBusy(
                "a prior production delivery lacks an acknowledgment receipt"
            )
        previous = connection.execute(
            "SELECT permit_artifact_bytes, receipt_artifact_bytes "
            "FROM remote_delivery_artifacts WHERE path_key = ? "
            "ORDER BY runtime_delivery_sequence DESC LIMIT 1",
            (self.path_key,),
        ).fetchone()
        if previous is not None:
            previous_permit = self._parse_permit_artifact_bytes(
                bytes(previous["permit_artifact_bytes"])
            )
            previous_receipt = self._parse_receipt_artifact_bytes(
                bytes(previous["receipt_artifact_bytes"])
            )
            previous_entry = ProductionDeliveryPermit.build(
                previous_permit, previous_receipt
            )
            if not (
                permit.authority_id == previous_entry.execution_authority_id
                and permit.authority_signer_sha256
                == previous_entry.authority_signer_sha256
                and permit.input_edge_sequence == previous_entry.input_edge_sequence + 1
                and permit.authority_sequence == previous_entry.authority_sequence + 1
                and permit.runtime_delivery_sequence
                == previous_entry.runtime_delivery_sequence + 1
            ):
                raise DurableAuthorityBusy(
                    "remote delivery permit changes the retained authority chain"
                )
        elif not (
            permit.input_edge_sequence == 1
            and permit.authority_sequence == 0
            and permit.runtime_delivery_sequence == 0
        ):
            raise DurableAuthorityBusy(
                "the first remote delivery permit sequence is invalid"
            )
        try:
            connection.execute(
                "INSERT INTO remote_delivery_pending "
                "(path_key, authority_id, authority_signer_sha256, permit_id, "
                "authority_origin, "
                "dispatch_session_id, one_use_claim_id, next_sequence, "
                "cursor_secret, input_edge_sequence, authority_sequence, "
                "runtime_delivery_sequence, permit_artifact_sha256, "
                "permit_artifact_bytes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self.path_key,
                    permit.authority_id,
                    permit.authority_signer_sha256,
                    permit.permit_id,
                    permit.authority_origin,
                    permit.dispatch_session_id,
                    permit.one_use_claim_id,
                    permit.next_sequence,
                    permit.cursor_secret,
                    permit.input_edge_sequence,
                    permit.authority_sequence,
                    permit.runtime_delivery_sequence,
                    permit.permit_digest.removeprefix("sha256:"),
                    permit.permit_artifact_bytes,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise DurableAuthorityBusy(
                "a production delivery permit is already pending"
            ) from exc

    @staticmethod
    def _same_remote_permit(
        left: _RemoteDeliveryPermit, right: _RemoteDeliveryPermit
    ) -> bool:
        return left == right

    def _validate_pending_delivery_owner(
        self,
        connection: sqlite3.Connection,
        manifest: Any,
        permit: _RemoteDeliveryPermit,
        *,
        attempt_id: str | None,
        owner_nonce_sha256: str | None,
    ) -> DurableAuthorityRecord:
        retained = self._pending_remote_delivery(connection)
        if retained is None or not self._same_remote_permit(retained, permit):
            raise DurableAuthorityBusy(
                "the pending remote delivery changed before acknowledgment"
            )
        if attempt_id is None:
            record = self._read(connection)
            if (
                record is None
                or not self._identity_matches(record, manifest)
                or record.phase != "active"
            ):
                raise DurableAuthorityBusy(
                    "the managed initial delivery authority changed"
                )
            return record
        if owner_nonce_sha256 is None:
            raise DurableAuthorityBusy("the continuation delivery owner is unavailable")
        record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
        if record.attempt_phase != "delivery_started":
            raise DurableAuthorityBusy("the continuation is not at a delivery boundary")
        return record

    @staticmethod
    def _identity_matches(record: DurableAuthorityRecord, manifest: Any) -> bool:
        return (
            record.schema_version == AUTHORITY_SCHEMA_VERSION
            and record.namespace_id == manifest.namespace_id
            and record.run_id == manifest.run_id
        )

    @staticmethod
    def _parse_time(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)

    def _update(
        self,
        connection: sqlite3.Connection,
        record: DurableAuthorityRecord,
        **changes: Any,
    ) -> DurableAuthorityRecord:
        updated = record.model_copy(
            update={
                **changes,
                "revision": record.revision + 1,
                "updated_at": _now(),
            }
        )
        values = updated.model_dump(mode="python")
        values["reject_requested"] = int(updated.reject_requested)
        assignments = ", ".join(
            f"{name} = :{name}" for name in values if name != "path_key"
        )
        cursor = connection.execute(
            f"UPDATE durable_authority SET {assignments} "
            "WHERE path_key = :path_key AND revision = :expected_revision",
            {**values, "expected_revision": record.revision},
        )
        if cursor.rowcount != 1:
            raise DurableAuthorityBusy(
                "the external durable authority changed before commit"
            )
        return updated

    def claim(self, manifest: Any) -> None:
        """Permanently reserve a canonical path for one fresh run identity."""

        if not manifest.namespace_id or not manifest.run_id:
            raise DurableAuthorityBusy("the fresh durable identity is incomplete")
        with self._transaction() as connection:
            retained = self._read(connection)
            if retained is not None:
                if (
                    self._identity_matches(retained, manifest)
                    and retained.phase == "claimed"
                    and not retained.progress_digest
                    and not retained.attempt_id
                    and retained.journal_sequence == 0
                    and retained.journal_head_digest == JOURNAL_GENESIS_DIGEST
                ):
                    # A process can stop after the external claim commits but
                    # before the local claim and manifest exist. The exact run
                    # can finish that one initialization; no other identity can.
                    return
                raise DurableAuthorityBusy(
                    "this canonical run path already has durable authority; "
                    "use a new run directory"
                )
            try:
                connection.execute(
                    """
                    INSERT INTO durable_authority (
                        path_key, namespace_id, run_id, schema_version, revision,
                        phase, progress_digest, pause_binding_sha256,
                        approval_digest,
                        report_sha256, attempt_id, operation,
                        owner_nonce_sha256, attempt_phase, reject_requested,
                        delivery_sequence, acquired_at, expires_at, updated_at
                    ) VALUES (?, ?, ?, 1, 1, 'claimed', '', '', '', '', '', '', '',
                              'none', 0, 0, '', '', ?)
                    """,
                    (
                        self.path_key,
                        manifest.namespace_id,
                        manifest.run_id,
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DurableAuthorityBusy(
                    "the durable run identity is already registered"
                ) from exc

    def activate(self, manifest: Any) -> str:
        """Bind a fresh local manifest/claim snapshot to its permanent record."""

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy(
                    "the external durable claim does not match this run"
                )
            if record.phase != "claimed" or record.progress_digest:
                raise DurableAuthorityBusy(
                    "the fresh durable authority was already activated"
                )
            digest = self.store.continuation_state_digest()
            self._update(
                connection,
                record,
                phase="active",
                progress_digest=digest,
            )
            return digest

    def validate(self, manifest: Any) -> DurableAuthorityRecord:
        """Require external identity and the exact current local progress."""

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy(
                    "the external durable authority is missing or belongs to "
                    "a different run"
                )
            if record.phase in {
                "claimed",
                "reconciliation_required",
                "terminal_prepared",
                "completed",
                "rejected",
            }:
                raise DurableAuthorityBusy(
                    f"the durable authority is {record.phase}; automatic "
                    "continuation is refused"
                )
            digest = self.store.continuation_state_digest()
            if record.progress_digest != digest:
                raise DurableAuthorityBusy(
                    "local durable state does not match the external monotonic "
                    "authority; reconcile or start a new run"
                )
            return record

    def advance(
        self,
        manifest: Any,
        *,
        expected_progress_digest: str,
        phase: Literal["active", "paused", "continuing"],
        pause_binding_sha256: str = "",
        attempt_id: str = "",
        owner_nonce_sha256: str = "",
    ) -> str:
        """CAS one trusted local mutation into the monotonic record."""

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy("durable authority identity changed")
            if record.progress_digest != expected_progress_digest:
                raise DurableAuthorityBusy(
                    "durable authority progress changed before local commit"
                )
            if record.attempt_id and (
                record.attempt_id != attempt_id
                or record.owner_nonce_sha256 != owner_nonce_sha256
            ):
                raise DurableAuthorityBusy(
                    "a different continuation owns durable progress"
                )
            digest = self.store.continuation_state_digest()
            if digest == record.progress_digest:
                return digest
            self._update(
                connection,
                record,
                phase=phase,
                progress_digest=digest,
                pause_binding_sha256=pause_binding_sha256,
                attempt_phase=(
                    "validating" if record.attempt_id else record.attempt_phase
                ),
            )
            return digest

    def acquire(
        self,
        manifest: Any,
        *,
        pause_binding_sha256: str,
        attempt_id: str,
        operation: str,
        owner_nonce_sha256: str,
        acquired_at: str,
        expires_at: str,
        now: datetime,
    ) -> None:
        """Acquire one external single-flight continuation attempt."""

        reconciliation_required = False
        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy("durable authority identity changed")
            digest = self.store.continuation_state_digest()
            if digest != record.progress_digest:
                raise DurableAuthorityBusy(
                    "local durable state was restored or changed outside the "
                    "monotonic authority"
                )
            if record.phase not in {"paused", "continuing"}:
                raise DurableAuthorityBusy(
                    f"durable continuation is unavailable while authority is "
                    f"{record.phase}"
                )
            if (
                record.pause_binding_sha256
                and record.pause_binding_sha256 != pause_binding_sha256
            ):
                raise DurableAuthorityBusy(
                    "the external authority binds a different active pause"
                )
            if record.attempt_id:
                expired = bool(record.expires_at) and (
                    self._parse_time(record.expires_at) < now
                )
                if not expired:
                    raise DurableAuthorityBusy(
                        "another continuation is already in progress"
                    )
                if record.attempt_phase != "validating":
                    self._update(
                        connection,
                        record,
                        phase="reconciliation_required",
                        attempt_phase="reconciliation_required",
                    )
                    reconciliation_required = True
            if not reconciliation_required:
                self._update(
                    connection,
                    record,
                    phase="continuing",
                    pause_binding_sha256=pause_binding_sha256,
                    approval_digest="",
                    attempt_id=attempt_id,
                    operation=operation,
                    owner_nonce_sha256=owner_nonce_sha256,
                    attempt_phase="validating",
                    reject_requested=False,
                    delivery_sequence=0,
                    acquired_at=acquired_at,
                    expires_at=expires_at,
                )
        if reconciliation_required:
            # Raise only after the transaction commits the monotonic refusal.
            # Raising inside ``_transaction`` would roll the safety state back.
            raise DurableAuthorityBusy(
                "an expired continuation may have delivered input; "
                "reconciliation is required"
            )

    def bind_approval(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
        approval_digest: str,
    ) -> None:
        """Bind exact per-pause authority without changing executable progress."""

        with self._transaction() as connection:
            record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
            if record.approval_digest:
                if record.approval_digest != approval_digest:
                    raise DurableAuthorityBusy(
                        "the external authority already binds a different approval"
                    )
                return
            if record.attempt_phase != "validating" or record.delivery_sequence != 0:
                raise DurableAuthorityBusy(
                    "approval can bind only before continuation delivery"
                )
            self._update(connection, record, approval_digest=approval_digest)

    @staticmethod
    def _remote_request_digest(payload: dict[str, Any]) -> str:
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _exact_artifact_bytes(value: Any, *, field: str) -> bytes:
        if (
            not isinstance(value, str)
            or not value
            or len(value) > ((MAX_DELIVERY_ARTIFACT_BYTES * 4 // 3) + 8)
        ):
            raise DurableAuthorityBusy(f"{field} is invalid")
        try:
            decoded = b64decode(value, validate=True)
        except (TypeError, ValueError, BinasciiError) as exc:
            raise DurableAuthorityBusy(f"{field} is invalid") from exc
        if (
            not decoded
            or len(decoded) > MAX_DELIVERY_ARTIFACT_BYTES
            or b64encode(decoded).decode("ascii") != value
        ):
            raise DurableAuthorityBusy(f"{field} is invalid")
        return decoded

    @staticmethod
    def _parse_permit_artifact_bytes(raw: bytes) -> ProductionDeliveryPermitArtifact:
        try:
            artifact = ProductionDeliveryPermitArtifact.model_validate_json(raw)
        except ValueError as exc:
            raise DurableAuthorityBusy(
                "remote delivery permit artifact is invalid"
            ) from exc
        if artifact.canonical_bytes() != raw:
            raise DurableAuthorityBusy(
                "remote delivery permit artifact is not canonical JSON"
            )
        return artifact

    @staticmethod
    def _parse_receipt_artifact_bytes(
        raw: bytes,
    ) -> ProductionDeliveryReceiptArtifact:
        try:
            artifact = ProductionDeliveryReceiptArtifact.model_validate_json(raw)
        except ValueError as exc:
            raise DurableAuthorityBusy(
                "remote delivery receipt artifact is invalid"
            ) from exc
        if artifact.canonical_bytes() != raw:
            raise DurableAuthorityBusy(
                "remote delivery receipt artifact is not canonical JSON"
            )
        return artifact

    def _require_remote_delivery_permit(
        self,
        manifest: Any,
        record: DurableAuthorityRecord,
        *,
        remote_authority_id: str | None,
        remote_delivery_sequence: int,
        permit_cursor: str | None,
    ) -> _RemoteDeliveryPermit | None:
        """Obtain the one server-owned permit for a production input edge.

        This intentionally records no local success before the response is
        completely validated.  A lost response after a server commit therefore
        leaves the local sequence unchanged; a retry asks for the same sequence
        and must halt when the authority rejects that duplicate.
        """

        authorization = getattr(manifest, "governed_authorization", None)
        profile = getattr(authorization, "execution_profile", None)
        if profile not in {"standard", "regulated"}:
            return None
        authority_kind = getattr(manifest, "delivery_authority_kind", "customer_local")
        if authority_kind == "customer_local":
            return None
        if authority_kind != "cloud_runner":
            raise DurableAuthorityBusy("durable run has an invalid delivery authority")
        initial = record.operation == "initial"
        pause_binding = (
            getattr(manifest, "managed_dispatch_binding_sha256", None)
            if initial
            else record.pause_binding_sha256
        )
        initial_binding = getattr(manifest, "managed_dispatch_binding_sha256", None)
        required = {
            "run_id": getattr(manifest, "remote_delivery_run_id", None),
            "namespace_id": manifest.namespace_id,
            "path_key": record.path_key,
            "pause_binding_sha256": pause_binding,
            "progress_digest": record.progress_digest,
            "approval_digest": initial_binding if initial else record.approval_digest,
            "attempt_id": manifest.namespace_id if initial else record.attempt_id,
            "operation": record.operation,
        }
        if not (
            isinstance(required["run_id"], str)
            and _REMOTE_UUID_RE.fullmatch(required["run_id"])
            and isinstance(required["namespace_id"], str)
            and _REMOTE_TOKEN_RE.fullmatch(required["namespace_id"])
            and isinstance(required["path_key"], str)
            and _REMOTE_PATH_KEY_RE.fullmatch(required["path_key"])
            and (
                isinstance(required["pause_binding_sha256"], str)
                and _REMOTE_DIGEST_RE.fullmatch(required["pause_binding_sha256"])
            )
            and isinstance(required["progress_digest"], str)
            and _REMOTE_DIGEST_RE.fullmatch(required["progress_digest"])
            and isinstance(required["approval_digest"], str)
            and _REMOTE_DIGEST_RE.fullmatch(required["approval_digest"])
            and isinstance(required["attempt_id"], str)
            and _REMOTE_TOKEN_RE.fullmatch(required["attempt_id"])
            and required["operation"] in _REMOTE_OPERATIONS
            and type(remote_delivery_sequence) is int
            and remote_delivery_sequence >= 0
            and (
                permit_cursor is None
                or (
                    isinstance(permit_cursor, str)
                    and _REMOTE_PERMIT_CURSOR_RE.fullmatch(permit_cursor)
                )
            )
        ):
            raise DurableAuthorityBusy(
                "production delivery requires privacy-safe retained remote authority inputs"
            )
        url = os.getenv(REMOTE_AUTHORITY_URL_ENV, "")
        token = os.getenv(REMOTE_AUTHORITY_TOKEN_ENV, "")
        expected_dispatch_session_id = os.getenv(REMOTE_DISPATCH_SESSION_ID_ENV, "")
        if not url or not token:
            raise DurableAuthorityBusy(
                "production delivery requires configured remote authority credentials"
            )
        parsed = urlparse(url)
        if (
            not parsed.scheme
            or not parsed.netloc
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise DurableAuthorityBusy(
                "production delivery authority endpoint is invalid"
            )
        if self._remote_transport is None:
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username is not None
                or parsed.password is not None
                or bool(parsed.query)
                or bool(parsed.fragment)
            ):
                raise DurableAuthorityBusy(
                    "production delivery authority endpoint must use HTTPS"
                )
        payload: dict[str, Any] = {
            "schema_version": 1,
            "run_id": required["run_id"],
            "namespace_id": required["namespace_id"],
            "path_key": required["path_key"],
            "execution_profile": profile,
            "pause_binding_sha256": required["pause_binding_sha256"],
            "progress_digest": required["progress_digest"],
            "approval_digest": required["approval_digest"],
            "attempt_id": required["attempt_id"],
            "operation": required["operation"],
            "remote_delivery_sequence": remote_delivery_sequence,
            "permit_cursor": permit_cursor,
            "delivery_sequence": record.delivery_sequence,
        }
        request_digest = self._remote_request_digest(payload)
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            if self._remote_transport is not None:
                response_bytes = self._remote_transport(url, headers, body)
            else:
                request = Request(url, data=body, headers=headers, method="POST")
                opener = build_opener(_RefuseRemoteAuthorityRedirects())
                with opener.open(request, timeout=10) as response:  # nosec B310 - HTTPS above
                    if not 200 <= response.status < 300:
                        raise DurableAuthorityBusy("remote delivery authority refused")
                    response_bytes = response.read(
                        MAX_REMOTE_AUTHORITY_RESPONSE_BYTES + 1
                    )
                    if len(response_bytes) > MAX_REMOTE_AUTHORITY_RESPONSE_BYTES:
                        raise DurableAuthorityBusy(
                            "remote delivery authority response is too large"
                        )
        except DurableAuthorityBusy:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise DurableAuthorityBusy(
                "remote delivery authority is unavailable or refused the permit"
            ) from exc
        if (
            not isinstance(response_bytes, bytes)
            or len(response_bytes) > MAX_REMOTE_AUTHORITY_RESPONSE_BYTES
        ):
            raise DurableAuthorityBusy(
                "remote delivery authority response is too large"
            )
        try:
            response = json.loads(response_bytes.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DurableAuthorityBusy(
                "remote delivery authority returned invalid JSON"
            ) from exc
        expected_keys = {
            "schema_version",
            "status",
            "execution_authority_id",
            "permit_id",
            "dispatch_session_id",
            "one_use_claim_id",
            "permit_artifact_bytes_base64",
            "permit_artifact_sha256",
            "next_permit_cursor",
            "input_edge_sequence",
            "authority_sequence",
            "runtime_delivery_sequence",
        }
        if not isinstance(response, dict) or set(response) != expected_keys:
            raise DurableAuthorityBusy(
                "remote delivery authority response has invalid schema"
            )
        if not (
            response["schema_version"]
            == "openadapt.production-delivery-permit-issue/v2"
            and response["status"] == "issued"
            and isinstance(response["execution_authority_id"], str)
            and _REMOTE_AUTHORITY_ID_RE.fullmatch(response["execution_authority_id"])
            and isinstance(response["permit_id"], str)
            and _REMOTE_UUID_RE.fullmatch(response["permit_id"])
            and isinstance(response["dispatch_session_id"], str)
            and _REMOTE_UUID_RE.fullmatch(response["dispatch_session_id"])
            and (
                not expected_dispatch_session_id
                or response["dispatch_session_id"] == expected_dispatch_session_id
            )
            and isinstance(response["one_use_claim_id"], str)
            and _REMOTE_UUID_RE.fullmatch(response["one_use_claim_id"])
            and isinstance(response["permit_artifact_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", response["permit_artifact_sha256"])
            and isinstance(response["next_permit_cursor"], str)
            and _REMOTE_PERMIT_CURSOR_RE.fullmatch(response["next_permit_cursor"])
            and response["next_permit_cursor"] != permit_cursor
            and type(response["input_edge_sequence"]) is int
            and response["input_edge_sequence"] == remote_delivery_sequence + 1
            and type(response["authority_sequence"]) is int
            and response["authority_sequence"] == remote_delivery_sequence
            and type(response["runtime_delivery_sequence"]) is int
            and response["runtime_delivery_sequence"] == remote_delivery_sequence
            and (
                remote_authority_id is None
                or response["execution_authority_id"] == remote_authority_id
            )
        ):
            raise DurableAuthorityBusy(
                "remote delivery authority response does not match request"
            )
        permit_artifact_bytes = self._exact_artifact_bytes(
            response["permit_artifact_bytes_base64"],
            field="remote delivery permit artifact bytes",
        )
        permit_artifact_sha256 = hashlib.sha256(permit_artifact_bytes).hexdigest()
        if permit_artifact_sha256 != response["permit_artifact_sha256"]:
            raise DurableAuthorityBusy(
                "remote delivery permit artifact digest does not match response"
            )
        permit_artifact = self._parse_permit_artifact_bytes(permit_artifact_bytes)
        permit_payload = permit_artifact.payload
        if not (
            permit_payload.execution_authority_id == response["execution_authority_id"]
            and permit_payload.permit_id == response["permit_id"]
            and permit_payload.run_id == required["run_id"]
            and permit_payload.flow_run_id_sha256
            == hashlib.sha256(required["run_id"].encode("utf-8")).hexdigest()
            and permit_payload.action_request_sha256 == request_digest
            and permit_payload.input_edge_sequence == response["input_edge_sequence"]
            and permit_payload.authority_sequence == response["authority_sequence"]
        ):
            raise DurableAuthorityBusy(
                "remote delivery permit artifact does not bind the exact request"
            )
        permit_digest = "sha256:" + permit_artifact_sha256
        return _RemoteDeliveryPermit(
            authority_id=response["execution_authority_id"],
            authority_origin=f"{parsed.scheme}://{parsed.netloc}",
            next_sequence=remote_delivery_sequence + 1,
            cursor_secret=response["next_permit_cursor"],
            permit_digest=permit_digest,
            authority_signer_sha256=permit_artifact.signer.signer_sha256(),
            permit_id=response["permit_id"],
            dispatch_session_id=response["dispatch_session_id"],
            one_use_claim_id=response["one_use_claim_id"],
            input_edge_sequence=response["input_edge_sequence"],
            authority_sequence=response["authority_sequence"],
            runtime_delivery_sequence=response["runtime_delivery_sequence"],
            permit_artifact_bytes=permit_artifact_bytes,
            permit_artifact=permit_artifact,
        )

    def acknowledge_remote_delivery(
        self,
        manifest: Any,
        permit: _RemoteDeliveryPermit,
        *,
        attempt_id: str | None = None,
        owner_nonce_sha256: str | None = None,
    ) -> ProductionDeliveryPermit:
        """Acknowledge one completed backend call and commit its exact edge.

        The backend call happens before this method. Until Cloud returns the
        signed receipt and the protected local transaction retains both exact
        artifacts, the edge remains uncertain and no later permit can issue.
        """

        # Validate the exact pending edge and current owner before a remote
        # mutation. Recheck it after the response before local commit.
        with self._transaction() as connection:
            self._validate_pending_delivery_owner(
                connection,
                manifest,
                permit,
                attempt_id=attempt_id,
                owner_nonce_sha256=owner_nonce_sha256,
            )
        url = os.getenv(REMOTE_AUTHORITY_URL_ENV, "")
        token = os.getenv(REMOTE_AUTHORITY_TOKEN_ENV, "")
        if not url or not token:
            raise DurableAuthorityBusy(
                "production delivery acknowledgment requires remote credentials"
            )
        parsed = urlparse(url)
        if not parsed.scheme or not parsed.netloc:
            raise DurableAuthorityBusy(
                "production delivery acknowledgment endpoint is invalid"
            )
        if self._remote_transport is None and (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.query)
            or bool(parsed.fragment)
        ):
            raise DurableAuthorityBusy(
                "production delivery acknowledgment endpoint must use HTTPS"
            )
        if f"{parsed.scheme}://{parsed.netloc}" != permit.authority_origin:
            raise DurableAuthorityBusy(
                "production delivery authority origin changed before acknowledgment"
            )
        acknowledgment_url = (
            f"{parsed.scheme}://{parsed.netloc}"
            "/api/internal/managed-delivery-acknowledgment"
        )
        body_value = {
            "schema_version": "openadapt.production-delivery-acknowledgment/v2",
            "run_id": getattr(manifest, "remote_delivery_run_id", None),
            "dispatch_session_id": permit.dispatch_session_id,
            "one_use_claim_id": permit.one_use_claim_id,
            "runtime_delivery_sequence": permit.runtime_delivery_sequence,
            "permit_artifact_bytes_base64": b64encode(
                permit.permit_artifact_bytes
            ).decode("ascii"),
        }
        body = json.dumps(
            body_value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            if self._remote_transport is not None:
                response_bytes = self._remote_transport(
                    acknowledgment_url, headers, body
                )
            else:
                request = Request(
                    acknowledgment_url, data=body, headers=headers, method="POST"
                )
                opener = build_opener(_RefuseRemoteAuthorityRedirects())
                with opener.open(request, timeout=10) as response:  # nosec B310
                    if response.status not in {200, 201}:
                        raise DurableAuthorityBusy(
                            "remote delivery acknowledgment was refused"
                        )
                    response_bytes = response.read(
                        MAX_REMOTE_AUTHORITY_RESPONSE_BYTES + 1
                    )
                    if len(response_bytes) > MAX_REMOTE_AUTHORITY_RESPONSE_BYTES:
                        raise DurableAuthorityBusy(
                            "remote delivery acknowledgment response is too large"
                        )
        except DurableAuthorityBusy:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise DurableAuthorityBusy(
                "remote delivery acknowledgment is unavailable or refused"
            ) from exc
        if (
            not isinstance(response_bytes, bytes)
            or len(response_bytes) > MAX_REMOTE_AUTHORITY_RESPONSE_BYTES
        ):
            raise DurableAuthorityBusy(
                "remote delivery acknowledgment response is too large"
            )
        try:
            response_value = json.loads(response_bytes.decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DurableAuthorityBusy(
                "remote delivery acknowledgment returned invalid JSON"
            ) from exc
        expected_keys = {
            "schema_version",
            "status",
            "receipt_artifact_bytes_base64",
            "receipt_artifact_sha256",
            "runtime_delivery_sequence",
            "delivered_at",
        }
        if not isinstance(response_value, dict) or set(response_value) != expected_keys:
            raise DurableAuthorityBusy(
                "remote delivery acknowledgment response has invalid schema"
            )
        if not (
            response_value["schema_version"]
            == "openadapt.production-delivery-acknowledgment-result/v2"
            and response_value["status"] == "acknowledged"
            and isinstance(response_value["receipt_artifact_sha256"], str)
            and re.fullmatch(r"[a-f0-9]{64}", response_value["receipt_artifact_sha256"])
            and type(response_value["runtime_delivery_sequence"]) is int
            and response_value["runtime_delivery_sequence"]
            == permit.runtime_delivery_sequence
            and isinstance(response_value["delivered_at"], str)
        ):
            raise DurableAuthorityBusy(
                "remote delivery acknowledgment does not match the pending edge"
            )
        receipt_bytes = self._exact_artifact_bytes(
            response_value["receipt_artifact_bytes_base64"],
            field="remote delivery receipt artifact bytes",
        )
        receipt_sha256 = hashlib.sha256(receipt_bytes).hexdigest()
        if receipt_sha256 != response_value["receipt_artifact_sha256"]:
            raise DurableAuthorityBusy(
                "remote delivery receipt artifact digest does not match response"
            )
        receipt_artifact = self._parse_receipt_artifact_bytes(receipt_bytes)
        try:
            entry = ProductionDeliveryPermit.build(
                permit.permit_artifact, receipt_artifact
            )
        except ValueError as exc:
            raise DurableAuthorityBusy(
                "remote delivery receipt does not bind the pending permit"
            ) from exc
        receipt_payload = receipt_artifact.payload
        if not (
            receipt_payload.one_use_claim_id == permit.one_use_claim_id
            and receipt_payload.runtime_delivery_sequence
            == permit.runtime_delivery_sequence
            and receipt_payload.delivered_at == response_value["delivered_at"]
        ):
            raise DurableAuthorityBusy(
                "remote delivery receipt does not bind the exact acknowledgment"
            )

        marker_payload: bytes | None = None
        with self._transaction() as connection:
            record = self._validate_pending_delivery_owner(
                connection,
                manifest,
                permit,
                attempt_id=attempt_id,
                owner_nonce_sha256=owner_nonce_sha256,
            )
            self._advance_remote_cursor(
                connection,
                authority_id=permit.authority_id,
                next_sequence=permit.next_sequence,
                cursor_secret=permit.cursor_secret,
            )
            try:
                connection.execute(
                    "INSERT INTO remote_delivery_artifacts "
                    "(path_key, runtime_delivery_sequence, "
                    "permit_artifact_sha256, permit_artifact_bytes, "
                    "receipt_artifact_sha256, receipt_artifact_bytes) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        self.path_key,
                        permit.runtime_delivery_sequence,
                        permit.permit_digest.removeprefix("sha256:"),
                        permit.permit_artifact_bytes,
                        receipt_sha256,
                        receipt_bytes,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise DurableAuthorityBusy(
                    "the production delivery artifact chain already changed"
                ) from exc
            deleted = connection.execute(
                "DELETE FROM remote_delivery_pending WHERE path_key = ? "
                "AND permit_artifact_sha256 = ?",
                (
                    self.path_key,
                    permit.permit_digest.removeprefix("sha256:"),
                ),
            )
            if deleted.rowcount != 1:
                raise DurableAuthorityBusy(
                    "the pending remote delivery changed before commit"
                )
            self._update(
                connection,
                record,
                delivery_sequence=record.delivery_sequence + 1,
            )
            marker_payload = self._synthetic_delivery_marker_payload(
                manifest,
                delivery_count=permit.input_edge_sequence,
                permit_count=permit.next_sequence,
                permit_digest=permit.permit_digest,
            )
        self._emit_synthetic_delivery_marker(marker_payload)
        return entry

    def production_delivery_permit_chain(
        self,
        *,
        allow_empty: bool = False,
        allow_pending: bool = False,
        receipt_absence_observed_at: str | None = None,
    ) -> ProductionDeliveryPermitChain:
        """Rebuild the exact retained chain without resolving uncertain delivery."""

        with self._transaction() as connection:
            pending = self._pending_remote_delivery(connection)
            if pending is not None and not allow_pending:
                raise DurableAuthorityBusy(
                    "production delivery remains uncertain without a receipt"
                )
            rows = connection.execute(
                "SELECT permit_artifact_bytes, receipt_artifact_bytes "
                "FROM remote_delivery_artifacts WHERE path_key = ? "
                "ORDER BY runtime_delivery_sequence",
                (self.path_key,),
            ).fetchall()
        if pending is not None and receipt_absence_observed_at is None:
            raise DurableAuthorityBusy(
                "pending production delivery requires an exact absence observation"
            )
        if not rows and pending is None:
            if allow_empty:
                return ProductionDeliveryPermitChain.build(())
            raise DurableAuthorityBusy(
                "the production delivery permit chain is unavailable"
            )
        entries = tuple(
            ProductionDeliveryPermit.build(
                self._parse_permit_artifact_bytes(bytes(row["permit_artifact_bytes"])),
                self._parse_receipt_artifact_bytes(
                    bytes(row["receipt_artifact_bytes"])
                ),
            )
            for row in rows
        )
        try:
            pending_entry = (
                ProductionPendingDeliveryPermit.build(
                    pending.permit_artifact,
                    receipt_absence_observed_at=receipt_absence_observed_at,
                )
                if pending is not None and receipt_absence_observed_at is not None
                else None
            )
            return ProductionDeliveryPermitChain.build(
                entries,
                pending=pending_entry,
            )
        except ValueError as exc:
            raise DurableAuthorityBusy(
                "the retained production delivery permit chain is invalid"
            ) from exc

    def _synthetic_delivery_marker_payload(
        self,
        manifest: Any,
        *,
        delivery_count: int,
        permit_count: int,
        permit_digest: str,
    ) -> bytes | None:
        """Build one bounded, non-secret marker for the exact synthetic run."""

        if self._synthetic_delivery_marker_sink is None:
            return None
        if os.getenv(SYNTHETIC_DELIVERY_MARKER_RUN_ID_ENV, "") != getattr(
            manifest, "remote_delivery_run_id", None
        ):
            return None
        run_id = getattr(manifest, "remote_delivery_run_id", None)
        binding = getattr(manifest, "managed_dispatch_binding_sha256", None)
        if not (
            isinstance(run_id, str)
            and _REMOTE_UUID_RE.fullmatch(run_id)
            and isinstance(binding, str)
            and _REMOTE_DIGEST_RE.fullmatch(binding)
            and type(delivery_count) is int
            and delivery_count > 0
            and type(permit_count) is int
            and permit_count > 0
            and _REMOTE_DIGEST_RE.fullmatch(permit_digest)
        ):
            return None
        marker = {
            "delivery_count": delivery_count,
            "managed_dispatch_binding_sha256": binding,
            "permit_count": permit_count,
            "permit_digest": permit_digest,
            "run_id": run_id,
            "schema_version": _SYNTHETIC_DELIVERY_MARKER_SCHEMA_VERSION,
        }
        payload = self._canonical(marker) + b"\n"
        if len(payload) > _MAX_SYNTHETIC_DELIVERY_MARKER_BYTES:
            return None
        return payload

    def _emit_synthetic_delivery_marker(self, payload: bytes | None) -> None:
        """Best-effort post-commit enqueue to the isolated observer.

        The consumer performs durable I/O. A full pipe, partial write, or sink
        failure must never alter a permit, delivery, or execution outcome.
        """

        sink = self._synthetic_delivery_marker_sink
        if payload is None or sink is None:
            return
        try:
            sink(payload)
        except Exception:  # noqa: BLE001 - observer failure is non-authoritative
            pass

    def before_delivery(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
    ) -> _RemoteDeliveryPermit | None:
        marker_payload: bytes | None = None
        with self._transaction() as connection:
            record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
            if record.reject_requested or record.attempt_phase not in {
                "validating",
                "delivery_started",
            }:
                raise DurableAuthorityBusy(
                    "the continuation was rejected or is not eligible for delivery"
                )
            if self.store.continuation_state_digest() != record.progress_digest:
                raise DurableAuthorityBusy(
                    "durable state changed before the delivery fence"
                )
            remote_authority_id, remote_sequence, remote_cursor = self._remote_cursor(
                connection
            )
            if self._pending_remote_delivery(connection) is not None:
                raise DurableAuthorityBusy(
                    "a prior production delivery lacks an acknowledgment receipt"
                )
            remote_permit = self._require_remote_delivery_permit(
                manifest,
                record,
                remote_authority_id=remote_authority_id,
                remote_delivery_sequence=remote_sequence,
                permit_cursor=remote_cursor,
            )
            self._consume_delivery_fence(record, manifest)
            if remote_permit is not None:
                self._retain_pending_remote_delivery(connection, remote_permit)
            self._update(
                connection,
                record,
                attempt_phase="delivery_started",
                delivery_sequence=(
                    record.delivery_sequence
                    if remote_permit is not None
                    else record.delivery_sequence + 1
                ),
            )
        self._emit_synthetic_delivery_marker(marker_payload)
        return remote_permit

    def before_initial_delivery(self, manifest: Any) -> _RemoteDeliveryPermit | None:
        """Consume the managed genesis permit immediately before any first input.

        The server-owned cursor is the cross-directory, cross-process authority.
        A copied envelope can therefore not start a second logical Cloud run.
        """

        marker_payload: bytes | None = None
        with self._transaction() as connection:
            record = self._read(connection)
            if (
                record is None
                or not self._identity_matches(record, manifest)
                or record.phase != "active"
            ):
                raise DurableAuthorityBusy(
                    "the managed initial delivery authority is unavailable"
                )
            if self.store.continuation_state_digest() != record.progress_digest:
                raise DurableAuthorityBusy(
                    "durable state changed before the initial delivery fence"
                )
            initial_record = record.model_copy(update={"operation": "initial"})
            remote_authority_id, remote_sequence, remote_cursor = self._remote_cursor(
                connection
            )
            if self._pending_remote_delivery(connection) is not None:
                raise DurableAuthorityBusy(
                    "a prior production delivery lacks an acknowledgment receipt"
                )
            remote_permit = self._require_remote_delivery_permit(
                manifest,
                initial_record,
                remote_authority_id=remote_authority_id,
                remote_delivery_sequence=remote_sequence,
                permit_cursor=remote_cursor,
            )
            self._consume_delivery_fence(record, manifest)
            if remote_permit is not None:
                self._retain_pending_remote_delivery(connection, remote_permit)
            self._update(
                connection,
                record,
                delivery_sequence=(
                    record.delivery_sequence
                    if remote_permit is not None
                    else record.delivery_sequence + 1
                ),
            )
        self._emit_synthetic_delivery_marker(marker_payload)
        return remote_permit

    def acknowledge_progress(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
        terminal_pause: bool,
        external_delivery: bool = False,
    ) -> str:
        with self._transaction() as connection:
            record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
            if record.attempt_phase not in {"validating", "delivery_started"}:
                raise DurableAuthorityBusy(
                    "the continuation cannot acknowledge progress in this phase"
                )
            # A continuation can make durable progress without an input edge.
            # Examples are an observation-only WAIT that becomes true and a
            # pre-delivery refusal that writes a new pause.  In those cases the
            # authority remains in ``validating`` because ``before_delivery``
            # never ran.  The owned attempt and changed state digest are the
            # required proof.  A real engine input edge still must pass through
            # ``before_delivery`` and therefore moves to ``delivery_started``.
            digest = self.store.continuation_state_digest()
            if digest == record.progress_digest:
                raise DurableAuthorityBusy(
                    "the continuation has no new durable progress to acknowledge"
                )
            pending = self.store.read_pending()
            pause_binding = ""
            if pending is not None:
                from openadapt_flow.runtime.durable.approval import (
                    approval_pause_digest,
                )

                pause_binding = approval_pause_digest(pending)
            self._update(
                connection,
                record,
                phase="paused" if terminal_pause else "continuing",
                progress_digest=digest,
                pause_binding_sha256=pause_binding,
                attempt_phase="validating",
                delivery_sequence=(
                    max(1, record.delivery_sequence)
                    if external_delivery
                    else record.delivery_sequence
                ),
            )
            return digest

    def request_reject(
        self,
        *,
        expected_run_id: str,
        expected_pause_binding: str,
    ) -> Literal["none", "preempted", "uncertain"]:
        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not record.attempt_id:
                return "none"
            if (
                record.run_id != expected_run_id
                or record.pause_binding_sha256 != expected_pause_binding
            ):
                raise DurableAuthorityBusy(
                    "the active continuation belongs to a different pause"
                )
            if (
                record.attempt_phase == "delivery_started"
                or record.delivery_sequence > 0
            ):
                self._update(
                    connection,
                    record,
                    phase="reconciliation_required",
                    attempt_phase="reconciliation_required",
                )
                return "uncertain"
            if record.attempt_phase != "validating":
                raise DurableAuthorityBusy(
                    "the continuation cannot be cleanly rejected"
                )
            self._update(connection, record, reject_requested=True)
            return "preempted"

    def mark_executor_uncertain(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
    ) -> None:
        """Fence an executor response that has no proven durable outcome."""

        with self._transaction() as connection:
            record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
            self._update(
                connection,
                record,
                phase="reconciliation_required",
                attempt_phase="reconciliation_required",
            )

    def prove_executor_outcome(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
        source_pause_binding: str,
    ) -> Optional[tuple[Literal["completed", "refused", "halted"], bool]]:
        """Derive an executor result from durable state, not its transport value."""

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy("durable authority identity changed")
            digest = self.store.continuation_state_digest()
            pending = self.store.read_pending()
            if record.phase == "completed":
                report_path = self.run_dir / "report.json"
                try:
                    report_sha256 = (
                        "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
                    )
                except OSError:
                    return None
                base_proof = (
                    record.pause_binding_sha256 == source_pause_binding
                    and record.attempt_phase == "none"
                    and not record.attempt_id
                    and pending is None
                    and record.progress_digest == digest
                    and bool(record.report_sha256)
                    and record.report_sha256 == report_sha256
                    and record.terminal_projection_state in {"settled", "corrected"}
                )
                if base_proof:
                    if record.terminal_projection_state == "settled":
                        return "completed", True
                    # A corrected report records a post-action persistence
                    # failure. It is durable evidence of a safe halt, not a
                    # completed executor result.
                    return "halted", False
                return None

            owned = (
                record.attempt_id == attempt_id
                and record.owner_nonce_sha256 == owner_nonce_sha256
            )
            if not owned or record.progress_digest != digest:
                return None
            if (
                record.phase == "paused"
                and record.attempt_phase == "validating"
                and pending is not None
                and pending.status == "pending"
                and approval_pause_digest(pending) != source_pause_binding
            ):
                return "halted", False
            if (
                record.phase == "continuing"
                and record.pause_binding_sha256 == source_pause_binding
                and record.attempt_phase == "validating"
                and record.delivery_sequence == 0
                and pending is not None
                and pending.status == "pending"
                and approval_pause_digest(pending) == source_pause_binding
            ):
                return "refused", False
            return None

    def prove_completed_pause(
        self,
        manifest: Any,
        *,
        source_pause_binding: str,
    ) -> bool:
        """Prove a terminal completion without reviving a dead executor lease."""

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy("durable authority identity changed")
            if record.phase != "completed":
                return False
            report_path = self.run_dir / "report.json"
            try:
                report_sha256 = (
                    "sha256:" + hashlib.sha256(report_path.read_bytes()).hexdigest()
                )
            except OSError:
                return False
            return bool(
                record.pause_binding_sha256 == source_pause_binding
                and record.attempt_phase == "none"
                and not record.attempt_id
                and self.store.read_pending() is None
                and record.progress_digest == self.store.continuation_state_digest()
                and record.report_sha256 == report_sha256
                and record.terminal_projection_state == "settled"
            )

    def attest_executor_outcome(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
        status: Literal["completed", "refused", "halted"],
        report_success: Optional[bool],
        source_pause_binding: str,
    ) -> None:
        """Derive an attended executor outcome from monotonic durable state."""
        proven = self.prove_executor_outcome(
            manifest,
            attempt_id=attempt_id,
            owner_nonce_sha256=owner_nonce_sha256,
            source_pause_binding=source_pause_binding,
        )
        if proven != (status, report_success):
            raise DurableAuthorityBusy(
                "the executor receipt has no matching durable authority proof"
            )

    def prepare_terminal(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
        report_sha256: str,
    ) -> None:
        with self._transaction() as connection:
            record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
            if record.reject_requested or record.attempt_phase != "validating":
                raise DurableAuthorityBusy(
                    "the continuation cannot enter terminal commit"
                )
            if self.store.continuation_state_digest() != record.progress_digest:
                raise DurableAuthorityBusy(
                    "durable state changed before terminal commit"
                )
            self._update(
                connection,
                record,
                phase="terminal_prepared",
                report_sha256=report_sha256,
                attempt_phase="terminal_prepared",
            )

    def finalize_terminal(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
        report_sha256: str,
    ) -> None:
        with self._transaction() as connection:
            record = self._owned(connection, manifest, attempt_id, owner_nonce_sha256)
            if (
                record.phase != "terminal_prepared"
                or record.attempt_phase != "terminal_prepared"
                or record.report_sha256 != report_sha256
            ):
                raise DurableAuthorityBusy(
                    "terminal authority was not prepared for this exact report"
                )
            if self.store.read_pending() is not None:
                raise DurableAuthorityBusy(
                    "the exact durable pause remains active at terminal commit"
                )
            digest = self.store.continuation_state_digest()
            self._update(
                connection,
                record,
                phase="completed",
                terminal_projection_state="pending",
                progress_digest=digest,
                attempt_id="",
                operation="",
                owner_nonce_sha256="",
                attempt_phase="none",
                reject_requested=False,
                approval_digest="",
                acquired_at="",
                expires_at="",
            )

    def settle_terminal_projection(
        self,
        manifest: Any,
        *,
        report_sha256: str,
    ) -> None:
        """Mark the exact completed report as projection-settled.

        A process crash before this transition leaves ``pending``.  Such a run
        remains non-attestable instead of appearing as a successful durable
        completion while its idempotency projection is unknown.
        """

        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy("durable authority identity changed")
            if (
                record.phase != "completed"
                or record.terminal_projection_state != "pending"
                or record.report_sha256 != report_sha256
            ):
                raise DurableAuthorityBusy(
                    "the completed terminal report is not pending this projection"
                )
            self._update(connection, record, terminal_projection_state="settled")

    def correct_terminal_projection_failure(
        self,
        manifest: Any,
        *,
        original_report_sha256: str,
        corrected_report_sha256: str,
        corrected_report_json: bytes,
    ) -> None:
        """Bind one fail-closed report replacement after projection failure.

        The correction is intentionally narrow. It can happen once, only from
        ``pending``, only after durable completion, and only for the typed
        idempotency-projection failure path. Both old and new report hashes
        remain in the monotonic authority record.
        """

        if not original_report_sha256 or not corrected_report_sha256:
            raise DurableAuthorityBusy("terminal report correction requires hashes")
        if original_report_sha256 == corrected_report_sha256:
            raise DurableAuthorityBusy("terminal report correction must change hash")
        actual_hash = "sha256:" + hashlib.sha256(corrected_report_json).hexdigest()
        if not hmac.compare_digest(actual_hash, corrected_report_sha256):
            raise DurableAuthorityBusy("terminal report correction hash mismatch")
        _validate_projection_failure_report(corrected_report_json, manifest)
        with self._transaction() as connection:
            record = self._read(connection)
            if record is None or not self._identity_matches(record, manifest):
                raise DurableAuthorityBusy("durable authority identity changed")
            if (
                record.phase != "completed"
                or record.terminal_projection_state != "pending"
                or record.report_sha256 != original_report_sha256
                or record.terminal_original_report_sha256
                or record.terminal_correction_reason
            ):
                raise DurableAuthorityBusy(
                    "the completed terminal report cannot accept this correction"
                )
            self._update(
                connection,
                record,
                report_sha256=corrected_report_sha256,
                terminal_projection_state="corrected",
                terminal_original_report_sha256=original_report_sha256,
                terminal_correction_reason="idempotency_projection_failure",
            )

    def release(
        self,
        manifest: Any,
        *,
        attempt_id: str,
        owner_nonce_sha256: str,
    ) -> None:
        with self._transaction() as connection:
            try:
                record = self._owned(
                    connection, manifest, attempt_id, owner_nonce_sha256
                )
            except DurableAuthorityBusy:
                return
            if record.phase in {"terminal_prepared", "reconciliation_required"}:
                return
            digest = self.store.continuation_state_digest()
            pending = self.store.read_pending()
            if pending is not None and pending.status == "rejected":
                self._update(
                    connection,
                    record,
                    phase="rejected",
                    progress_digest=digest,
                    attempt_id="",
                    operation="",
                    owner_nonce_sha256="",
                    attempt_phase="none",
                    reject_requested=False,
                    approval_digest="",
                    acquired_at="",
                    expires_at="",
                )
                return
            if record.attempt_phase == "delivery_started" or (
                digest != record.progress_digest
            ):
                self._update(
                    connection,
                    record,
                    phase="reconciliation_required",
                    attempt_phase="reconciliation_required",
                )
                return
            phase = "paused" if pending is not None else "active"
            self._update(
                connection,
                record,
                phase=phase,
                attempt_id="",
                operation="",
                owner_nonce_sha256="",
                attempt_phase="none",
                reject_requested=False,
                approval_digest="",
                acquired_at="",
                expires_at="",
            )

    def _owned(
        self,
        connection: sqlite3.Connection,
        manifest: Any,
        attempt_id: str,
        owner_nonce_sha256: str,
    ) -> DurableAuthorityRecord:
        record = self._read(connection)
        if (
            record is None
            or not self._identity_matches(record, manifest)
            or record.attempt_id != attempt_id
            or record.owner_nonce_sha256 != owner_nonce_sha256
        ):
            raise DurableAuthorityBusy(
                "the external continuation authority is no longer owned by this attempt"
            )
        return record
