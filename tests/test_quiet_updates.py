"""Tests count the UPDATE statements the database actually receives and read
rows back through a separate session, instead of trusting the library's own
statistics. Set QU_TEST_DB_URL to run them against Postgres (CI does)."""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import DateTime, Integer, String, create_engine, event, func
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

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


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def real_sqlite_transactions(engine) -> None:
    """SQLAlchemy's documented pysqlite workaround: without it SAVEPOINTs, and
    so the rollback tests, do not behave as they do on a real server. WAL lets
    one session read while another commits."""

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


def test_evicted_values_cannot_stretch_the_interval(env):
    # Regression for 0.2.0: the stored clock was kept with the skipped values,
    # so evicting them made the object's own skipped clock look stored.
    engine, factory, quiet, clock, updates = env
    quiet.max_remembered = 1
    with factory() as s:
        s.add(Item(id=2, title="fan", price=5, checked_at=T0, countdown=600))
        s.commit()
    with Session(engine, expire_on_commit=False) as s:
        quiet.install(s)
        try:
            items = [s.get(Item, 1), s.get(Item, 2)]
            for minute in range(1, 8):
                clock.at = T0 + timedelta(minutes=minute)
                for item in items:  # each skip evicts the other row
                    item.checked_at = clock.at
                    s.commit()
        finally:
            quiet.uninstall(s)
    for pk in (1, 2):
        assert utc(stored(engine, Item, pk).checked_at) >= T0 + timedelta(minutes=5)


@pytest.mark.parametrize("end", ["rollback", "close"])
def test_a_write_that_is_not_committed_gives_the_skipped_values_back(env, end):
    # Regression for 0.2.0: a write took the skipped values out of memory at
    # flush time, so a rollback, or a session closed without a commit, lost them.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 540
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11
        s.flush()  # this UPDATE carries the skipped values
        if end == "rollback":
            s.rollback()
    with factory() as s:
        s.get(Item, 1).title = "lamp 2"
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.title, row.price, row.countdown) == ("lamp 2", 10, 540)
    assert utc(row.checked_at) == clock.at


def test_skips_in_a_rolled_back_transaction_are_forgotten(env):
    # Regression for 0.2.0: a value skipped and then rolled back was still
    # written with the row's next UPDATE.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.countdown = 540
        s.flush()
        assert quiet.value(item, "countdown") == 540
        s.rollback()
        assert quiet.value(item, "countdown") == 600
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    assert stored(engine, Item, 1).countdown == 600


def test_a_clock_read_inside_a_rolled_back_transaction_is_not_trusted(env):
    # Regression for 0.2.0: a skip right after a write remembered the clock
    # that write had stored, and kept trusting it after the rollback.
    engine, factory, quiet, clock, updates = env
    with Session(engine, expire_on_commit=False) as s:
        quiet.install(s)
        try:
            item = s.get(Item, 1)
            clock.at = T0 + timedelta(minutes=6)
            item.checked_at = clock.at  # stale, so written
            s.flush()
            clock.at = T0 + timedelta(minutes=7)
            item.countdown = 100  # fresh against the clock just written: skipped
            s.flush()
            s.rollback()  # the database holds T0 again
            clock.at = T0 + timedelta(minutes=8)
            item.checked_at = clock.at
            s.commit()
        finally:
            quiet.uninstall(s)
    assert utc(stored(engine, Item, 1).checked_at) == T0 + timedelta(minutes=8)


def test_a_rolled_back_savepoint_gives_back_what_its_write_took(env):
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.countdown = 540  # skipped, outside the savepoint
        s.flush()
        savepoint = s.begin_nested()
        item.price = 11  # this UPDATE carries countdown=540
        s.flush()
        savepoint.rollback()
        s.commit()
    assert stored(engine, Item, 1).countdown == 600  # the skip is still only remembered
    with factory() as s:
        s.get(Item, 1).title = "lamp 2"
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (10, 540)


def test_a_write_without_the_clock_keeps_the_stored_clock(env):
    # Regression for 0.2.1: every write forgot the stored clock, even one that
    # did not store the clock. After an eviction the object kept its skipped
    # clock, so that row looked fresh forever.
    engine, factory, quiet, clock, updates = env
    quiet.max_remembered = 2
    with factory() as s:
        for pk in (2, 3):
            s.add(Item(id=pk, title="x", price=1, checked_at=T0, countdown=600))
        s.commit()
    with Session(engine, expire_on_commit=False) as s:
        quiet.install(s)
        try:
            items = [s.get(Item, pk) for pk in (1, 2, 3)]
            for minute in range(1, 11):
                clock.at = T0 + timedelta(minutes=minute)
                for item in items:  # the third skip evicts the first row
                    item.checked_at = clock.at
                    s.commit()
                for item in items:  # price only: these UPDATEs do not store the clock
                    item.price += 1
                    s.commit()
        finally:
            quiet.uninstall(s)
    for pk in (1, 2, 3):
        assert utc(stored(engine, Item, pk).checked_at) >= T0 + timedelta(minutes=5)


def test_a_remembered_value_never_overwrites_a_newer_write_from_another_process(env):
    # Regression for 0.2.1: worker A carried its old skipped values into its
    # next UPDATE and overwrote what worker B had written since.
    engine, factory_a, quiet_a, clock, updates = env
    quiet_b = QuietUpdates(now=clock).watch(
        Item, columns={"checked_at", "countdown"}, clock="checked_at",
        min_interval=timedelta(minutes=5),
    )
    factory_b = sessionmaker(engine)
    quiet_b.install(factory_b)
    try:
        clock.at = T0 + timedelta(minutes=1)
        with factory_a() as s:
            item = s.get(Item, 1)
            item.checked_at, item.countdown = clock.at, 540  # A skips
            s.commit()
        clock.at = T0 + timedelta(minutes=6)
        with factory_b() as s:
            item = s.get(Item, 1)
            item.checked_at, item.countdown = clock.at, 240  # stale for B: written
            s.commit()
        clock.at = T0 + timedelta(minutes=7)
        with factory_a() as s:
            s.get(Item, 1).price = 11  # A's next UPDATE
            s.commit()
    finally:
        quiet_b.uninstall(factory_b)
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 240)
    assert utc(row.checked_at) == T0 + timedelta(minutes=6)


def test_a_rolled_back_empty_savepoint_gives_nothing_back(env):
    # Regression for 0.2.1: a SAVEPOINT rollback undid the whole session's
    # journal, so values an earlier write had stored were sent again.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 1).countdown = 540  # skipped
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11  # carries countdown=540
        s.flush()
        s.begin_nested().rollback()  # an empty SAVEPOINT
        s.commit()
    with factory() as s:
        s.get(Item, 1).title = "lamp 2"
        s.commit()
    assert "countdown" not in updates[-1]


def test_a_skip_inside_a_rolled_back_savepoint_is_forgotten(env):
    # Regression for 0.2.1: the skip survived its rolled-back SAVEPOINT and
    # was written with the row's next UPDATE.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        savepoint = s.begin_nested()
        item.countdown = 540
        s.flush()
        savepoint.rollback()
        assert quiet.value(item, "countdown") == 600
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    assert stored(engine, Item, 1).countdown == 600


def test_releasing_a_savepoint_keeps_what_a_rollback_must_give_back(env):
    # Regression for 0.2.2 (B1): SQLAlchemy fires after_commit when a SAVEPOINT
    # is released, and that wiped the journal, so a later rollback lost the
    # skipped value a write had taken.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 1).countdown = 540  # skipped
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11  # this UPDATE takes countdown=540
        s.flush()
        with s.begin_nested():  # released on success
            pass
        s.rollback()
    with factory() as s:
        s.get(Item, 1).title = "lamp 2"
        s.commit()
    assert stored(engine, Item, 1).countdown == 540


def test_a_skip_in_a_released_savepoint_rolls_back_with_its_parent(env):
    # Regression for 0.2.2 (B2).
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        with s.begin_nested():
            item.countdown = 540
            s.flush()  # skipped inside the SAVEPOINT, which is then released
        s.rollback()
        assert quiet.value(s.get(Item, 1), "countdown") == 600
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    assert stored(engine, Item, 1).countdown == 600


def test_a_skip_that_did_not_move_the_clock_never_overwrites_a_newer_write(env):
    # Regression for 0.2.2 (B3): the guard was only built when the skip moved
    # the clock, so a countdown-only skip overwrote another process's write.
    engine, factory_a, quiet_a, clock, updates = env
    quiet_b = QuietUpdates(now=clock).watch(
        Item, columns={"checked_at", "countdown"}, clock="checked_at",
        min_interval=timedelta(minutes=5),
    )
    factory_b = sessionmaker(engine)
    quiet_b.install(factory_b)
    try:
        clock.at = T0 + timedelta(minutes=1)
        with factory_a() as s:
            s.get(Item, 1).countdown = 540  # A skips; the clock is untouched
            s.commit()
        clock.at = T0 + timedelta(minutes=6)
        with factory_b() as s:
            item = s.get(Item, 1)
            item.checked_at, item.countdown = clock.at, 240  # B writes
            s.commit()
        with factory_a() as s:
            s.get(Item, 1).price = 11
            s.commit()
    finally:
        quiet_b.uninstall(factory_b)
    assert (stored(engine, Item, 1).price, stored(engine, Item, 1).countdown) == (11, 240)


def test_another_sessions_uncommitted_skip_is_not_written(env):
    # Regression for 0.2.2 (B4): a skip was shared with every session before
    # it committed, so another session's write stored a value whose own
    # transaction then rolled back.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s1, factory() as s2:
        s1.get(Item, 1).countdown = 540
        s1.flush()  # skipped, not committed
        item = s2.get(Item, 1)
        assert quiet.value(item, "countdown") == 600
        item.price = 11
        s2.commit()
        s1.rollback()
    assert stored(engine, Item, 1).countdown == 600


def test_a_sql_expression_is_written_not_remembered(env):
    # Regression for 0.2.2 (B5): a skipped func.now() was kept as a Python
    # object, and every later write of the row failed.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 1).countdown = func.abs(-540)
        s.commit()
    assert len(updates) == 1
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    assert (stored(engine, Item, 1).price, stored(engine, Item, 1).countdown) == (11, 540)


def test_setting_a_column_back_to_its_stored_value_after_a_reload_counts(env):
    # Found by the oracle test in 0.3.0 development: a reloaded object showed
    # the stored 600, so setting 600 was no change to SQLAlchemy, and the
    # remembered 540 was written later instead.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 1).countdown = 540  # skipped
        s.commit()
    with factory() as s:
        item = s.get(Item, 1)
        assert item.countdown == 540  # a loaded object shows the remembered value
        item.countdown = 600
        s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    assert stored(engine, Item, 1).countdown == 600


def test_skipped_increments_in_one_process_add_up(env):
    # Round-4 review (B6): three skipped `+= 1` were stored as one. A loaded
    # object now shows the remembered value, so each session adds to it.
    engine, factory, quiet, clock, updates = env
    clock.at = T0 + timedelta(minutes=1)
    for _ in range(3):
        with factory() as s:
            s.get(Item, 1).countdown += 1  # skipped each time
            s.commit()
    with factory() as s:
        s.get(Item, 1).price = 11
        s.commit()
    assert quiet.stats.skipped == 3
    assert stored(engine, Item, 1).countdown == 603


def test_the_stored_clock_is_read_once_per_object(env):
    # Regression for 0.2.0: the stored clock of an expired object was read
    # from the database on every flush, even when it was already known.
    engine, factory, quiet, clock, updates = env
    clock_reads: list[str] = []

    def _count(conn, cursor, statement, params, context, executemany):
        sql = statement.lstrip().upper()
        if sql.startswith("SELECT") and "TITLE" not in sql:
            clock_reads.append(statement)

    event.listen(engine, "before_cursor_execute", _count)
    try:
        with factory() as s:  # expire_on_commit=True: each commit expires the object
            item = s.get(Item, 1)
            for minute in range(1, 5):
                clock.at = T0 + timedelta(minutes=minute)
                item.checked_at = clock.at
                s.commit()
    finally:
        event.remove(engine, "before_cursor_execute", _count)
    assert updates == []
    assert clock_reads == []


def test_clock_must_be_a_bookkeeping_column():
    with pytest.raises(ValueError):
        QuietUpdates().watch(Item, columns={"countdown"}, clock="checked_at",
                             min_interval=timedelta(minutes=1))
