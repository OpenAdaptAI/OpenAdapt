"""Admit exact result text on a native MySQL-family read connection."""

from collections.abc import Mapping
from typing import Any


MYSQL_RESULT_CHARSET_REFUSAL = "MySQL reads require character_set_results=utf8mb4"
_MYSQL_DRIVERS = frozenset({"pymysql", "MySQLdb", "mysql.connector", "mariadb"})
_NATIVE_MODULES = _MYSQL_DRIVERS | {"_mysql", "_mysql_connector", "_mariadb"}


class MySQLResultCharsetRefused(ValueError):
    """The read connection cannot establish exact result text."""

    def __init__(self) -> None:
        super().__init__(MYSQL_RESULT_CHARSET_REFUSAL)


def is_mysql_read(connection: Any, driver: str | None) -> bool:
    """Recognize a configured driver or a native connection family."""
    return driver in _MYSQL_DRIVERS or any(
        cls.__module__ == module or cls.__module__.startswith(module + ".")
        for cls in type(connection).__mro__
        for module in _NATIVE_MODULES
    )


def mysql_utf8mb4_cursor(connection: Any) -> Any:
    """Probe the same session before metadata or record queries."""
    try:
        cursor = connection.cursor()
        cursor.execute("SELECT @@SESSION.character_set_results")
        rows = cursor.fetchall()
        if len(rows) == 1:
            row = rows[0]
            cells = tuple(row.values()) if isinstance(row, Mapping) else row
            if (
                isinstance(cells, (tuple, list))
                and len(cells) == 1
                and type(cells[0]) is str
                and cells[0] == "utf8mb4"
            ):
                return cursor
    except Exception:  # noqa: BLE001 - an unreadable probe cannot qualify the read
        raise MySQLResultCharsetRefused() from None
    raise MySQLResultCharsetRefused()
