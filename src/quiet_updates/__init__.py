"""Skip SQLAlchemy UPDATEs that only touch bookkeeping columns.

Many tables carry columns that change on every read of the outside world but
mean nothing on their own: "last checked at", a countdown. Writing them every
time produces a new row version, WAL, index work and lock traffic, even though
the data the row exists for did not change.

QuietUpdates installs a ``before_flush`` hook. For a watched model, a change
that touches only bookkeeping columns is discarded while the row's *stored*
clock is younger than ``min_interval``: no UPDATE is sent, and the object goes
back to the values the database holds. The first such change after the
interval is written. A change to any other column is written as usual,
together with the bookkeeping values set in the same flush.

The library keeps no state between flushes. The object always matches the
database, so transactions, SAVEPOINTs, other sessions and other processes
behave exactly as they do without it. If you need the newest bookkeeping
value, keep it in your application.

Only ORM flushes are seen. Core ``update()`` statements bypass the hook.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Iterable

from sqlalchemy import event, inspect, select
from sqlalchemy.orm import Session, SynonymProperty
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.sql.expression import ClauseElement

__all__ = ["QuietUpdates", "QuietStats"]
__version__ = "0.4.1"


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
        below ``min_interval``, a change to bookkeeping columns alone is
        discarded instead of written.
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

    # -- internals ---------------------------------------------------------

    def _rule_for(self, obj: object) -> _Rule | None:
        for cls in type(obj).__mro__:
            rule = self._rules.get(cls)
            if rule is not None:
                return rule
        return None

    def _before_flush(self, session: Session, flush_context, instances) -> None:
        dirty = session.dirty
        # session.flush(objects=[...]) flushes only those: leave the rest pending.
        candidates = list(dirty) if instances is None else [obj for obj in instances if obj in dirty]
        quiet = []
        for obj in candidates:
            rule = self._rule_for(obj)
            if rule is None or not session.is_modified(obj, include_collections=False):
                continue
            state = inspect(obj)
            synonyms = {prop.key for prop in state.mapper.attrs if isinstance(prop, SynonymProperty)}
            changed = {
                a.key: a.history for a in state.attrs
                if a.key not in synonyms and a.history.has_changes()  # a synonym shows its column's change
            }
            if not changed:
                continue
            if set(changed) <= rule.columns and not any(
                isinstance(value, ClauseElement)  # a SQL expression is always written
                for history in changed.values()
                for value in history.added
            ):
                quiet.append((obj, state, rule, changed))
            else:
                self.stats.written += 1
        stored = self._stored_clocks(session, [(state, rule.clock) for _, state, rule, _ in quiet])
        for obj, state, rule, changed in quiet:
            if self._fresh(stored[id(state)], rule):
                self._discard(session, obj, changed)
                self.stats.skipped += 1
            else:
                self.stats.written += 1

    @staticmethod
    def _discard(session: Session, obj, changed: dict) -> None:
        """Put the object back to what the database holds, so nothing is written."""
        reload = []
        for column, history in changed.items():
            if history.deleted:
                set_committed_value(obj, column, history.deleted[0])
            else:
                reload.append(column)  # set while expired: the stored value is not known here
        if reload:
            session.expire(obj, reload)

    @staticmethod
    def _stored_clocks(session: Session, wanted: list) -> dict:
        """id(state) -> the clock as SQLAlchemy last loaded or flushed it.

        Expired or never-loaded clocks (possibly set since) have no stored value
        in SQLAlchemy, so they are read from the database: one query per model
        with a single-column primary key, one per row otherwise.
        """
        found, missing = {}, {}
        for state, clock in wanted:
            history = state.attrs[clock].history
            if history.deleted:
                found[id(state)] = history.deleted[0]
            elif history.unchanged:
                found[id(state)] = history.unchanged[0]
            else:
                missing.setdefault((state.mapper, clock), []).append(state)
        with session.no_autoflush:
            for (mapper, clock), states in missing.items():
                column = getattr(mapper.class_, clock)
                keys = mapper.primary_key
                if len(keys) == 1:
                    rows = session.execute(
                        select(keys[0], column).where(keys[0].in_([s.identity[0] for s in states]))
                    ).all()
                    by_key = {key: value for key, value in rows}
                    for s in states:
                        found[id(s)] = by_key.get(s.identity[0])
                else:
                    for s in states:
                        query = select(column).where(*[k == v for k, v in zip(keys, s.identity)])
                        found[id(s)] = session.execute(query).scalar_one_or_none()
        return found

    def _fresh(self, stored: datetime | None, rule: _Rule) -> bool:
        if stored is None:
            return False  # never stamped: write it
        # abs(): a stored clock ahead of now() (a clock in another timezone,
        # read as UTC) is trusted for one interval only, not until it is reached.
        return abs(_aware(self.now()) - _aware(stored)) < rule.min_interval
