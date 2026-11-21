"""One conservative completeness contract for native UIA value consumers.

A provider value is complete only when two identity-bound Unicode native
extent/full reads agree with two exact UIA reads. A disagreement is unavailable,
never an instruction to prefer one source. No names, normalization or truncation
are used. The caller also supplies its current focus/window identity guard.
"""

import ctypes
import hashlib
import time
from dataclasses import asdict, dataclass
from typing import Any, Callable, Protocol

MAX_UTF16_UNITS = 65536
PROOF_SCHEMA = "openadapt.native-value-completeness/v3"
IS_PASSWORD_PROPERTY = 30019
# Win32 EDIT password evidence: the ES_PASSWORD style and EM_GETPASSWORDCHAR.
ES_PASSWORD = 0x0020
EM_GETPASSWORDCHAR = 0x00D2
_EDIT_CLASSES = frozenset({"edit", "richedit20w", "richedit50w", "richedit60w"})


def utf16_bytes(value: str) -> bytes:
    return value.encode("utf-16-le", errors="surrogatepass")


class ValueUnavailable(Exception):
    """A specific absence of completeness evidence; never carries field text."""


@dataclass(frozen=True)
class NativeOwner:
    hwnd: int
    pid: int
    thread: int


@dataclass(frozen=True)
class NativeRead:
    owner: NativeOwner
    extent: int
    copied: int
    value: str


@dataclass(frozen=True)
class ValueProof:
    schema: str
    method: str
    hwnd: int
    pid: int
    thread: int
    utf16_units: int
    sha256_utf16le: str
    native_reads: int = 2
    uia_reads: int = 2

    def payload(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CompleteValue:
    complete: bool
    value: str | None
    reason: str | None
    proof: ValueProof | None


class NativeTextReader(Protocol):
    def owner(self, hwnd: int) -> NativeOwner: ...
    def read(self, owner: NativeOwner, *, guard: Callable[[], None]) -> NativeRead: ...


class Win32TextReader:
    """Bounded Unicode WM_GETTEXTLENGTH/WM_GETTEXT, without sending input.

    Only a real Unicode HWND owned by the bound UIA PID is accepted. The length
    may overestimate for some controls; exact copied count and UTF-16 length
    must agree or the observation is refused. Every message has a timeout.
    """

    def __init__(self) -> None:
        from ctypes import wintypes

        load = getattr(ctypes, "WinDLL", None)
        if load is None:
            raise ValueUnavailable("native_api_unavailable")
        self._user = load("user32", use_last_error=True)
        self._user.IsWindow.argtypes = [wintypes.HWND]
        self._user.IsWindow.restype = wintypes.BOOL
        self._user.IsWindowUnicode.argtypes = [wintypes.HWND]
        self._user.IsWindowUnicode.restype = wintypes.BOOL
        self._user.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        self._user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self._user.SendMessageTimeoutW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        self._user.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        self._user.GetClassNameW.argtypes = [
            wintypes.HWND,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        self._user.GetClassNameW.restype = ctypes.c_int
        self._user.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user.GetWindowLongW.restype = wintypes.LONG

    def owner(self, hwnd: int) -> NativeOwner:
        from ctypes import wintypes

        if not self._user.IsWindow(hwnd) or not self._user.IsWindowUnicode(hwnd):
            raise ValueUnavailable("native_window_unavailable_or_not_unicode")
        pid = wintypes.DWORD()
        thread = self._user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not thread or not pid.value:
            raise ValueUnavailable("native_owner_unavailable")
        return NativeOwner(hwnd, int(pid.value), int(thread))

    def _message(
        self, hwnd: int, message: int, count: int = 0, pointer: int = 0
    ) -> int:
        result = ctypes.c_size_t()
        # BLOCK | ABORTIFHUNG | ERRORONEXIT. No input, focus or value mutation.
        accepted = self._user.SendMessageTimeoutW(
            hwnd, message, count, pointer, 0x23, 1000, ctypes.byref(result)
        )
        if not accepted:
            raise ValueUnavailable("native_text_timeout_or_failure")
        return int(result.value)

    def plain_edit(self, hwnd: int) -> bool:
        """True only for a Unicode EDIT window that shows its text.

        Its class is a Win32 or WinForms EDIT/RichEdit class, its style has no
        ES_PASSWORD, and EM_GETPASSWORDCHAR answers 0 within the timeout. Any
        other class, a failed call or a password character is False.
        """
        self.owner(hwnd)
        name = ctypes.create_unicode_buffer(257)
        if not 0 < self._user.GetClassNameW(hwnd, name, 257) < 257:
            return False
        if not _edit_class(name.value):
            return False
        if self._user.GetWindowLongW(hwnd, -16) & ES_PASSWORD:  # GWL_STYLE
            return False
        return self._message(hwnd, EM_GETPASSWORDCHAR) == 0

    def read(self, owner: NativeOwner, *, guard: Callable[[], None]) -> NativeRead:
        def unchanged() -> None:
            if self.owner(owner.hwnd) != owner:
                raise ValueUnavailable("native_owner_changed")
            # Extent and full-text messages are separate provider calls.
            # Recheck sensitivity after ownership and before each message.
            guard()

        unchanged()
        extent = self._message(owner.hwnd, 0x000E)
        unchanged()
        if not 0 <= extent <= MAX_UTF16_UNITS:
            raise ValueUnavailable("value_exceeds_limit")
        # Win32 WCHAR is always two bytes, including when protocol-tested on a
        # host where ctypes.c_wchar is four bytes. Decode exact copied units.
        buffer = (ctypes.c_uint16 * (extent + 1))()
        copied = self._message(owner.hwnd, 0x000D, extent + 1, ctypes.addressof(buffer))
        unchanged()
        if copied != extent or buffer[extent] != 0:
            raise ValueUnavailable("native_extent_or_copy_incomplete")
        value = ctypes.string_at(ctypes.addressof(buffer), copied * 2).decode(
            "utf-16-le", errors="surrogatepass"
        )
        if len(utf16_bytes(value)) // 2 != extent or "\x00" in value:
            raise ValueUnavailable("native_extent_or_copy_incomplete")
        return NativeRead(owner, extent, copied, value)


def _edit_class(name: str) -> bool:
    """A Win32 EDIT/RichEdit class, or WinForms' ``WindowsForms10.EDIT.app...``."""
    parts = name.casefold().split(".")
    if len(parts) > 2 and parts[0].startswith("windowsforms") and parts[2] == "app":
        return parts[1] in _EDIT_CLASSES
    return len(parts) == 1 and parts[0] in _EDIT_CLASSES


_PASSWORD_EVIDENCE: list[Any] = []


def _native_plain_edit(control: Any) -> bool:
    """Win32 evidence for a provider that does not support IsPassword."""
    hwnd = control.NativeWindowHandle
    if not _positive_int(hwnd):
        return False
    if not _PASSWORD_EVIDENCE:
        _PASSWORD_EVIDENCE.append(Win32TextReader())
    reader = _PASSWORD_EVIDENCE[0]
    plain = getattr(reader, "plain_edit", None)
    return callable(plain) and plain(hwnd) is True


def _positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _identity(control: Any) -> tuple[int, int, tuple[int, ...]]:
    hwnd, pid = control.NativeWindowHandle, control.ProcessId
    runtime = control.GetRuntimeId()
    if (
        not _positive_int(hwnd)
        or not _positive_int(pid)
        or not isinstance(runtime, (tuple, list))
        or not 1 <= len(runtime) <= 64
        or any(type(item) is not int for item in runtime)
    ):
        raise ValueUnavailable("native_identity_unavailable")
    return hwnd, pid, tuple(runtime)


def require_non_sensitive_value(control: Any) -> None:
    """Refuse text access unless the current password status is exactly False.

    Missing, exceptional and non-boolean states do not authorize a getter.
    All value, capability and preview consumers share this rule. Call again
    after any provider call and immediately before each text getter; a prior
    result or negotiated protocol version is never authority for a later read.
    """
    try:
        # CurrentIsPassword/Control.IsPassword can return a default FALSE for
        # unsupported status. Request the raw property with defaults ignored;
        # a reserved unsupported object is unknown, not non-sensitive. Some
        # providers (the WinForms TextBox proxies) do not support IsPassword
        # at all; then only Win32 evidence from the control's own EDIT window
        # (no ES_PASSWORD style, no password character) allows the read.
        method = getattr(control, "GetPropertyValueEx", None)
        if not callable(method):
            raise ValueUnavailable("password_status_unavailable")
        raw = method(IS_PASSWORD_PROPERTY, 1)
        if type(raw) is bool:
            allowed = raw is False
        else:
            allowed = _native_plain_edit(control)
    except Exception:
        allowed = False
    if not allowed:
        raise ValueUnavailable("password_or_password_status_unavailable")


def observe_complete_value(
    control: Any,
    *,
    identity: Callable[[], str | None],
    require_editable: bool = False,
    native_reader: NativeTextReader | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> CompleteValue:
    """Observe exactly one stable value or return an explicit incomplete result.

    ``identity`` must re-read the caller's bound target scope (including actual
    focus for editable input). Returning None or a changed identity refuses.
    The helper separately re-reads UIA HWND/PID/RuntimeId and native ownership
    around every source read. It never upgrades either disagreeing source.
    """
    try:
        require_non_sensitive_value(control)
        deadline = clock() + 5.0
        binding = identity()
        if not isinstance(binding, str) or not 1 <= len(binding) <= 256:
            raise ValueUnavailable("bound_identity_unavailable")
        bound = _identity(control)
        reader = native_reader if native_reader is not None else Win32TextReader()
        owner = reader.owner(bound[0])
        if (
            owner.hwnd != bound[0]
            or owner.pid != bound[1]
            or not _positive_int(owner.thread)
        ):
            raise ValueUnavailable("native_owner_disagreement")

        def guard() -> None:
            if clock() >= deadline:
                raise ValueUnavailable("value_observation_deadline")
            if (
                identity() != binding
                or _identity(control) != bound
                or reader.owner(bound[0]) != owner
            ):
                raise ValueUnavailable("value_identity_changed")
            if (
                require_editable
                and getattr(control, "ControlTypeName", None) != "EditControl"
            ):
                raise ValueUnavailable("not_editable_value_control")
            # This MUST be last: any preceding provider metadata/identity
            # getter can change the sensitive status before the next access.
            require_non_sensitive_value(control)

        def uia() -> str:
            guard()
            method = control.GetValuePattern
            guard()
            pattern = method()
            guard()
            read_only = getattr(pattern, "IsReadOnly", None)
            guard()
            if type(read_only) is not bool or (require_editable and read_only):
                raise ValueUnavailable("read_only_or_read_only_status_unavailable")
            value = getattr(pattern, "Value", None)
            guard()
            if not isinstance(value, str):
                raise ValueUnavailable("value_pattern_unavailable")
            if len(utf16_bytes(value)) // 2 > MAX_UTF16_UNITS:
                raise ValueUnavailable("value_exceeds_limit")
            if getattr(pattern, "IsReadOnly", None) is not read_only:
                raise ValueUnavailable("value_readability_changed")
            guard()
            return value

        def native() -> NativeRead:
            guard()
            result = reader.read(owner, guard=guard)
            guard()
            units = len(utf16_bytes(result.value)) // 2
            if not 0 <= result.extent <= MAX_UTF16_UNITS:
                raise ValueUnavailable("value_exceeds_limit")
            if (
                result.owner != owner
                or result.extent != result.copied
                or units != result.extent
            ):
                raise ValueUnavailable("native_extent_or_copy_incomplete")
            return result

        # Native extent is read first so oversized storage cannot be hidden by
        # a short provider prefix. Sources bracket one another twice.
        before = native()
        first = uia()
        after = native()
        second = uia()
        guard()
        if before != after or first != second:
            raise ValueUnavailable("value_observation_changed")
        if utf16_bytes(first) != utf16_bytes(before.value):
            raise ValueUnavailable("provider_native_value_disagreement")
        proof = ValueProof(
            PROOF_SCHEMA,
            "uia-value+wm-gettext",
            owner.hwnd,
            owner.pid,
            owner.thread,
            before.extent,
            hashlib.sha256(utf16_bytes(first)).hexdigest(),
        )
        return CompleteValue(True, first, None, proof)
    except ValueUnavailable as exc:
        return CompleteValue(False, None, str(exc), None)
    except Exception:
        # COM/ctypes/provider errors never expose a partial value or a Name.
        return CompleteValue(False, None, "value_observation_unavailable", None)


def valid_value_proof(value: str, proof: object) -> bool:
    """Strict shared wire validation; a legacy complete flag is insufficient."""
    if not isinstance(proof, dict) or set(proof) != set(
        ValueProof.__dataclass_fields__
    ):
        return False
    raw = utf16_bytes(value)
    return (
        proof["schema"] == PROOF_SCHEMA
        and proof["method"] == "uia-value+wm-gettext"
        and all(_positive_int(proof[key]) for key in ("hwnd", "pid", "thread"))
        and type(proof["utf16_units"]) is int
        and 0 <= proof["utf16_units"] <= MAX_UTF16_UNITS
        and proof["utf16_units"] == len(raw) // 2
        and proof["sha256_utf16le"] == hashlib.sha256(raw).hexdigest()
        and type(proof["native_reads"]) is int
        and proof["native_reads"] == 2
        and type(proof["uia_reads"]) is int
        and proof["uia_reads"] == 2
    )
