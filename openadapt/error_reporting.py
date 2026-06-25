"""Module for error reporting logic."""

from typing import Any

from loguru import logger
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QMessageBox, QPushButton
import git
import sentry_sdk
import webbrowser

from openadapt.build_utils import is_running_from_executable
from openadapt.config import PARENT_DIR_PATH, config


def configure_error_reporting() -> None:
    """Configure error reporting."""
    logger.info(f"{config.ERROR_REPORTING_ENABLED=}")
    if not config.ERROR_REPORTING_ENABLED:
        return

    if is_running_from_executable():
        is_reporting_branch = True
    else:
        active_branch_name = git.Repo(PARENT_DIR_PATH).active_branch.name
        logger.info(f"{active_branch_name=}")
        is_reporting_branch = active_branch_name == config.ERROR_REPORTING_BRANCH
        logger.info(f"{is_reporting_branch=}")

    if is_reporting_branch:
        sentry_sdk.init(
            dsn=config.ERROR_REPORTING_DSN,
            traces_sample_rate=1.0,
            before_send=before_send_event,
            ignore_errors=[KeyboardInterrupt],
        )


def show_alert() -> None:
    """Show an alert to the user."""
    # TODO: move to config
    from openadapt.app.tray import ICON_PATH

    msg = QMessageBox()
    msg.setIcon(QMessageBox.Warning)
    msg.setWindowIcon(QIcon(ICON_PATH))
    msg.setText(
        """
        An error has occurred. The development team has been notified.
        Please join the discord server to get help or send an email to
        help@openadapt.ai
        """
    )
    discord_button = QPushButton("Join the discord server")
    discord_button.clicked.connect(
        lambda: webbrowser.open("https://discord.gg/yF527cQbDG")
    )
    msg.addButton(discord_button, QMessageBox.ActionRole)
    msg.addButton(QMessageBox.Ok)
    msg.exec()


# Errors from environments where OpenAdapt cannot run -- headless / no display,
# unsupported platform, or a broken/partial native dependency set -- are not
# actionable bugs. They otherwise flood the dashboard from CI and automated
# headless environments and bury real issues. Drop them. See GLITCHTIP_FIX_PLAN.md.
_UNSUPPORTED_ENV_SUBSTRINGS = (
    "failed to acquire X connection",
    "Bad display name",
    "DISPLAY not set",
    "this platform is not supported",  # pynput import on headless Linux
    "No module named 'pywinauto'",  # non-Windows / old releases (guarded on main)
    "module 'pywinauto' has no attribute",
    "_ARRAY_API not found",  # numpy 1.x/2.x ABI mismatch in a broken env
    "crop_active_window should return an image",  # headless screenshot returned None
)


def is_unsupported_environment_error(event: Any, hint: Any) -> bool:
    """Return True if the event is an unactionable unsupported-environment error."""
    texts = []
    exc_info = hint.get("exc_info") if hint else None
    if exc_info and exc_info[1] is not None:
        texts.append(f"{type(exc_info[1]).__name__}: {exc_info[1]}")
    for value in (event.get("exception", {}) or {}).get("values", []) or []:
        texts.append(f"{value.get('type')}: {value.get('value')}")
    blob = " ".join(texts)
    return any(substring in blob for substring in _UNSUPPORTED_ENV_SUBSTRINGS)


def before_send_event(event: Any, hint: Any) -> Any:
    """Filter events before sending to GlitchTip.

    Drops unactionable unsupported-environment errors (headless, wrong platform,
    broken native deps) that otherwise flood the dashboard from CI/automated
    environments. For genuine errors, surfaces a user alert -- but only when a GUI
    is actually running, never headless, and never for filtered noise.
    """
    if is_unsupported_environment_error(event, hint):
        return None  # drop -- not an actionable bug

    try:
        from PySide6.QtWidgets import QApplication

        if QApplication.instance() is not None:
            show_alert()
    except Exception:
        pass
    return event
