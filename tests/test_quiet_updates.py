"""Tests count the UPDATE statements the database actually receives and read
rows back through a separate session, instead of trusting the library's own
statistics. Set QU_TEST_DB_URL to run them against Postgres (CI does)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import DateTime, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from quiet_updates import QuietUpdates

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DB_URL = os.environ.get("QU_TEST_DB_URL", "sqlite://")


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "qu_items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(50))
    price: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    countdown: Mapped[int] = mapped_column(Integer)


class Other(Base):
    __tablename__ = "qu_others"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture()
def env():
    engine = create_engine(DB_URL)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    updates: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)

    clock = Clock(T0)
    quiet = QuietUpdates(now=clock).watch(
        Item, columns={"checked_at", "countdown"}, clock="checked_at",
        min_interval=timedelta(minutes=5),
    )
    factory = sessionmaker(engine)
    quiet.install(factory)
    with factory() as s:
        s.add(Item(id=1, title="lamp", price=10, checked_at=T0, countdown=600))
        s.add(Other(id=1, checked_at=T0))
        s.commit()
    yield engine, factory, quiet, clock, updates
    quiet.uninstall(factory)
    Base.metadata.drop_all(engine)
    engine.dispose()


def stored(engine, model, pk):
    with Session(engine) as s:
        return s.get(model, pk)


def utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def test_bookkeeping_only_change_inside_interval_is_not_written(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 540
        s.commit()
        assert quiet.value(item, "countdown") == 540
    assert updates == []
    assert quiet.stats.skipped == 1
    assert stored(engine, Item, 1).countdown == 600


def test_first_change_after_the_interval_is_written(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=6)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 240
        s.commit()
    assert len(updates) == 1
    assert stored(engine, Item, 1).countdown == 240


def test_a_real_data_change_writes_everything(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.price = 12
        item.countdown = 540
        s.commit()
    assert len(updates) == 1
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (12, 540)


def test_skipped_values_are_written_with_the_next_real_change(env):
    # Regression for 0.1.0: the skipped countdown and clock were dropped when a
    # later UPDATE changed only the price.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 540
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11  # nothing else touched
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 540)
    assert utc(row.checked_at) == clock.at


def test_skip_then_real_change_in_the_same_session_writes_the_newest_values(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.countdown = 540  # skipped
        s.commit()
        item.price = 11
        item.countdown = 530
        s.commit()
    assert len(updates) == 1  # setting attributes must not trigger an extra flush
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 530)


def test_repeated_skips_cannot_stretch_past_the_interval(env):
    # Regression for 0.1.0: with expire_on_commit=False the freshness check
    # read the in-memory clock it had just overwritten, so the row was never
    # written again.
    engine, factory, quiet, clock, updates = env
    with Session(engine, expire_on_commit=False) as s:
        quiet.install(s)
        try:
            item = s.get(Item, 1)
            for minute in range(1, 8):
                clock.at = T0 + timedelta(minutes=minute)
                item.checked_at = clock.at
                item.countdown = 600 - 60 * minute
                s.commit()
        finally:
            quiet.uninstall(s)
    assert len(updates) >= 1
    row = stored(engine, Item, 1)
    assert utc(row.checked_at) >= T0 + timedelta(minutes=5)


def test_expired_objects_compare_against_the_stored_clock(env):
    engine, factory, quiet, clock, updates = env
    with factory() as s:  # expire_on_commit=True, the default
        item = s.get(Item, 1)
        for minute in range(1, 8):
            clock.at = T0 + timedelta(minutes=minute)
            item.checked_at = clock.at  # set on an expired object
            s.commit()
    assert len(updates) >= 1
    assert utc(stored(engine, Item, 1).checked_at) >= T0 + timedelta(minutes=5)


def test_unwatched_models_are_untouched(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Other, 1).checked_at = clock.at
        s.commit()
    assert len(updates) == 1
    assert quiet.stats.skipped == 0


def test_never_stamped_rows_are_written(env):
    engine, factory, quiet, clock, updates = env
    with factory() as s:
        s.add(Item(id=2, title="fan", price=5, checked_at=None, countdown=100))
        s.commit()
    with factory() as s:
        s.get(Item, 2).checked_at = clock.at
        s.commit()
    assert len(updates) == 1


def test_a_naive_clock_is_read_as_utc(env):
    engine, factory, quiet, clock, updates = env
    clock.at = (T0 + timedelta(minutes=1)).replace(tzinfo=None)
    with factory() as s:
        s.get(Item, 1).countdown = 59
        s.commit()
    assert updates == []


def test_memory_is_bounded(env):
    engine, factory, quiet, clock, updates = env
    quiet.max_remembered = 2
    with factory() as s:
        for i in range(10, 15):
            s.add(Item(id=i, title="x", price=1, checked_at=T0, countdown=60))
        s.commit()
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        for i in range(10, 15):
            s.get(Item, i).countdown = 59
        s.commit()
    assert len(quiet._memory) == 2


def test_clock_must_be_a_bookkeeping_column():
    with pytest.raises(ValueError):
        QuietUpdates().watch(Item, columns={"countdown"}, clock="checked_at",
                             min_interval=timedelta(minutes=1))
