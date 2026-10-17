from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import DateTime, Integer, String, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from quiet_updates import QuietUpdates

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    price: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    countdown: Mapped[int] = mapped_column(Integer)


class Other(Base):
    __tablename__ = "others"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.at = at

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture()
def env():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    updates: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _count(conn, cursor, statement, params, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE"):
            updates.append(statement)

    factory = sessionmaker(engine)
    clock = Clock(T0)
    quiet = QuietUpdates(now=clock).watch(
        Item, columns={"checked_at", "countdown"}, clock="checked_at",
        min_interval=timedelta(minutes=5),
    ).install(factory)
    with factory() as s:
        s.add(Item(id=1, title="lamp", price=10, checked_at=T0, countdown=600))
        s.add(Other(id=1, checked_at=T0))
        s.commit()
    yield factory, quiet, clock, updates, engine
    quiet.uninstall(factory)


def stored(engine, model, pk):
    with Session(engine) as s:
        return s.get(model, pk)


def test_bookkeeping_only_change_inside_interval_is_not_written(env):
    factory, quiet, clock, updates, engine = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 540
        s.commit()
        assert quiet.value(item, "countdown") == 540  # remembered, not written
    assert updates == []
    assert quiet.stats.skipped == 1
    assert stored(engine, Item, 1).countdown == 600


def test_first_change_after_the_interval_is_written(env):
    factory, quiet, clock, updates, engine = env
    clock.at = T0 + timedelta(minutes=6)
    with factory() as s:
        item = s.get(Item, 1)
        item.checked_at = clock.at
        item.countdown = 240
        s.commit()
    assert len(updates) == 1
    assert quiet.stats.written == 1
    assert stored(engine, Item, 1).countdown == 240


def test_a_real_data_change_writes_everything(env):
    factory, quiet, clock, updates, engine = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.price = 12
        item.countdown = 540
        s.commit()
    assert len(updates) == 1
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (12, 540)


def test_unwatched_models_are_untouched(env):
    factory, quiet, clock, updates, engine = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Other, 1).checked_at = clock.at
        s.commit()
    assert len(updates) == 1
    assert quiet.stats.skipped == 0


def test_never_stamped_rows_are_written(env):
    factory, quiet, clock, updates, engine = env
    with factory() as s:
        s.add(Item(id=2, title="fan", price=5, checked_at=None, countdown=100))
        s.commit()
    with factory() as s:
        item = s.get(Item, 2)
        item.checked_at = clock.at
        s.commit()
    assert len(updates) == 1


def test_naive_timestamps_are_read_as_utc(env):
    factory, quiet, clock, updates, engine = env
    with factory() as s:
        s.add(Item(id=3, title="cup", price=1, checked_at=T0.replace(tzinfo=None), countdown=60))
        s.commit()
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        s.get(Item, 3).countdown = 59
        s.commit()
    assert updates == []


def test_clock_must_be_a_bookkeeping_column():
    with pytest.raises(ValueError):
        QuietUpdates().watch(Item, columns={"countdown"}, clock="checked_at",
                             min_interval=timedelta(minutes=1))


def test_skipped_values_reach_the_database_with_the_next_real_write(env):
    factory, quiet, clock, updates, engine = env
    clock.at = T0 + timedelta(minutes=1)
    with factory() as s:
        item = s.get(Item, 1)
        item.countdown = 540  # skipped
        s.commit()
        item.price = 11  # real change in the same session
        item.countdown = 530
        s.commit()
    row = stored(engine, Item, 1)
    assert (row.price, row.countdown) == (11, 530)
    with Session(engine) as s:
        assert s.scalar(select(Item.title).where(Item.id == 1)) == "lamp"
