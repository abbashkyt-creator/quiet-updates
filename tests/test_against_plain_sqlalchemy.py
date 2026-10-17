"""The oracle test. Random programs run twice: with QuietUpdates installed,
and on plain SQLAlchemy that applies the library's rule by hand (a change to
bookkeeping columns alone is not made while the stored clock is younger than
the interval). After every step the two sides must match exactly: the ORM
objects, and the rows each session's transaction sees in the database.

So a library that skips too much, skips too little, stretches the interval,
or leaves the object different from the database fails at the first step
where it does. A program mixes bookkeeping-only changes, data changes, clock
ticks, SAVEPOINTs that are released or rolled back, commits, rollbacks and
sessions closed without a commit. It runs on SQLite with real SAVEPOINTs; it
does not cover concurrent sessions or processes. The seed is in the failure.
"""
from __future__ import annotations

import os
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import DateTime, Integer, String, create_engine, event, select
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from quiet_updates import QuietUpdates

T0 = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)
INTERVAL = timedelta(minutes=5)
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


def snapshot(rows) -> list:
    return [(r.id, r.title, r.price, r.countdown, utc(r.checked_at)) for r in rows]


class Program:
    def __init__(self, seed: int) -> None:
        self.rng = random.Random(seed)
        self.now = T0
        self.quiet = QuietUpdates(now=lambda: self.now).watch(
            Row, columns={"checked_at", "countdown"}, clock="checked_at", min_interval=INTERVAL,
        )
        self.lib_engine, self.ref_engine = make_engine(), make_engine()
        self.lib_factory = sessionmaker(self.lib_engine)
        self.ref_factory = sessionmaker(self.ref_engine)
        self.quiet.install(self.lib_factory)
        self.lib = self.ref = None
        self.held: list = []  # like an application, keep the objects alive between steps
        self.savepoints: list = []
        self.log: list[str] = []

    def fresh(self, row) -> bool:
        """The rule, written out: the stored clock is younger than the interval."""
        return row.checked_at is not None and self.now - utc(row.checked_at) < INTERVAL

    def step(self) -> None:
        rng = self.rng
        if self.lib is None:
            self.lib, self.ref = self.lib_factory(), self.ref_factory()
            self.log.append("open")
            self.check()
            return
        op, pk, value = rng.choice(OPS), rng.choice((1, 2)), rng.randrange(1000)
        self.log.append(f"{op} {pk} {value}")
        if op == "tick":
            self.now += timedelta(minutes=rng.choice((0, 1, 2, 3, 6)))
        elif op in ("stamp", "count", "price"):
            rows = []
            for session in (self.lib, self.ref):
                row = session.get(Row, pk)
                rows.append(row)
                self.held.append(row)
                if session is self.ref and op != "price" and self.fresh(row):
                    continue  # the reference does by hand what the library does
                if op == "stamp":
                    row.checked_at, row.countdown = self.now, value
                elif op == "count":
                    row.countdown = value
                else:
                    row.price = value
                session.flush()
            assert snapshot(rows[:1]) == snapshot(rows[1:]), self.log  # the objects just changed
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
                self.held.clear()
        self.check()

    def check(self) -> None:
        if self.lib is None:  # no session: compare what is committed
            assert self.committed(self.lib_engine) == self.committed(self.ref_engine), self.log
            return
        for pk in (1, 2):  # the ORM objects
            lib, ref = self.lib.get(Row, pk), self.ref.get(Row, pk)
            assert snapshot([lib]) == snapshot([ref]), self.log
        # The rows each transaction sees, read from the database, not the identity map.
        assert self.in_transaction(self.lib) == self.in_transaction(self.ref), self.log

    @staticmethod
    def in_transaction(session) -> list:
        query = select(Row.id, Row.title, Row.price, Row.countdown, Row.checked_at).order_by(Row.id)
        return [(i, t, p, c, utc(at)) for i, t, p, c, at in session.execute(query)]

    @staticmethod
    def committed(engine) -> list:
        with Session(engine) as session:
            return snapshot(session.scalars(select(Row).order_by(Row.id)))


def test_random_programs_match_the_rule_on_plain_sqlalchemy():
    for seed in range(PROGRAMS):
        program = Program(seed)
        try:
            for _ in range(STEPS):
                program.step()
        except AssertionError as exc:
            raise AssertionError(f"seed {seed}: {exc}") from None
        finally:
            for session in (program.lib, program.ref):
                if session is not None:
                    session.close()
            program.lib_engine.dispose()
            program.ref_engine.dispose()
