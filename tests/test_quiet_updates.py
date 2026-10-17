"""Tests count the UPDATE statements the database actually receives and read
rows back through a separate session, instead of trusting the library's own
statistics. Set QU_TEST_DB_URL to run them against Postgres (CI does)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import DateTime, Integer, String, create_engine, event, func, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, load_only, mapped_column, sessionmaker, synonym

from quiet_updates import QuietUpdates

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
DB_URL = os.environ.get("QU_TEST_DB_URL")


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


class Polled(Base):
    __tablename__ = "qu_polled"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    ticks: Mapped[int] = mapped_column(Integer)
    tick_count = synonym("ticks")


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def real_sqlite_transactions(engine) -> None:
    """SQLAlchemy's documented pysqlite workaround: without it SAVEPOINTs do
    not behave as they do on a real server. WAL lets one session read while
    another commits."""

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, record):
        dbapi_connection.isolation_level = None
        dbapi_connection.execute("PRAGMA journal_mode=WAL")

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")


@pytest.fixture()
def env(tmp_path):
    engine = create_engine(DB_URL or f"sqlite:///{tmp_path / 'qu.db'}")
    if engine.dialect.name == "sqlite":
        real_sqlite_transactions(engine)
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


def test_a_bookkeeping_only_change_inside_the_interval_is_discarded(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 540
        s.flush()
        assert (item.countdown, utc(item.checked_at)) == (600, T0)  # the object matches the database
        s.commit()
    assert updates == []
    assert quiet.stats.skipped == 1
    assert stored(engine, Item, 1).countdown == 600


def test_the_first_change_after_the_interval_is_written(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=6)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 240
        s.commit()
    assert len(updates) == 1
    assert stored(engine, Item, 1).countdown == 240


def test_a_data_change_writes_the_bookkeeping_set_with_it(env):
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


def test_a_discarded_change_is_not_written_later(env):
    # The contract since 0.4.0: nothing is remembered. A later data change
    # writes only what is set with it.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 1).countdown = 540
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 600)


def test_repeated_changes_cannot_stretch_past_the_interval(env):
    # Regression for 0.1.0: with expire_on_commit=False the freshness check
    # read the clock the skip had just put on the object.
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
    assert len(updates) == 1
    assert utc(stored(engine, Item, 1).checked_at) == T0 + timedelta(minutes=5)


def test_a_change_set_on_an_expired_object_compares_against_the_stored_clock(env):
    engine, factory, quiet, clock, updates = env
    with factory() as s:  # expire_on_commit=True: each commit expires the object
        item = s.get(Item, 1)
        for minute in range(1, 8):
            clock.at = T0 + timedelta(minutes=minute)
            item.checked_at = clock.at  # set without loading first
            s.commit()
    assert len(updates) == 1
    assert utc(stored(engine, Item, 1).checked_at) == T0 + timedelta(minutes=5)


def test_a_clock_that_was_not_loaded_still_keeps_the_interval(env):
    # Regression for 0.3.0 (round-5 review, N2): with load_only the clock was
    # never loaded, and rows stamped every minute were never written.
    engine, factory, quiet, clock, updates = env
    for minute in range(1, 31):
        clock.at = T0 + timedelta(minutes=minute)
        with factory() as s:
            item = s.scalars(select(Item).options(load_only(Item.price))).one()
            item.checked_at = clock.at
            s.commit()
    assert len(updates) == 6  # minutes 5, 10, 15, 20, 25 and 30
    assert utc(stored(engine, Item, 1).checked_at) == T0 + timedelta(minutes=30)


def test_merge_without_load_works_with_and_without_the_library(env):
    # Regression for 0.3.0 (round-5 review, N1): Session.merge(load=False)
    # raised AttributeError for any watched model.
    engine, factory, quiet, clock, updates = env
    with Session(engine) as plain:
        detached = plain.get(Item, 1)
    for session in (Session(engine), factory()):
        with session:
            merged = session.merge(detached, load=False)
            assert merged.price == 10


def test_rollbacks_and_savepoints_behave_as_without_the_library(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        with s.begin_nested():
            item.price = 11
        savepoint = s.begin_nested()
        item.countdown = 540  # discarded
        s.flush()
        savepoint.rollback()
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 600)


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


def test_a_sql_expression_is_always_written(env):
    # Regression for 0.2.2 (round-4 review, B5).
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 1).countdown = Item.countdown - 60
        s.commit()
    assert len(updates) == 1
    assert stored(engine, Item, 1).countdown == 540


def test_another_session_sees_the_database(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s1, factory() as s2:
        s1.get(Item, 1).countdown = 540
        s1.flush()  # discarded
        assert s2.get(Item, 1).countdown == 600
        s2.get(Item, 1).price = 11
        s2.commit()
        s1.rollback()
    assert (stored(engine, Item, 1).price, stored(engine, Item, 1).countdown) == (11, 600)


def test_a_synonym_does_not_turn_the_library_off(env):
    # Regression for 0.4.0 (round-6 review): the synonym's own key showed up as
    # a changed attribute that is not bookkeeping, so every change was written.
    engine, factory, quiet, clock, updates = env
    quiet.watch(Polled, columns={"checked_at", "ticks"}, clock="checked_at", min_interval=timedelta(minutes=5))
    with factory() as s:
        s.add(Polled(id=1, checked_at=T0, ticks=0))
        s.commit()
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Polled, 1).tick_count = 5
        s.commit()
    assert updates == []


def test_a_stored_clock_ahead_of_now_is_trusted_for_one_interval_only(env):
    # Regression for 0.4.0 (round-6 review): a clock 3 hours ahead (local time
    # read as UTC) looked fresh until now() caught up, so nothing was written.
    engine, factory, quiet, clock, updates = env
    with factory() as s:
        s.add(Item(id=2, title="fan", price=5, checked_at=T0 + timedelta(hours=3), countdown=100))
        s.commit()
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 2)
        item.checked_at, item.countdown = clock.at, 90
        s.commit()
    assert len(updates) == 1
    assert utc(stored(engine, Item, 2).checked_at) == clock.at


def test_flushing_other_objects_leaves_this_ones_changes_pending(env):
    # Regression for 0.4.0 (round-6 review): session.flush([other]) discarded
    # changes on objects outside the flush.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item, other = s.get(Item, 1), s.get(Other, 1)
        item.countdown = 540
        other.checked_at = clock.at
        s.flush([other])
        item.price = 11  # now a data change: the countdown goes with it
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 540)


def test_expired_clocks_are_read_in_one_query_per_model(env):
    engine, factory, quiet, clock, updates = env
    with factory() as s:
        for pk in (2, 3, 4):
            s.add(Item(id=pk, title="x", price=1, checked_at=T0, countdown=60))
        s.commit()
    clock_reads: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        sql = statement.lstrip().upper()
        if sql.startswith("SELECT") and "TITLE" not in sql:
            clock_reads.append(statement)

    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        items = [s.get(Item, pk) for pk in (1, 2, 3, 4)]
        s.commit()  # expires them
        event.listen(engine, "before_cursor_execute", _count)
        try:
            for item in items:
                item.checked_at = clock.at  # set while expired
            s.commit()
        finally:
            event.remove(engine, "before_cursor_execute", _count)
    assert updates == []
    assert len(clock_reads) == 1


def test_the_clock_must_be_a_bookkeeping_column():
    with pytest.raises(ValueError):
        QuietUpdates().watch(Item, columns={"countdown"}, clock="checked_at",
                             min_interval=timedelta(minutes=1))
