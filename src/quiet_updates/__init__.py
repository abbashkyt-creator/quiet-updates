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
after the interval. They are written only where the stored clock is still older
than the skip, so they never overwrite a newer write from another process.

Memory follows the transaction. Whatever a transaction or SAVEPOINT did to it
(values a write took, values a skip added) is undone if that transaction or
SAVEPOINT rolls back, or the session closes without a commit.

Only ORM flushes are seen. Core ``update()`` statements bypass the hook.
"""
from __future__ import annotations

import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import case, event, inspect, literal, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified, set_committed_value

__all__ = ["QuietUpdates", "QuietStats"]
__version__ = "0.2.2"

_ABSENT = object()  # "this column had no remembered value"
_ROOT = "root"  # journal scope of the outermost transaction
_JOURNAL = "quiet_updates.journal"  # session.info: {scope: {key: {column: [before, now]}}}
_CLOCKS = "quiet_updates.clocks"  # session.info: {scope: [(state, clock column)]}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Read a naive datetime as UTC, so SQLite and Postgres compare the same."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _scope(session: Session):
    """The innermost SAVEPOINT, or the outermost transaction."""
    return session.get_nested_transaction() or _ROOT


def _inside(scope, savepoint) -> bool:
    """Whether ``scope`` is ``savepoint`` or a SAVEPOINT nested in it."""
    while scope is not None and scope is not _ROOT:
        if scope is savepoint:
            return True
        scope = scope.parent
    return False


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
    the oldest row's skipped values are forgotten and never reach the
    database. The interval is still kept: the row's next bookkeeping change
    is judged against the clock the database holds, as always.
    """

    now: Callable[[], datetime] = _utc_now
    max_remembered: int = 100_000
    stats: QuietStats = field(default_factory=QuietStats)
    _rules: dict[type, _Rule] = field(default_factory=dict)
    _memory: "OrderedDict[tuple, dict]" = field(default_factory=OrderedDict)
    # The clock the database held when an object's first skip overwrote it in
    # memory, per object. Separate from _memory, so evicting skipped values can
    # never make a row look fresher than it is.
    _clocks: "weakref.WeakKeyDictionary" = field(default_factory=weakref.WeakKeyDictionary)
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
        for name, listener in self._listeners():
            event.listen(target, name, listener)
        return self

    def uninstall(self, target: type[Session] | object = Session) -> None:
        for name, listener in self._listeners():
            event.remove(target, name, listener)

    def value(self, obj: object, column: str):
        """The newest value of ``column``: a skipped one if any, else the object's."""
        with self._lock:
            remembered = self._memory.get(self._key(obj), {})
            if column in remembered:
                return remembered[column]
        return getattr(obj, column)

    # -- internals ---------------------------------------------------------

    def _listeners(self):
        return (
            ("before_flush", self._before_flush),
            ("after_commit", self._after_commit),
            ("after_soft_rollback", self._after_soft_rollback),
            ("after_transaction_end", self._after_transaction_end),
        )

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
            changed = {
                a.key: (a.history.added[0] if a.history.added else None)
                for a in state.attrs
                if a.history.has_changes()
            }
            if not changed:
                continue
            if set(changed) <= rule.columns:
                db_clock = self._db_clock(state, rule.clock)
                if self._fresh(db_clock, rule):
                    self._skip(session, state, rule.clock, changed, db_clock)
                    continue
            self._write(session, state, rule.clock, changed)

    def _journal(self, session: Session, key: tuple) -> dict:
        return session.info.setdefault(_JOURNAL, {}).setdefault(_scope(session), {}).setdefault(key, {})

    def _skip(self, session: Session, state, clock: str, changed: dict, db_clock) -> None:
        key = self._key(state.obj())
        with self._lock:
            if state not in self._clocks:
                self._clocks[state] = db_clock
                session.info.setdefault(_CLOCKS, {}).setdefault(_scope(session), []).append((state, clock))
            entry = self._memory.pop(key, {})
            journal = self._journal(session, key)
            for column, value in changed.items():
                journal.setdefault(column, [entry.get(column, _ABSENT), None])[1] = value
                entry[column] = value
            self._memory[key] = entry  # most recently used goes last
            self._trim()
        for column, value in changed.items():
            # Accept the new value on the object without scheduling a write.
            set_committed_value(state.obj(), column, value)
        self.stats.skipped += 1

    def _write(self, session: Session, state, clock: str, changed: dict) -> None:
        obj = state.obj()
        key = self._key(obj)
        with self._lock:
            remembered = self._memory.pop(key, {})
            if clock in changed or clock in remembered:
                self._clocks.pop(state, None)  # this UPDATE stores the clock
            if remembered:
                journal = self._journal(session, key)
                for column, value in remembered.items():
                    journal.setdefault(column, [value, None])[1] = _ABSENT
        # Real write: carry every skipped value that was not changed again, but
        # only where the stored clock is still older than the skip. Otherwise
        # another process has written the row since, and its values are newer.
        skipped_clock = remembered.get(clock)
        mapped = type(obj)
        for column, value in remembered.items():
            if column in changed:
                continue
            if skipped_clock is not None:
                attr = getattr(mapped, column)
                value = case(
                    (getattr(mapped, clock) < literal(skipped_clock, getattr(mapped, clock).type),
                     literal(value, attr.type)),
                    else_=attr,
                )
            setattr(obj, column, value)
            flag_modified(obj, column)
        self.stats.written += 1

    def _after_commit(self, session: Session) -> None:
        session.info.pop(_JOURNAL, None)
        session.info.pop(_CLOCKS, None)

    def _after_soft_rollback(self, session: Session, previous_transaction) -> None:
        if previous_transaction.nested:
            self._undo(session, lambda scope: _inside(scope, previous_transaction))
        elif previous_transaction.parent is None:
            self._undo(session, lambda scope: True)
        # Otherwise a flush failed: the transaction around it must still roll
        # back, and that rollback undoes its scope.

    def _after_transaction_end(self, session: Session, transaction) -> None:
        if transaction.parent is None:
            # Closed without a commit (a commit has already cleared the journal).
            self._undo(session, lambda scope: True)
            session.info.pop(_JOURNAL, None)
            session.info.pop(_CLOCKS, None)

    def _undo(self, session: Session, chosen: Callable[[object], bool]) -> None:
        """Put memory back as it was before the chosen scopes changed it, newest first."""
        journals = session.info.get(_JOURNAL, {})
        recorded = session.info.get(_CLOCKS, {})
        undone = []
        with self._lock:
            for scope in [s for s in reversed(list(journals)) if chosen(s)]:
                for key, journal in journals.pop(scope).items():
                    entry = self._memory.pop(key, {})
                    for column, (before, now) in journal.items():
                        if entry.get(column, _ABSENT) is not now:
                            continue  # changed since by another session: theirs is newer
                        if before is _ABSENT:
                            entry.pop(column, None)
                        else:
                            entry[column] = before
                    if entry:
                        self._memory[key] = entry
            self._trim()
            # Clocks recorded in these scopes may come from their own
            # rolled-back writes: forget them and read the database again.
            for scope in [s for s in list(recorded) if chosen(s)]:
                for state, clock in recorded.pop(scope):
                    self._clocks.pop(state, None)
                    undone.append((state, clock))
        for state, clock in undone:
            obj = state.obj()
            if obj is not None and state.session is session and state.persistent:
                session.expire(obj, [clock])

    def _trim(self) -> None:
        while len(self._memory) > self.max_remembered:
            self._memory.popitem(last=False)

    def _db_clock(self, state, clock: str):
        """The clock value as the database holds it right now."""
        with self._lock:
            recorded = self._clocks.get(state, _ABSENT)
        if recorded is not _ABSENT:
            return recorded
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
