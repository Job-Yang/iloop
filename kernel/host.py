"""Process-external trust ledger for the managed public host entrypoint."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path


EXTERNAL_ATTESTATION_KINDS = frozenset({
    "evidence_subjects",
    "independent_review",
    "user_confirmation",
})


def _canonical(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class HostTrustStore:
    """Durable local-integrity attestations kept outside project task state.

    The managed host records facts when they occur and later verifies the exact
    payload. Identity-bearing facts are deliberately excluded: they must come
    from an external host adapter with a separate trust boundary.
    """

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser()
        self.root.mkdir(parents=True, exist_ok=True)
        self.key_path = self.root / "host.key"
        self.db_path = self.root / "attestations.sqlite3"
        self._key = self._load_key()
        self._init_db()

    def _load_key(self) -> bytes:
        if not self.key_path.exists():
            try:
                fd = os.open(
                    self.key_path,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except FileExistsError:
                pass
            else:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
        return self.key_path.read_bytes()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        return connection

    def _init_db(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS attestations (
                    kind TEXT NOT NULL,
                    path TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    signature TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY (kind, path, payload_sha256)
                )
                """
            )

    def _signature(self, kind: str, path: Path, payload_sha256: str) -> str:
        message = f"{kind}\0{path.resolve()}\0{payload_sha256}".encode("utf-8")
        return hmac.new(self._key, message, hashlib.sha256).hexdigest()

    def attest(self, kind: str, path: Path, payload: dict) -> None:
        if kind in EXTERNAL_ATTESTATION_KINDS:
            raise ValueError(
                f"{kind} requires an external host attestation provider"
            )
        resolved = Path(path).expanduser().resolve()
        payload_sha256 = hashlib.sha256(
            _canonical(payload).encode("utf-8")
        ).hexdigest()
        signature = self._signature(kind, resolved, payload_sha256)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO attestations
                    (kind, path, payload_sha256, signature, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    kind,
                    str(resolved),
                    payload_sha256,
                    signature,
                    time.time(),
                ),
            )

    def verify(self, kind: str, path: Path, payload: dict) -> bool:
        if kind in EXTERNAL_ATTESTATION_KINDS:
            return False
        resolved = Path(path).expanduser().resolve()
        payload_sha256 = hashlib.sha256(
            _canonical(payload).encode("utf-8")
        ).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_sha256, signature FROM attestations
                WHERE kind = ? AND path = ?
                ORDER BY rowid DESC
                LIMIT 1
                """,
                (kind, str(resolved)),
            ).fetchone()
        return bool(
            row
            and str(row[0]) == payload_sha256
            and hmac.compare_digest(
                str(row[1]),
                self._signature(kind, resolved, payload_sha256),
            )
        )
