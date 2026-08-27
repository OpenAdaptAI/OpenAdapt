"""Platform-neutral contracts for native global input observation."""

from __future__ import annotations

import queue
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, TypeAlias


class InputObserverError(RuntimeError):
    """Base error for fail-loud input observation."""


class _InputObserverStartupCancelledError(InputObserverError):
    """Internal marker for setup failures caused by the parent's startup abort."""


class InputObserverUnavailableError(InputObserverError):
    """The current platform/session cannot provide complete input observation."""


class InputObserverPermissionError(InputObserverError):
    """The operating system denied global input-observation permission."""


@dataclass(frozen=True, slots=True)
class ObservedMouseMove:
    """A global mouse-pointer movement in logical screen coordinates."""

    x: float
    y: float
    injected: bool = False
    timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class ObservedMouseButton:
    """A global mouse-button transition."""

    x: float
    y: float
    button: str
    pressed: bool
    injected: bool = False
    timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class ObservedMouseScroll:
    """A global scroll event using platform-normalized wheel units."""

    x: float
    y: float
    dx: float
    dy: float
    injected: bool = False
    timestamp: float | None = None


@dataclass(frozen=True, slots=True)
class ObservedKey:
    """A global key transition with both physical and canonical identity."""

    pressed: bool
    key_name: str | None = None
    key_char: str | None = None
    key_vk: str | None = None
    canonical_key_name: str | None = None
    canonical_key_char: str | None = None
    canonical_key_vk: str | None = None
    injected: bool = False
    timestamp: float | None = None


ObservedInput: TypeAlias = (
    ObservedMouseMove | ObservedMouseButton | ObservedMouseScroll | ObservedKey
)
InputCallback: TypeAlias = Callable[[ObservedInput], None]


@dataclass(frozen=True, slots=True)
class _ObservedDelivery:
    """One normalized event plus its native-receipt reservation."""

    event: ObservedInput
    receipt: object | None


@dataclass(slots=True)
class _ReceiptFrameCapture:
    """One platform-neutral cut around window-pixel acquisition."""

    start_receipt_count: int
    start_callback_generation: int
    started_with_active_callback: bool
    finish_called: bool = False
    cut_held: bool = False
    completed: bool = False


_SEALED_FRAME_RECEIPT = object()


def add_exception_note(error: BaseException, note: str) -> None:
    """Attach cleanup context when supported without masking the primary error."""
    add_note = getattr(error, "add_note", None)
    if add_note is not None:
        add_note(note)


class InputObserver(ABC):
    """Lifecycle contract for a complete native input observer."""

    @abstractmethod
    def start(self) -> None:
        """Start observing, or raise before returning if setup is incomplete."""

    @abstractmethod
    def check_health(self) -> None:
        """Raise if the observer failed after startup."""

    @abstractmethod
    def stop(self) -> None:
        """Stop and join the observer, surfacing any observer failure."""

    def begin_frame_capture(self) -> object | None:
        """Start an input-stable frame capture transaction."""
        return None

    def finish_frame_capture(self, token: object | None) -> bool:
        """Close the capture interval and report whether it contained input."""
        del token
        return True

    def complete_frame_capture(self, token: object | None) -> None:
        """Release an input-stable frame after it is published or discarded."""
        del token

    def seal_frame_capture(self, token: object | None) -> None:
        """Stop accepting native input at an exact terminal-frame boundary."""
        del token

    def __enter__(self) -> "InputObserver":
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.stop()


class ThreadedInputObserver(InputObserver):
    """Shared fail-loud lifecycle for native event-loop implementations."""

    def __init__(
        self,
        callback: InputCallback,
        *,
        observe_keyboard: bool,
        observe_mouse: bool,
        capture_mouse_moves: bool,
        startup_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
        delivery_queue_size: int = 4096,
    ) -> None:
        if not observe_keyboard and not observe_mouse:
            raise ValueError("at least one of observe_keyboard or observe_mouse is required")
        self.callback = callback
        self.observe_keyboard = observe_keyboard
        self.observe_mouse = observe_mouse
        self.capture_mouse_moves = capture_mouse_moves
        self.startup_timeout = startup_timeout
        self.shutdown_timeout = shutdown_timeout
        if delivery_queue_size <= 0:
            raise ValueError("delivery_queue_size must be positive")
        self.delivery_queue_size = delivery_queue_size
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread: threading.Thread | None = None
        self._delivery_thread: threading.Thread | None = None
        self._delivery_queue: queue.Queue[_ObservedDelivery | object] = queue.Queue(
            maxsize=delivery_queue_size
        )
        self._delivery_sentinel = object()
        self._delivery_ready = threading.Event()
        self._delivery_stop_requested = threading.Event()
        self._delivery_decided = threading.Event()
        self._delivery_state_lock = threading.Lock()
        self._delivery_state = "inactive"
        self._failure: BaseException | None = None
        self._failure_lock = threading.Lock()
        self._startup_failure: BaseException | None = None
        self._receipt_lock = threading.Lock()
        self._unqueued_receipts: list[object] = []
        self._input_receipt_count = 0
        self._native_callback_generation = 0
        self._active_native_callbacks = 0
        self._capture_sealed = False

    @abstractmethod
    def _setup(self) -> None:
        """Create platform resources on the observer thread."""

    @abstractmethod
    def _run_loop(self) -> None:
        """Run until ``_stop_requested`` is set."""

    @abstractmethod
    def _teardown(self) -> None:
        """Release any resources created by ``_setup``."""

    def _wake(self) -> None:
        """Wake a blocked event loop during shutdown, when needed."""

    def _reserve_receipt(self, timestamp: float | None) -> object | None:
        """Reserve source order at the first native receipt boundary."""
        reserve = getattr(self.callback, "_openadapt_input_receipt", None)
        with self._receipt_lock:
            if self._capture_sealed:
                return _SEALED_FRAME_RECEIPT
            self._input_receipt_count += 1
            if not callable(reserve):
                return None
            if timestamp is None:
                raise InputObserverError(
                    "native input has no receipt timestamp for source-order reservation"
                )
            receipt = reserve(timestamp)
            if receipt is not None:
                self._unqueued_receipts.append(receipt)
        return receipt

    def _mark_native_activity(self) -> None:
        """Make an unrecorded native transition invalidate an in-flight frame."""
        with self._receipt_lock:
            if not self._capture_sealed:
                self._input_receipt_count += 1

    def _begin_native_callback(self) -> None:
        """Mark an OS input callback active before it can deliver its input."""
        with self._receipt_lock:
            self._native_callback_generation += 1
            self._active_native_callbacks += 1

    def _end_native_callback(self) -> None:
        """Close an OS input callback only after its downstream hook returns."""
        with self._receipt_lock:
            if self._active_native_callbacks <= 0:
                raise InputObserverError(
                    "native input callback completion has no matching start"
                )
            self._active_native_callbacks -= 1
            self._native_callback_generation += 1

    @staticmethod
    def _fail_receipt(receipt: object | None, failure: BaseException) -> None:
        """Release a reserved consumer position after observation fails."""
        if receipt is None:
            return
        fail = getattr(receipt, "fail", None)
        if callable(fail):
            fail(failure)

    def _forget_unqueued_receipt(self, receipt: object | None) -> None:
        if receipt is None:
            return
        with self._receipt_lock:
            for index, pending in enumerate(self._unqueued_receipts):
                if pending is receipt:
                    self._unqueued_receipts.pop(index)
                    return

    def _fail_unqueued_receipts(self, failure: BaseException) -> int:
        """Fail reservations that normalization has not queued for delivery."""
        with self._receipt_lock:
            pending = self._unqueued_receipts
            self._unqueued_receipts = []
        for receipt in pending:
            self._fail_receipt(receipt, failure)
        return len(pending)

    def _emit(
        self,
        event: ObservedInput,
        *,
        receipt: object | None = None,
    ) -> None:
        if receipt is _SEALED_FRAME_RECEIPT:
            return
        if receipt is None:
            with self._receipt_lock:
                if self._capture_sealed:
                    return
        with self._delivery_state_lock:
            if self._delivery_state in {"aborted", "inactive"}:
                if receipt is None:
                    return
                failure = InputObserverError(
                    f"{type(self).__name__} discarded reserved input after "
                    "delivery stopped"
                )
                self._forget_unqueued_receipt(receipt)
                self._fail_receipt(receipt, failure)
                raise failure

        # Keep the state check and enqueue atomic with startup cancellation.
        # Otherwise an abort could drain the queue and exit its delivery thread
        # between these two operations, leaving a late setup event stranded.
        failure: InputObserverError | None = None
        with self._delivery_state_lock:
            if self._delivery_state in {"aborted", "inactive"}:
                failure = InputObserverError(
                    f"{type(self).__name__} discarded input after delivery stopped"
                )
            else:
                self._forget_unqueued_receipt(receipt)
                try:
                    self._delivery_queue.put_nowait(
                        _ObservedDelivery(event, receipt)
                    )
                except queue.Full:
                    failure = InputObserverError(
                        f"{type(self).__name__} input delivery queue overflowed; "
                        "recording coverage is incomplete"
                    )
        if failure is not None:
            self._fail_receipt(receipt, failure)
            # Wake hooks are platform-defined and may re-enter lifecycle code;
            # never call them while holding the delivery-state lock.
            self._fail(failure)
            raise failure

    def _emit_received(
        self,
        event: ObservedInput,
        receipt: object | None,
    ) -> None:
        """Queue one normalized event with its optional receipt reservation."""
        if receipt is _SEALED_FRAME_RECEIPT:
            return
        if receipt is None:
            self._emit(event)
        else:
            self._emit(event, receipt=receipt)

    def begin_frame_capture(self) -> object:
        """Snapshot accepted input before the screen reader acquires pixels."""
        self.check_health()
        with self._receipt_lock:
            if self._capture_sealed:
                raise InputObserverError(
                    "native input was already sealed before frame capture"
                )
            return _ReceiptFrameCapture(
                start_receipt_count=self._input_receipt_count,
                start_callback_generation=self._native_callback_generation,
                started_with_active_callback=self._active_native_callbacks > 0,
            )

    def finish_frame_capture(self, token: object | None) -> bool:
        """Reject an input-dirty frame and hold a clean cut through commit."""
        if not isinstance(token, _ReceiptFrameCapture):
            raise InputObserverError("native input received an invalid frame token")
        if token.finish_called or token.completed:
            raise InputObserverError("native input frame token was already finished")
        if not self._receipt_lock.acquire(timeout=self.shutdown_timeout):
            raise InputObserverError(
                "native input could not close the frame cut before timeout"
            )
        token.finish_called = True
        token.cut_held = True
        try:
            self.check_health()
            if (
                token.started_with_active_callback
                or self._active_native_callbacks > 0
                or self._native_callback_generation
                != token.start_callback_generation
                or self._input_receipt_count != token.start_receipt_count
            ):
                token.cut_held = False
                self._receipt_lock.release()
                return False
            return True
        except BaseException:
            token.cut_held = False
            self._receipt_lock.release()
            raise

    def complete_frame_capture(self, token: object | None) -> None:
        """Release native callbacks after a clean frame enters the journal."""
        if not isinstance(token, _ReceiptFrameCapture) or token.completed:
            return
        token.completed = True
        if token.cut_held:
            token.cut_held = False
            self._receipt_lock.release()

    def seal_frame_capture(self, token: object | None) -> None:
        """Close native receipt acceptance before a terminal frame commits."""
        if isinstance(token, _ReceiptFrameCapture):
            if not token.cut_held or token.completed:
                raise InputObserverError(
                    "terminal input seal requires an active clean frame cut"
                )
            self._capture_sealed = True
            return
        with self._receipt_lock:
            self._capture_sealed = True

    def _fail(self, failure: BaseException) -> None:
        primary = self._store_failure(failure)
        self._fail_unqueued_receipts(primary)
        self._stop_requested.set()
        try:
            self._wake()
        except BaseException as wake_error:
            add_exception_note(
                primary,
                f"waking the failed observer also failed: {wake_error}",
            )

    def _store_failure(self, failure: BaseException) -> BaseException:
        """Record and return the first failure atomically."""
        with self._failure_lock:
            if self._failure is None:
                self._failure = failure
            primary = self._failure
        return primary

    def _delivery_main(self) -> None:
        # Native setup may need to process events before it can prove that the
        # observation boundary is ready. Preserve those events, but do not make
        # them externally visible until the parent start() transaction commits.
        self._delivery_decided.wait()
        with self._delivery_state_lock:
            delivery_state = self._delivery_state
        if delivery_state == "aborted":
            self._discard_delivery_queue()
            return
        if delivery_state != "committed":
            self._abort_failed_delivery(
                InputObserverError(
                    f"{type(self).__name__} delivery started without a valid "
                    "startup decision"
                )
            )
            return
        start_hook = getattr(
            self.callback,
            "_openadapt_delivery_thread_start",
            None,
        )
        stop_hook = getattr(
            self.callback,
            "_openadapt_delivery_thread_stop",
            None,
        )
        try:
            if callable(start_hook):
                start_hook()
            self._delivery_ready.set()
            while True:
                if (
                    self._delivery_stop_requested.is_set()
                    and self._delivery_queue.empty()
                ):
                    return
                try:
                    item = self._delivery_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                try:
                    if item is self._delivery_sentinel:
                        return
                    if not isinstance(item, _ObservedDelivery):
                        raise InputObserverError(
                            f"{type(self).__name__} delivery queue contained "
                            "an invalid item"
                        )
                    if item.receipt is None:
                        self.callback(item.event)
                    else:
                        deliver = getattr(
                            self.callback,
                            "_openadapt_input_delivery",
                            None,
                        )
                        if not callable(deliver):
                            raise InputObserverError(
                                "a native input receipt was reserved without "
                                "a matching delivery consumer"
                            )
                        deliver(item.event, item.receipt)
                        if not bool(getattr(item.receipt, "finished", False)):
                            raise InputObserverError(
                                "the input consumer returned without completing "
                                "its native receipt reservation"
                            )
                except BaseException as exc:
                    failure = (
                        exc
                        if isinstance(exc, InputObserverError)
                        else InputObserverError(
                            f"{type(self).__name__} input consumer failed: {exc}"
                        )
                    )
                    if isinstance(item, _ObservedDelivery):
                        self._fail_receipt(item.receipt, failure)
                    self._abort_failed_delivery(failure)
                    return
                finally:
                    self._delivery_queue.task_done()
        except BaseException as exc:
            failure = (
                exc
                if isinstance(exc, InputObserverError)
                else InputObserverError(
                    f"{type(self).__name__} input delivery setup failed: {exc}"
                )
            )
            self._abort_failed_delivery(failure)
        finally:
            if callable(stop_hook):
                try:
                    stop_hook()
                except BaseException as exc:
                    self._fail(
                        InputObserverError(
                            f"{type(self).__name__} input delivery cleanup failed: {exc}"
                        )
                    )

    def _abort_failed_delivery(self, failure: BaseException) -> None:
        """Close delivery and fail every receipt that cannot reach its consumer."""
        with self._delivery_state_lock:
            self._delivery_state = "aborted"
            self._delivery_decided.set()
        self._delivery_stop_requested.set()
        self._fail(failure)
        self._discard_delivery_queue(failure)
        self._delivery_ready.set()

    def _discard_delivery_queue(
        self,
        failure: BaseException | None = None,
    ) -> None:
        """Discard every event buffered by a startup that did not commit."""
        if failure is None:
            failure = InputObserverError(
                f"{type(self).__name__} discarded input from an aborted startup"
            )
        while True:
            try:
                item = self._delivery_queue.get_nowait()
            except queue.Empty:
                return
            else:
                if isinstance(item, _ObservedDelivery):
                    self._fail_receipt(item.receipt, failure)
                self._delivery_queue.task_done()

    def _abort_delivery_start(self) -> None:
        """Prevent setup-time events from escaping a failed start transaction."""
        with self._delivery_state_lock:
            if self._delivery_state == "pending":
                self._delivery_state = "aborted"
                self._delivery_decided.set()

    def _commit_delivery_start(self) -> None:
        """Publish setup-time events after readiness and health are proven."""
        with self._delivery_state_lock:
            if self._delivery_state != "pending":
                raise InputObserverError(
                    f"{type(self).__name__} cannot commit input delivery from "
                    f"state {self._delivery_state!r}"
                )
            self._delivery_state = "committed"
            self._delivery_decided.set()

    def _delivery_start_was_aborted(self) -> bool:
        """Return whether the current startup transaction was cancelled."""
        with self._delivery_state_lock:
            return self._delivery_state == "aborted"

    def _start_delivery(self) -> None:
        if self._delivery_thread is not None:
            state = "live" if self._delivery_thread.is_alive() else "stopped"
            raise InputObserverError(
                f"{type(self).__name__} has a {state} delivery thread from a "
                "previous lifecycle; stop it or create a new observer"
            )
        self._delivery_queue = queue.Queue(maxsize=self.delivery_queue_size)
        self._delivery_ready.clear()
        self._delivery_stop_requested.clear()
        self._delivery_decided.clear()
        with self._delivery_state_lock:
            self._delivery_state = "pending"
        thread = threading.Thread(
            target=self._delivery_main,
            name=f"{type(self).__name__}-delivery",
            daemon=True,
        )
        self._delivery_thread = thread
        try:
            thread.start()
        except BaseException:
            with self._delivery_state_lock:
                self._delivery_state = "inactive"
            self._delivery_thread = None
            raise

    def _stop_delivery(self) -> None:
        thread = self._delivery_thread
        if thread is None:
            return
        self._abort_delivery_start()
        self._delivery_stop_requested.set()
        if thread is threading.current_thread():
            # A consumer callback may intentionally stop its listener (for
            # example after matching the configured stop sequence). Returning
            # lets the callback unwind; the delivery loop then drains events
            # already received before exiting without attempting a self-join.
            return
        with self._delivery_state_lock:
            delivery_state = self._delivery_state
        if thread.is_alive() and delivery_state == "committed":
            try:
                self._delivery_queue.put(
                    self._delivery_sentinel,
                    timeout=self.shutdown_timeout,
                )
            except queue.Full:
                self._fail(
                    InputObserverError(
                        f"{type(self).__name__} delivery queue could not drain "
                        "during shutdown"
                    )
                )
            thread.join(self.shutdown_timeout)
        elif thread.is_alive():
            # A rejected start wakes the delivery thread through
            # _delivery_decided and discards its buffered events.
            thread.join(self.shutdown_timeout)
        if thread.is_alive():
            self._fail(
                InputObserverError(
                    f"{type(self).__name__} delivery thread did not stop within "
                    f"{self.shutdown_timeout:.1f}s"
                )
            )
        else:
            self._delivery_thread = None
            with self._delivery_state_lock:
                self._delivery_state = "inactive"

    def _thread_main(self) -> None:
        try:
            self._setup()
            self._ready.set()
            self._run_loop()
        except BaseException as exc:
            with self._failure_lock:
                if self._failure is None:
                    self._failure = exc
            self._ready.set()
        finally:
            try:
                self._teardown()
            except BaseException as exc:
                self._store_failure(exc)

    def start(self) -> None:
        if self._thread is not None:
            if self._thread.is_alive():
                if self._startup_failure is not None:
                    raise InputObserverError(
                        f"{type(self).__name__} still has a live thread from a "
                        "failed startup; create a new observer after fixing the cause"
                    ) from self._startup_failure
                self.check_health()
                return
            self._stop_delivery()
            self._thread = None
            self.check_health()
            raise InputObserverError(
                f"{type(self).__name__} cannot restart after its event loop "
                "stopped unexpectedly; create a new observer"
            )
        if self._delivery_thread is not None:
            state = "live" if self._delivery_thread.is_alive() else "stopped"
            raise InputObserverError(
                f"{type(self).__name__} still has a {state} delivery thread "
                "from a previous lifecycle; stop it or create a new observer"
            )
        self._ready.clear()
        self._stop_requested.clear()
        self._failure = None
        self._startup_failure = None
        try:
            self._start_delivery()
        except BaseException as exc:
            self._startup_failure = exc
            self._stop_delivery()
            raise
        self._thread = threading.Thread(
            target=self._thread_main,
            name=f"{type(self).__name__}-event-loop",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException as exc:
            self._startup_failure = exc
            self._thread = None
            self._stop_delivery()
            raise
        if not self._ready.wait(self.startup_timeout):
            primary = InputObserverError(
                f"{type(self).__name__} did not become ready within "
                f"{self.startup_timeout:.1f}s"
            )
            self._abort_start(primary, prefer_observer_failure=True)
        try:
            self.check_health()
        except BaseException as exc:
            self._abort_start(exc)
        try:
            self._commit_delivery_start()
        except BaseException as exc:
            self._abort_start(exc)
        if not self._delivery_ready.wait(self.startup_timeout):
            self._abort_start(
                InputObserverError(
                    f"{type(self).__name__} delivery setup did not become ready within "
                    f"{self.startup_timeout:.1f}s"
                )
            )
        try:
            self.check_health()
        except BaseException as exc:
            self._abort_start(exc)

    def _abort_start(
        self,
        primary: BaseException,
        *,
        prefer_observer_failure: bool = False,
    ) -> None:
        """Make a failed ``start`` transactional before surfacing its cause."""
        self._startup_failure = primary
        self._abort_delivery_start()
        thread = self._thread
        if thread is None:
            self._stop_delivery()
            raise primary
        self._stop_requested.set()
        try:
            self._wake()
        except BaseException as cleanup_exc:
            add_exception_note(
                primary,
                f"observer wake during failed startup also failed: {cleanup_exc}"
            )
        thread.join(self.shutdown_timeout)
        if prefer_observer_failure:
            # A setup failure can race with the readiness deadline.  Teardown
            # must still complete transactionally, but once the observer
            # thread has reported its concrete cause it is more useful and
            # accurate than the parent thread's generic timeout.  A clean
            # cancellation (the genuinely-never-ready case) retains the
            # timeout above.
            with self._failure_lock:
                observer_failure = self._failure
            if (
                observer_failure is not None
                and not isinstance(
                    observer_failure,
                    _InputObserverStartupCancelledError,
                )
            ):
                if isinstance(observer_failure, InputObserverError):
                    primary = observer_failure
                else:
                    wrapped = InputObserverError(
                        f"{type(self).__name__} failed: {observer_failure}"
                    )
                    wrapped.__cause__ = observer_failure
                    primary = wrapped
                self._startup_failure = primary
        if thread.is_alive():
            add_exception_note(
                primary,
                f"{type(self).__name__} setup thread did not stop within "
                f"{self.shutdown_timeout:.1f}s",
            )
        else:
            self._thread = None
        incomplete_receipt = InputObserverError(
            f"{type(self).__name__} stopped before reserved native input "
            "reached delivery"
        )
        if self._fail_unqueued_receipts(incomplete_receipt):
            self._store_failure(incomplete_receipt)
        self._stop_delivery()
        raise primary

    def check_health(self) -> None:
        with self._failure_lock:
            failure = self._failure
        if failure is not None:
            if isinstance(failure, InputObserverError):
                raise failure
            raise InputObserverError(
                f"{type(self).__name__} failed: {failure}"
            ) from failure
        if self._thread is not None and not self._thread.is_alive():
            raise InputObserverError(f"{type(self).__name__} stopped unexpectedly")
        if self._delivery_thread is not None and not self._delivery_thread.is_alive():
            raise InputObserverError(
                f"{type(self).__name__} delivery stopped unexpectedly"
            )

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            self._stop_delivery()
            self.check_health()
            return
        self._stop_requested.set()
        primary: BaseException | None = None
        try:
            self._wake()
        except BaseException as wake_error:
            primary = wake_error
        thread.join(self.shutdown_timeout)
        if thread.is_alive():
            timeout_error = InputObserverError(
                f"{type(self).__name__} did not stop within "
                f"{self.shutdown_timeout:.1f}s"
            )
            if primary is None:
                primary = timeout_error
            else:
                add_exception_note(
                    primary,
                    f"observer event loop also failed to stop: {timeout_error}",
                )
        else:
            self._thread = None
        incomplete_receipt = InputObserverError(
            f"{type(self).__name__} stopped before reserved native input "
            "reached delivery"
        )
        if self._fail_unqueued_receipts(incomplete_receipt):
            self._store_failure(incomplete_receipt)
        self._stop_delivery()
        if primary is not None:
            with self._failure_lock:
                cleanup_failure = self._failure
            if cleanup_failure is not None:
                add_exception_note(
                    primary,
                    f"observer shutdown also recorded: {cleanup_failure}",
                )
            raise primary
        self.check_health()
