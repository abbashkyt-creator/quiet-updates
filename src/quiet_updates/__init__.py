"""Skip SQLAlchemy UPDATEs that only touch bookkeeping columns.

Many tables carry columns that change on every read of the outside world but
mean nothing on their own: "last checked at", a countdown, a poll counter.
Writing them every time produces a new row version, WAL, index work and lock
traffic, even though the data the row exists for did not change.

QuietUpdates installs a ``before_flush`` hook. For a watched model, an UPDATE
whose changed columns are all bookkeeping columns is dropped while the row's
stored timestamp is still younger than ``min_interval``. The skipped values are
remembered (``quiet.value(obj, column)`` returns the newest one, even after the
session expired the object), and the next real change, or the first change
after the interval, writes the row as usual.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import event, inspect
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

__all__ = ["QuietUpdates", "QuietStats"]
__version__ = "0.1.0"


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
    """Register models, then ``install`` on a Session class or sessionmaker."""

    now: Callable[[], datetime] = _utc_now
    stats: QuietStats = field(default_factory=QuietStats)
    _rules: dict[type, _Rule] = field(default_factory=dict)
    _memory: dict[tuple, dict[str, object]] = field(default_factory=dict)

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
        remembered = self._memory.get(self._key(obj), {})
        if column in remembered:
            return remembered[column]
        return getattr(obj, column)

    @staticmethod
    def _key(obj: object) -> tuple:
        return (type(obj), inspect(obj).identity)

    # -- internals ---------------------------------------------------------

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
                attr.key: attr.history
                for attr in state.attrs
                if attr.history.has_changes()
            }
            if not changed:
                continue
            if not set(changed) <= rule.columns or not self._still_fresh(state, rule):
                self.stats.written += 1
                self._memory.pop(self._key(obj), None)
                continue
            remembered = self._memory.setdefault(self._key(obj), {})
            for key, history in changed.items():
                new = history.added[0] if history.added else None
                remembered[key] = new
                # Accept the new value on the object without scheduling a write.
                set_committed_value(obj, key, new)
            self.stats.skipped += 1

    def _still_fresh(self, state, rule: _Rule) -> bool:
        history = state.attrs[rule.clock].history
        if history.deleted:
            stored = history.deleted[0]
        elif history.unchanged:
            stored = history.unchanged[0]
        else:
            stored = None
        if stored is None:
            return False  # never stamped: write it
        return self.now() - _aware(stored) < rule.min_interval
