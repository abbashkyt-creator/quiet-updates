"""Skip SQLAlchemy UPDATEs that only touch bookkeeping columns.

Many tables carry columns that change on every read of the outside world but
mean nothing on their own: "last checked at", a countdown, a poll counter.
Writing them every time produces a new row version, WAL, index work and lock
traffic, even though the data the row exists for did not change.

QuietUpdates installs a ``before_flush`` hook. For a watched model, an UPDATE
whose changed columns are all bookkeeping columns is dropped while the row's
*stored* timestamp is younger than ``min_interval``. The skipped values are
remembered (``quiet.value(obj, column)`` returns the newest one) and are written
with the row's next UPDATE: a real data change, or the first bookkeeping change
after the interval.

Only ORM flushes are seen. Core ``update()`` statements bypass the hook.
"""
from __future__ import annotations

import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified, set_committed_value

__all__ = ["QuietUpdates", "QuietStats"]
__version__ = "0.2.0"

_DB_CLOCK = object()  # memory slot for the clock value the database really holds


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Read a naive datetime as UTC, so SQLite and Postgres compare the same."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


@dataclass
class _Rule:
    columns: frozenset[str]
    clock: str
    min_interval: timedelta


@dataclass
class QuietStats:
    skipped: int = 0
    written: int = 0

    def reset(self) -> None:
        self.skipped = 0
        self.written = 0


@dataclass
class QuietUpdates:
    """Register models, then ``install`` on a Session class or sessionmaker.

    ``max_remembered`` bounds the memory of skipped values. When it is full,
    the oldest row is forgotten: its database copy simply stays older until
    that row's next real write.
    """

    now: Callable[[], datetime] = _utc_now
    max_remembered: int = 100_000
    stats: QuietStats = field(default_factory=QuietStats)
    _rules: dict[type, _Rule] = field(default_factory=dict)
    _memory: "OrderedDict[tuple, dict]" = field(default_factory=OrderedDict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def watch(
        self,
        model: type,
        *,
        columns: Iterable[str],
        clock: str,
        min_interval: timedelta,
    ) -> "QuietUpdates":
        """Treat ``columns`` of ``model`` as bookkeeping.

        ``clock`` must be one of them: a timestamp column whose stored value
        says when the row was last written. While ``now() - stored clock`` is
        below ``min_interval``, an UPDATE that changes only bookkeeping columns
        is not sent.
        """
        cols = frozenset(columns)
        if clock not in cols:
            raise ValueError(f"clock column {clock!r} must be one of the bookkeeping columns")
        if min_interval <= timedelta(0):
            raise ValueError("min_interval must be positive")
        self._rules[model] = _Rule(cols, clock, min_interval)
        return self

    def install(self, target: type[Session] | object = Session) -> "QuietUpdates":
        """Listen on a Session class, a sessionmaker, or a single Session."""
        event.listen(target, "before_flush", self._before_flush)
        return self

    def uninstall(self, target: type[Session] | object = Session) -> None:
        event.remove(target, "before_flush", self._before_flush)

    def value(self, obj: object, column: str):
        """The newest value of ``column``: a skipped one if any, else the object's."""
        with self._lock:
            remembered = self._memory.get(self._key(obj), {})
            if column in remembered:
                return remembered[column]
        return getattr(obj, column)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _key(obj: object) -> tuple:
        return (type(obj), inspect(obj).identity)

    def _rule_for(self, obj: object) -> _Rule | None:
        for cls in type(obj).__mro__:
            rule = self._rules.get(cls)
            if rule is not None:
                return rule
        return None

    def _before_flush(self, session: Session, flush_context, instances) -> None:
        for obj in list(session.dirty):
            rule = self._rule_for(obj)
            if rule is None or not session.is_modified(obj, include_collections=False):
                continue
            state = inspect(obj)
            changed = {a.key: a.history for a in state.attrs if a.history.has_changes()}
            if not changed:
                continue
            key = self._key(obj)
            with self._lock:
                remembered = self._memory.get(key, {})
                db_clock = remembered.get(_DB_CLOCK, self._stored(state, rule.clock))
                quiet = set(changed) <= rule.columns and self._fresh(db_clock, rule)
                if not quiet:
                    self._memory.pop(key, None)
            if not quiet:
                # Real write: carry every skipped value that was not changed again.
                for column, value in remembered.items():
                    if column is not _DB_CLOCK and column not in changed:
                        setattr(obj, column, value)
                        flag_modified(obj, column)
                self.stats.written += 1
                continue
            with self._lock:
                entry = self._memory.pop(key, {})
                entry.setdefault(_DB_CLOCK, db_clock)
                for column, history in changed.items():
                    entry[column] = history.added[0] if history.added else None
                self._memory[key] = entry  # most recently used goes last
                while len(self._memory) > self.max_remembered:
                    self._memory.popitem(last=False)
            for column, history in changed.items():
                # Accept the new value on the object without scheduling a write.
                set_committed_value(obj, column, history.added[0] if history.added else None)
            self.stats.skipped += 1

    @staticmethod
    def _stored(state, clock: str):
        """The clock value as the database holds it right now."""
        history = state.attrs[clock].history
        if history.deleted:
            return history.deleted[0]
        if history.unchanged:
            return history.unchanged[0]
        # The attribute was expired (or never loaded) and possibly set since:
        # SQLAlchemy does not keep the old value, so ask the database directly.
        mapper = state.mapper
        query = select(getattr(mapper.class_, clock)).where(
            *[col == value for col, value in zip(mapper.primary_key, state.identity)]
        )
        with state.session.no_autoflush:
            return state.session.execute(query).scalar_one_or_none()

    def _fresh(self, stored: datetime | None, rule: _Rule) -> bool:
        if stored is None:
            return False  # never stamped: write it
        return _aware(self.now()) - _aware(stored) < rule.min_interval
