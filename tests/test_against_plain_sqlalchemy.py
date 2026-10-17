"""The oracle test. Random programs run twice: on plain SQLAlchemy, and with
QuietUpdates installed. The library may only delay bookkeeping writes, so:

- after every step, each session sees the same newest values on both sides,
  through ``quiet.value`` and through the ORM attributes themselves;
- after a final real write to every row, the two databases are identical.

A program mixes bookkeeping-only changes, data changes, clock ticks,
SAVEPOINTs that are released or rolled back, commits, rollbacks and sessions
closed without a commit. The seed is in the failure message.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import DateTime, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from quiet_updates import QuietUpdates

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
PROGRAMS = int(os.environ.get("QU_ORACLE_PROGRAMS", 300))  # set it higher for a deeper run
STEPS = int(os.environ.get("QU_ORACLE_STEPS", 40))
OPS = ["tick", "stamp", "stamp", "count", "count", "price", "begin", "release", "rollback_sp",
       "commit", "rollback", "close"]


class Base(DeclarativeBase):
    pass


class Row(Base):
    __tablename__ = "qu_model_rows"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    title: Mapped[str] = mapped_column(String(20))
    price: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    countdown: Mapped[int] = mapped_column(Integer)


def make_engine():
    engine = create_engine("sqlite://")

    @event.listens_for(engine, "connect")
    def _connect(dbapi_connection, record):
        dbapi_connection.isolation_level = None  # real SAVEPOINTs on pysqlite

    @event.listens_for(engine, "begin")
    def _begin(conn):
        conn.exec_driver_sql("BEGIN")

    Base.metadata.create_all(engine)
    with Session(engine) as s:
        for pk in (1, 2):
            s.add(Row(id=pk, title="t", price=0, checked_at=T0, countdown=0))
        s.commit()
    return engine


def utc(value):
    return value if value is None or value.tzinfo else value.replace(tzinfo=timezone.utc)


class Program:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.now = T0
        self.quiet = QuietUpdates(now=lambda: self.now).watch(
            Row, columns={"checked_at", "countdown"}, clock="checked_at",
            min_interval=timedelta(minutes=5),
        )
        self.lib_engine, self.ref_engine = make_engine(), make_engine()
        self.lib_factory = sessionmaker(self.lib_engine)
        self.ref_factory = sessionmaker(self.ref_engine)
        self.quiet.install(self.lib_factory)
        self.lib = self.ref = None
        self.savepoints: list = []
        self.log: list[str] = []

    def step(self) -> None:
        rng = self.rng
        if self.lib is None:
            self.lib, self.ref = self.lib_factory(), self.ref_factory()
            self.log.append("open")
            return
        op, pk, value = rng.choice(OPS), rng.choice((1, 2)), rng.randrange(1000)
        self.log.append(f"{op} {pk} {value}")
        if op == "tick":
            self.now += timedelta(minutes=rng.choice((0, 1, 2, 3, 6)))
        elif op in ("stamp", "count", "price"):
            for session in (self.lib, self.ref):
                row = session.get(Row, pk)
                if op == "stamp":
                    row.checked_at, row.countdown = self.now, value
                elif op == "count":
                    row.countdown = value
                else:
                    row.price = value
                session.flush()
        elif op == "begin":
            self.savepoints.append((self.lib.begin_nested(), self.ref.begin_nested()))
        elif op in ("release", "rollback_sp") and self.savepoints:
            for savepoint in self.savepoints.pop():
                savepoint.commit() if op == "release" else savepoint.rollback()
        elif op in ("commit", "rollback", "close"):
            for session in (self.lib, self.ref):
                getattr(session, op)()
            self.savepoints.clear()
            if op == "close":
                self.lib = self.ref = None
                return
        self.check()

    def check(self) -> None:
        for pk in (1, 2):
            lib, ref = self.lib.get(Row, pk), self.ref.get(Row, pk)
            expected = (ref.price, ref.countdown, utc(ref.checked_at))
            seen = (lib.price, self.quiet.value(lib, "countdown"), utc(self.quiet.value(lib, "checked_at")))
            assert seen == expected, self.log
            assert (lib.price, lib.countdown, utc(lib.checked_at)) == expected, self.log  # the ORM agrees

    def settle(self) -> None:
        if self.lib is not None:
            for session in (self.lib, self.ref):
                session.commit()
                session.close()
        self.now += timedelta(minutes=10)
        for factory in (self.lib_factory, self.ref_factory):
            with factory() as session:
                for pk in (1, 2):
                    session.get(Row, pk).title = "settled"  # a real write carries every remembered value
                session.commit()

    def rows(self, engine) -> list:
        with Session(engine) as session:
            return [(r.title, r.price, r.countdown, utc(r.checked_at))
                    for r in session.query(Row).order_by(Row.id)]


def test_random_programs_match_plain_sqlalchemy():
    for seed in range(PROGRAMS):
        program = Program(seed)
        try:
            for _ in range(STEPS):
                program.step()
            program.settle()
            assert program.rows(program.lib_engine) == program.rows(program.ref_engine), (seed, program.log)
        except AssertionError as exc:
            raise AssertionError(f"seed {seed}: {exc}") from None
        finally:
            program.lib_engine.dispose()
            program.ref_engine.dispose()
