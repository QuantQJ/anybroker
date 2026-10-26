"""Every alert, and what became of it.

The HTTP response cannot be the record: TradingView cancels after three
seconds, so the bridge answers before the broker has. This is the record --
what arrived, whether it was accepted, and what the broker said.

Including the refusals. A bridge that only logs what it placed cannot answer
why an alert did nothing, which is the question you actually have at 3am.
"""

from __future__ import annotations

import json
import pathlib
import sqlite3
import threading
import time

from .alert import redact_all

__all__ = ['Journal']

SCHEMA = """
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL    NOT NULL,
    order_id    TEXT,
    outcome     TEXT    NOT NULL,
    code        TEXT,
    detail      TEXT,
    venue       TEXT,
    env         TEXT,
    symbol      TEXT,
    action      TEXT,
    qty         REAL,
    broker_id   TEXT,
    source_ip   TEXT,
    body        TEXT
);
CREATE INDEX IF NOT EXISTS alerts_at ON alerts (at);
CREATE INDEX IF NOT EXISTS alerts_order ON alerts (order_id);
"""


class Journal:
    """An append-only record. Use ``path=None`` to keep it in memory.

    Give it the shared secret and every field is cleaned on the way in. One
    place, rather than at each call site: a field added later is covered by
    default instead of being remembered about.
    """

    def __init__(self, path=None, *, secret=None, secrets=()) -> None:
        self.secrets = tuple(
            dict.fromkeys([s for s in (secret, *(secrets or ())) if s])
        )
        self.path = pathlib.Path(path).expanduser() if path else None
        self._lock = threading.Lock()
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # It holds every alert body, so it is created private and
            # tightened if it was not. Mode is set before sqlite opens it,
            # so there is no window where it exists world-readable.
            if not self.path.exists():
                self.path.touch(mode=0o600)
            elif self.path.stat().st_mode & 0o077:
                self.path.chmod(0o600)
        self._conn = sqlite3.connect(
            str(self.path) if self.path else ':memory:',
            check_same_thread=False,
        )
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def record(
        self,
        outcome,
        *,
        alert=None,
        code=None,
        detail=None,
        venue=None,
        env=None,
        broker_id=None,
        source_ip=None,
        body=None,
    ) -> int:
        """Write one line. Never raises: a journal must not break a trade."""
        clean = self._clean
        row = (
            time.time(),
            clean(getattr(alert, 'order_id', None)),
            str(outcome),
            clean(code),
            clean(detail, 2000),
            clean(venue),
            clean(env),
            clean(getattr(alert, 'symbol', None)),
            clean(getattr(alert, 'action', None)),
            getattr(alert, 'qty', None),
            clean(broker_id),
            clean(source_ip),
            clean(body, 4000),
        )
        try:
            with self._lock:
                cursor = self._conn.execute(
                    'INSERT INTO alerts (at, order_id, outcome, code, detail, '
                    'venue, env, symbol, action, qty, broker_id, source_ip, '
                    'body) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    row,
                )
                self._conn.commit()
                return cursor.lastrowid or 0
        except sqlite3.Error:
            return 0

    def _clean(self, value, limit=None):
        """One field, with the secret taken out and the length capped.

        Everything written passes through here, including text that came from
        a broker exception -- which can quote a credential it was given.
        """
        if value is None:
            return None
        text = redact_all(str(value), self.secrets) if self.secrets else str(value)
        return text[:limit] if limit else text

    def recent(self, limit=50) -> list:
        with self._lock:
            cursor = self._conn.execute(
                'SELECT at, outcome, code, symbol, action, qty, broker_id, '
                'detail FROM alerts ORDER BY id DESC LIMIT ?',
                (int(limit),),
            )
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]

    def counts(self) -> dict:
        with self._lock:
            return dict(
                self._conn.execute(
                    'SELECT outcome, count(*) FROM alerts GROUP BY outcome'
                ).fetchall()
            )

    def refusals(self, limit=50) -> list:
        """Why alerts did nothing. The question a fill log cannot answer."""
        with self._lock:
            cursor = self._conn.execute(
                'SELECT at, code, detail, body FROM alerts '
                "WHERE outcome NOT IN ('placed', 'dry_run') "
                'ORDER BY id DESC LIMIT ?',
                (int(limit),),
            )
            columns = [c[0] for c in cursor.description]
            return [dict(zip(columns, r, strict=True)) for r in cursor.fetchall()]

    def as_json(self, limit=50) -> str:
        return json.dumps(
            {'counts': self.counts(), 'recent': self.recent(limit)},
            indent=1,
            default=str,
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
