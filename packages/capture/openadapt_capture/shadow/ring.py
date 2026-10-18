"""A bounded, self-expiring ring buffer of shadow symbols.

Two caps apply at once: a size cap (the oldest symbol is overwritten first)
and a time cap (a symbol older than ``max_age_s`` is deleted on every write
and every read). The buffer lives in memory unless a ``path`` is given.

On disk, every row is sealed with AES-256-GCM, following OpenAdapt Desktop's
item vault: a random 96-bit nonce per row, and the key id and the row's
timestamp as associated data, so a row can't be moved or re-dated unnoticed.
The key comes from :mod:`openadapt_capture.shadow.keys`. The file stores only
the key id and a check value; neither reveals the key. A buffer sealed with a
key that is no longer available is discarded rather than kept unreadable.

The file has owner-only permissions, ``secure_delete`` on (deleted rows are
overwritten), and a rollback journal instead of a write-ahead log, so
expired rows don't linger in a side file. A disk buffer without a cipher is
refused: shadow symbols are never written to disk in plain text.
"""

from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

from loguru import logger

from openadapt_capture.shadow.symbols import ShadowSymbol

Clock = Callable[[], float]
SEALED_PREFIX = "enc:v1:"
_NONCE_BYTES = 12


class ShadowDependencyError(RuntimeError):
    """An optional dependency for shadow mining is missing."""


class RowCipher:
    """AES-256-GCM sealing for ring-buffer rows."""

    def __init__(self, key: bytes, *, key_id: str, check: str) -> None:
        if len(key) != 32:
            raise ValueError("the ring-buffer key must be 32 bytes")
        try:
            from cryptography.exceptions import InvalidTag
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:  # pragma: no cover - depends on the install
            raise ShadowDependencyError(
                "an on-disk shadow buffer needs the 'cryptography' package: "
                "pip install 'openadapt-capture[shadow]'"
            ) from exc
        self._aead = AESGCM(key)
        self._invalid = InvalidTag
        self.key_id = key_id
        self.check = check

    def _aad(self, t: float) -> bytes:
        return f"{self.key_id}\x00{t!r}".encode()

    def seal(self, text: str, t: float) -> str:
        nonce = secrets.token_bytes(_NONCE_BYTES)
        blob = nonce + self._aead.encrypt(nonce, text.encode("utf-8"), self._aad(t))
        return SEALED_PREFIX + base64.urlsafe_b64encode(blob).decode("ascii")

    def open(self, token: str, t: float) -> str | None:
        """Return the plain text, or None if the row can't be authenticated."""

        if not token.startswith(SEALED_PREFIX):
            return None
        try:
            blob = base64.urlsafe_b64decode(token[len(SEALED_PREFIX) :].encode("ascii"))
            plain = self._aead.decrypt(blob[:_NONCE_BYTES], blob[_NONCE_BYTES:], self._aad(t))
        except (self._invalid, ValueError):
            return None
        return plain.decode("utf-8")


class SymbolRing:
    """Ring buffer with a size cap, a time cap, and automatic overwrite."""

    def __init__(
        self,
        *,
        max_symbols: int,
        max_age_s: float,
        path: Path | None = None,
        cipher: RowCipher | None = None,
        clock: Clock = time.time,
    ) -> None:
        if max_symbols < 1:
            raise ValueError("max_symbols must be at least 1")
        if max_age_s <= 0:
            raise ValueError("max_age_s must be positive")
        if path is not None and cipher is None:
            raise ValueError("an on-disk shadow buffer must be encrypted; pass a cipher")
        self.max_symbols = int(max_symbols)
        self.max_age_s = float(max_age_s)
        self._clock = clock
        self._lock = threading.Lock()
        self._path = Path(path) if path is not None else None
        self._cipher = cipher
        self._mem: deque[ShadowSymbol] = deque(maxlen=self.max_symbols)
        self._db: sqlite3.Connection | None = None
        if self._path is not None:
            self._db = self._open(self._path)
            self._bind_key()
            self._expire_locked()

    @staticmethod
    def _open(path: Path) -> sqlite3.Connection:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not path.exists():
            fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
            os.close(fd)
        os.chmod(path, 0o600)
        db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        db.execute("PRAGMA secure_delete=ON")
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute(
            "CREATE TABLE IF NOT EXISTS shadow_symbol ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, t REAL NOT NULL, payload TEXT NOT NULL)"
        )
        db.execute("CREATE INDEX IF NOT EXISTS shadow_symbol_t ON shadow_symbol(t)")
        db.execute(
            "CREATE TABLE IF NOT EXISTS shadow_meta (name TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        return db

    def _bind_key(self) -> None:
        """Discard rows sealed with another key, then record this key's id."""

        assert self._db is not None and self._cipher is not None
        meta = dict(self._db.execute("SELECT name, value FROM shadow_meta").fetchall())
        same = meta.get("key_id") == self._cipher.key_id and meta.get("key_check") == (
            self._cipher.check
        )
        if not same:
            count = self._db.execute("SELECT COUNT(*) FROM shadow_symbol").fetchone()[0]
            if count:
                logger.warning(
                    "discarding {} shadow symbols sealed with a key that is no longer available",
                    count,
                )
            self._db.execute("DELETE FROM shadow_symbol")
            self._db.execute("DELETE FROM shadow_meta")
            self._db.executemany(
                "INSERT INTO shadow_meta (name, value) VALUES (?, ?)",
                [("key_id", self._cipher.key_id), ("key_check", self._cipher.check)],
            )
            if count:
                self._db.execute("VACUUM")

    @property
    def path(self) -> Path | None:
        return self._path

    def append(self, symbol: ShadowSymbol) -> None:
        """Add one symbol, then enforce both caps."""

        with self._lock:
            if self._db is not None:
                assert self._cipher is not None
                payload = self._cipher.seal(json.dumps(symbol.to_dict(), sort_keys=True), symbol.t)
                self._db.execute(
                    "INSERT INTO shadow_symbol (t, payload) VALUES (?, ?)", (symbol.t, payload)
                )
            else:
                self._mem.append(symbol)
            self._expire_locked()

    def extend(self, symbols: list[ShadowSymbol]) -> None:
        for symbol in symbols:
            self.append(symbol)

    def snapshot(self) -> list[ShadowSymbol]:
        """Return the live symbols, oldest first, after expiring stale ones."""

        with self._lock:
            self._expire_locked()
            if self._db is not None:
                assert self._cipher is not None
                rows = self._db.execute(
                    "SELECT seq, t, payload FROM shadow_symbol ORDER BY seq"
                ).fetchall()
                out: list[ShadowSymbol] = []
                bad: list[int] = []
                for seq, t, payload in rows:
                    plain = self._cipher.open(payload, t)
                    if plain is None:
                        bad.append(seq)
                        continue
                    out.append(ShadowSymbol.from_dict(json.loads(plain)))
                if bad:
                    logger.warning("dropping {} shadow rows that failed authentication", len(bad))
                    self._db.executemany(
                        "DELETE FROM shadow_symbol WHERE seq = ?", [(s,) for s in bad]
                    )
                return out
            # A clock step backwards can leave a stale symbol behind a fresh
            # one; filter so nothing past the time cap is ever returned.
            cutoff = self._clock() - self.max_age_s
            live = [symbol for symbol in self._mem if symbol.t >= cutoff]
            if len(live) != len(self._mem):
                self._mem = deque(live, maxlen=self.max_symbols)
            return live

    def __len__(self) -> int:
        return len(self.snapshot())

    def purge(self) -> None:
        """Delete every symbol now."""

        with self._lock:
            self._mem.clear()
            if self._db is not None:
                self._db.execute("DELETE FROM shadow_symbol")
                self._db.execute("VACUUM")

    def rekey(self, cipher: RowCipher) -> None:
        """Purge every symbol, then seal new rows with ``cipher``.

        Called when the key's epoch ends: rows sealed with the old key go
        with it.
        """

        with self._lock:
            self._mem.clear()
            if self._db is None:
                return
            self._cipher = cipher
            self._db.execute("DELETE FROM shadow_symbol")
            self._db.execute("DELETE FROM shadow_meta")
            self._db.executemany(
                "INSERT INTO shadow_meta (name, value) VALUES (?, ?)",
                [("key_id", cipher.key_id), ("key_check", cipher.check)],
            )
            self._db.execute("VACUUM")

    def close(self) -> None:
        with self._lock:
            if self._db is not None:
                self._db.close()
                self._db = None

    def destroy(self) -> None:
        """Purge, close, and delete the buffer file. The ring is unusable after."""

        self.purge()
        self.close()
        if self._path is not None:
            for candidate in (self._path, Path(f"{self._path}-journal")):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass

    def _expire_locked(self) -> None:
        cutoff = self._clock() - self.max_age_s
        if self._db is not None:
            self._db.execute("DELETE FROM shadow_symbol WHERE t < ?", (cutoff,))
            self._db.execute(
                "DELETE FROM shadow_symbol WHERE seq <= (SELECT MAX(seq) FROM shadow_symbol) - ?",
                (self.max_symbols,),
            )
            return
        while self._mem and self._mem[0].t < cutoff:
            self._mem.popleft()
