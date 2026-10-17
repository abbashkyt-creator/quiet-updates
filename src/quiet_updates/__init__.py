"""Skip SQLAlchemy UPDATEs that only touch bookkeeping columns.

Many tables carry columns that change on every read of the outside world but
mean nothing on their own: "last checked at", a countdown. Writing them every
time produces a new row version, WAL, index work and lock traffic, even though
the data the row exists for did not change.

QuietUpdates installs a ``before_flush`` hook. For a watched model, an UPDATE
whose changed columns are all bookkeeping columns is dropped while the row's
*stored* timestamp is younger than ``min_interval``. The skipped values are
remembered (``quiet.value(obj, column)`` returns the newest one) and are written
with the row's next UPDATE: a real data change, or the first bookkeeping change
after the interval. They are written only where no other process has stamped
the row since, so they never overwrite a newer write.

Skipped values follow the transaction like any other change. They stay with
the session, in the SAVEPOINT that made them, until the outermost transaction
commits; only then do other sessions see them. A rolled-back SAVEPOINT or
transaction drops what it skipped and gives back what its writes took.

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
from sqlalchemy.sql.expression import ClauseElement

__all__ = ["QuietUpdates", "QuietStats"]
__version__ = "0.3.0"

_ABSENT = object()  # "this column had no remembered value"
_ROOT = "root"  # scope of the outermost transaction
_SEEN = "quiet_updates.seen"  # pseudo-column: the stored clock when the row was first skipped
_LAYERS = "quiet_updates.layers"  # session.info: {scope: _Layer}, not yet committed
_OWNERS = "quiet_updates.owners"  # session.info: ids of the QuietUpdates installed on it


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    """Read a naive datetime as UTC, so SQLite and Postgres compare the same."""
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _scope(session: Session):
    """The innermost SAVEPOINT, or the outermost transaction."""
    return session.get_nested_transaction() or _ROOT


def _depth(scope) -> int:
    depth = 0
    while scope is not None and scope is not _ROOT:
        depth += 1 if scope.nested else 0
        scope = scope.parent
    return depth


def _inside(scope, savepoint) -> bool:
    """Whether ``scope`` is ``savepoint`` or a SAVEPOINT nested in it."""
    while scope is not None and scope is not _ROOT:
        if scope is savepoint:
            return True
        scope = scope.parent
    return False


def _parent_scope(savepoint):
    parent = savepoint.parent
    while parent is not None and not parent.nested:
        parent = parent.parent
    return parent or _ROOT


@dataclass
class _Layer:
    """What one transaction or SAVEPOINT did to the remembered values."""

    skips: dict = field(default_factory=dict)  # key -> {column: value} skipped here, after any cut
    # key -> the committed values a write here consumed. A write consumes every
    # remembered value of its row below this scope; committed values are named
    # so that newer ones, committed meanwhile by other sessions, survive.
    cuts: dict = field(default_factory=dict)
    clocks: list = field(default_factory=list)  # (state, clock column) stored clocks recorded here


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

    ``max_remembered`` bounds the committed memory of skipped values. When it
    is full, the oldest row's skipped values are forgotten and never reach the
    database. The interval is still kept: the row's next bookkeeping change is
    judged against the clock the database holds, as always.
    """

    now: Callable[[], datetime] = _utc_now
    max_remembered: int = 100_000
    stats: QuietStats = field(default_factory=QuietStats)
    _rules: dict[type, _Rule] = field(default_factory=dict)
    _memory: "OrderedDict[tuple, dict]" = field(default_factory=OrderedDict)  # committed skips
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
        if model not in self._rules:
            # A loaded or refreshed object shows its remembered values, so the
            # ORM sees the newest value and setting a column back to its stored
            # value still counts as a change.
            event.listen(model, "load", self._on_load, propagate=True)
            event.listen(model, "refresh", self._on_refresh, propagate=True)
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
        """The newest value of ``column`` as this object's session sees it."""
        state = inspect(obj)
        view = self._view(state.session, self._key(obj))
        if column in view:
            return view[column][0]
        return getattr(obj, column)

    # -- internals ---------------------------------------------------------

    def _listeners(self):
        return (
            ("after_begin", self._after_begin),
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

    def _after_begin(self, session: Session, transaction, connection) -> None:
        session.info.setdefault(_OWNERS, set()).add(id(self))

    def _on_load(self, obj, context) -> None:
        self._overlay(context.session, obj, None)

    def _on_refresh(self, obj, context, attrs) -> None:
        self._overlay(context.session, obj, attrs)

    def _overlay(self, session: Session | None, obj, attrs) -> None:
        if session is None or id(self) not in session.info.get(_OWNERS, ()):
            return  # a session this instance is not installed on sees the database
        rule = self._rule_for(obj)
        if rule is None:
            return
        state = inspect(obj)
        for column, (value, _) in self._view(session, self._key(obj)).items():
            if column == _SEEN or (attrs is not None and column not in attrs):
                continue
            if column == rule.clock and column in state.dict:
                with self._lock:
                    if state not in self._clocks:
                        self._clocks[state] = state.dict[column]  # the stored clock, just loaded
                        layers = session.info.setdefault(_LAYERS, {})
                        layers.setdefault(_scope(session), _Layer()).clocks.append((state, column))
            set_committed_value(obj, column, value)

    @staticmethod
    def _layers(session: Session | None) -> list:
        """This session's uncommitted layers, outermost first."""
        if session is None:
            return []
        layers = session.info.get(_LAYERS, {})
        return sorted(layers.items(), key=lambda item: _depth(item[0]))

    def _view(self, session: Session | None, key: tuple) -> dict:
        """column -> (newest remembered value, the scope that skipped it, or None if committed)."""
        with self._lock:
            committed = dict(self._memory.get(key, {}))
        view = {column: (value, None) for column, value in committed.items()}
        for scope, layer in self._layers(session):
            if key in layer.cuts:
                consumed = layer.cuts[key]
                view = {column: (value, None) for column, value in committed.items()
                        if consumed.get(column, _ABSENT) is not value}
            for column, value in layer.skips.get(key, {}).items():
                view[column] = (value, scope)
        return view

    def _before_flush(self, session: Session, flush_context, instances) -> None:
        self._after_begin(session, None, None)  # installed on a session already in a transaction
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
            quiet_kind = set(changed) <= rule.columns and not any(
                isinstance(value, ClauseElement) for value in changed.values()  # a SQL expression is a write
            )
            if quiet_kind:
                db_clock = self._db_clock(state, rule.clock)
                if self._fresh(db_clock, rule):
                    self._skip(session, state, rule.clock, changed, db_clock)
                    continue
            self._write(session, state, rule.clock, changed)

    def _skip(self, session: Session, state, clock: str, changed: dict, db_clock) -> None:
        key = self._key(state.obj())
        scope = _scope(session)
        layer = session.info.setdefault(_LAYERS, {}).setdefault(scope, _Layer())
        seen = _SEEN in self._view(session, key)
        entry = layer.skips.setdefault(key, {})
        if not seen:
            entry[_SEEN] = db_clock
        entry.update(changed)
        with self._lock:
            if state not in self._clocks:
                self._clocks[state] = db_clock
                layer.clocks.append((state, clock))
        for column, value in changed.items():
            # Accept the new value on the object without scheduling a write.
            set_committed_value(state.obj(), column, value)
        self.stats.skipped += 1

    def _write(self, session: Session, state, clock: str, changed: dict) -> None:
        obj = state.obj()
        key = self._key(obj)
        scope = _scope(session)
        view = self._view(session, key)
        if clock in changed or clock in view:
            with self._lock:
                self._clocks.pop(state, None)  # this UPDATE stores the clock
        if view:
            # This write consumes every remembered value of the row: it carries
            # the ones not changed again, and the changed ones are superseded.
            layer = session.info.setdefault(_LAYERS, {}).setdefault(scope, _Layer())
            with self._lock:
                committed = dict(self._memory.get(key, {}))
            layer.cuts[key] = {**layer.cuts.get(key, {}), **committed}
            layer.skips.pop(key, None)
        remembered = {column: value for column, (value, _) in view.items()}
        seen = remembered.pop(_SEEN, None)
        mapped = type(obj)
        stored = getattr(mapped, clock)
        skipped_clock = remembered.get(clock)
        # Land only where no other process has written the row since the skip:
        # its stored clock must not be newer than ours (a tie is a re-stamp
        # with the same time), or must be unchanged.
        if skipped_clock is not None:
            unchanged = stored <= literal(skipped_clock, stored.type)
        elif seen is not None:
            unchanged = stored == literal(seen, stored.type)
        else:
            unchanged = None
        for column, value in remembered.items():
            if column in changed:
                continue
            attr = getattr(mapped, column)
            if unchanged is not None:
                value = case((unchanged, literal(value, attr.type)), else_=attr)
            setattr(obj, column, value)
            flag_modified(obj, column)
        self.stats.written += 1

    def _after_commit(self, session: Session) -> None:
        layers = session.info.get(_LAYERS)
        released = session.get_nested_transaction()
        if released is not None:
            # A SAVEPOINT was released: what it did now belongs to the scope around it.
            if layers and released in layers:
                self._merge(layers, layers.pop(released), _parent_scope(released))
            return
        # The outermost transaction committed: fold any SAVEPOINTs still open
        # into it, then publish to every session.
        session.info.pop(_LAYERS, None)
        if not layers:
            return
        while len(layers) > 1:
            deepest = max(layers, key=_depth)
            self._merge(layers, layers.pop(deepest), _parent_scope(deepest))
        root = layers.get(_ROOT) or next(iter(layers.values()))
        with self._lock:
            for key, consumed in root.cuts.items():
                entry = self._memory.get(key)
                if entry is None:
                    continue
                for column, value in consumed.items():
                    if entry.get(column, _ABSENT) is value:
                        del entry[column]
                if not entry:
                    del self._memory[key]
            for key, columns in root.skips.items():
                entry = self._memory.pop(key, {})
                entry.update(columns)
                self._memory[key] = entry  # most recently used goes last
            while len(self._memory) > self.max_remembered:
                self._memory.popitem(last=False)

    @staticmethod
    def _merge(layers: dict, child: _Layer, parent_scope) -> None:
        parent = layers.setdefault(parent_scope, _Layer())
        for key, consumed in child.cuts.items():
            parent.skips.pop(key, None)  # below the child's cut: written
            parent.cuts[key] = {**parent.cuts.get(key, {}), **consumed}
        for key, columns in child.skips.items():
            parent.skips.setdefault(key, {}).update(columns)
        parent.clocks.extend(child.clocks)

    def _after_soft_rollback(self, session: Session, previous_transaction) -> None:
        if previous_transaction.nested:
            self._drop(session, lambda scope: _inside(scope, previous_transaction))
        elif previous_transaction.parent is None:
            self._drop(session, lambda scope: True)
        # Otherwise a flush failed: the transaction around it must still roll
        # back, and that rollback drops its scope.

    def _after_transaction_end(self, session: Session, transaction) -> None:
        if transaction.parent is None:
            self._drop(session, lambda scope: True)  # closed without a commit
            session.info.pop(_LAYERS, None)

    def _drop(self, session: Session, chosen: Callable[[object], bool]) -> None:
        """Forget what the chosen scopes skipped and took."""
        layers = session.info.get(_LAYERS, {})
        dropped = [layers.pop(scope) for scope in list(layers) if chosen(scope)]
        recorded = [entry for layer in dropped for entry in layer.clocks]
        with self._lock:
            # Clocks recorded in these scopes may come from their own
            # rolled-back writes: forget them and read the database again.
            for state, _ in recorded:
                self._clocks.pop(state, None)
        for state, clock in recorded:
            obj = state.obj()
            if obj is not None and state.session is session and state.persistent:
                session.expire(obj, [clock])

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
