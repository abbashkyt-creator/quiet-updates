# quiet-updates

Skip SQLAlchemy UPDATEs that change nothing but bookkeeping.

Many tables carry columns that move every time you look at the outside world: a "last checked at" timestamp, a countdown. Writing them on every pass creates a new row version every time, with WAL, index maintenance and lock traffic, even when the data the row exists for did not change.

In my own system, re-stamping unchanged rows made up most of the listings table's writes. Dropping those writes, along with a few other changes, more than halved Postgres WAL ([the write-up](https://github.com/abbashkyt-creator/engineering-notes/blob/main/02-write-amplification.md)). This library is that idea as a small, general SQLAlchemy extension. It is written from scratch and shares no code with that system.

## Usage

```python
from datetime import timedelta
from quiet_updates import QuietUpdates
from sqlalchemy.orm import sessionmaker

Session = sessionmaker(engine)

quiet = QuietUpdates().watch(
    Item,
    columns={"checked_at", "countdown"},  # bookkeeping columns
    clock="checked_at",                   # when the row was last written
    min_interval=timedelta(minutes=5),
).install(Session)

with Session() as s:
    item = s.get(Item, 1)
    item.checked_at = now()
    item.countdown = 540
    s.commit()        # no UPDATE: only bookkeeping changed, and the row was written under 5 minutes ago
    item.countdown    # the stored value again: the object always matches the database
    quiet.stats       # QuietStats(skipped=1, written=0)
```

## The rule

For a watched model, at flush time:

| What changed | Stored `clock` age | Result |
|---|---|---|
| Only bookkeeping columns | below `min_interval` | Discarded. No UPDATE, and the object goes back to the stored values. |
| Only bookkeeping columns | at or above `min_interval`, or never set | Written |
| Any other column | any | Written, with the bookkeeping values set in the same flush |

"Stored `clock`" means the value SQLAlchemy last loaded or flushed for the row, not one set on the object since. Normally that is what the database holds in the current transaction. With `expire_on_commit=False` or `merge(load=False)`, it can be a snapshot from an earlier transaction. A discarded change never moves it, so a row stamped every minute is still written once per `min_interval`. If the clock is not loaded (expired, or left out by `load_only`), it is read from the database, in one query per model. A stored clock ahead of `now()`, such as local time read as UTC, is trusted for one interval only.

Each flush is judged on its own. Autoflush runs a flush before any query or lazy load, so bookkeeping set before one is discarded there, and a later data change is written without it. Set bookkeeping last, or use `with session.no_autoflush:`.

What you give up: between writes, the database holds bookkeeping values up to `min_interval` old, and a discarded value is gone. If your application needs the newest value, it keeps it itself. In my own system the scheduler already kept "last checked" in memory.

The library keeps no state between flushes, so transactions, SAVEPOINTs, other sessions and other processes behave exactly as they do without it. A SQL expression (`row.count = Row.count + 1`) is always written. Unwatched models are untouched. Naive datetimes are read as UTC, so SQLite and Postgres behave the same.

## When not to use it

- **When another process reads the bookkeeping value for correctness.** The database holds it up to `min_interval` old.
- **With a `before_update` hook that stamps a column on every call.** SQLAlchemy calls `before_update` for every dirty object, even one with no net changes, so such a hook turns a discarded change back into an UPDATE, and `stats` still counts it as skipped. Check `object_session(target).is_modified(target)` inside the hook, as SQLAlchemy's documentation advises, or use the column's `onupdate=`, which only fires when an UPDATE is sent.
- **With a clock set by a SQL expression** such as `func.now()`. A SQL expression is always written, so nothing is saved. Set the clock from Python.
- **On columns with triggers that must fire on every change.**
- **For writes made with Core `update()` statements.** Only ORM flushes are seen.

## Tests

```
pip install -e ".[test]"
pytest
python tools/check_oracle_catches_bugs.py
```

The unit tests count the UPDATE statements the database actually receives and read rows back through a separate session, rather than trusting the library's own statistics. They run on SQLite locally, with real SAVEPOINTs. Set `QU_TEST_DB_URL` to run them on PostgreSQL; CI runs them on both.

`tests/test_against_plain_sqlalchemy.py` is the oracle test, and it runs on SQLite only.

- It runs 300 random programs twice: once with the library, and once on plain SQLAlchemy that applies the rule above by hand.
- The programs mix bookkeeping changes, data changes, data and bookkeeping changed together, clock ticks, SAVEPOINTs that are released or rolled back, commits, rollbacks and closed sessions.
- After every step, the ORM objects and the rows each transaction sees must be identical on both sides.
- It does not cover concurrent sessions or processes. With no state kept between flushes, there is nothing in the library for them to race on.
- Every program flushes after every change, so it does not cover autoflush timing. Synonyms, subclasses and the other cases above are unit tests.

`tools/check_oracle_catches_bugs.py` first checks that the oracle passes on the unmodified library. It then plants five bugs in a copy and checks that the oracle catches each one:

- a library that never skips;
- one that ignores the interval;
- the interval-stretch bug fixed in 0.2.0;
- an object left with a discarded value;
- a data change discarded whole because bookkeeping came with it.

CI runs it on every push.

## How it was written

I wrote it with Claude Code as a pair programmer, and every commit carries its co-author line. No one else uses it yet. All releases so far came out on the same day.

The bugs were found by AI code review (a Claude agent asked to break the library), and each was reproduced as a failing test before it was fixed.

Versions 0.2.0 to 0.3.0 remembered skipped values and wrote them with the row's next UPDATE. Each of the four reviews of those versions found a new way for that to lose or overwrite data. Remembering them is a cache that has to stay coherent across sessions, SAVEPOINTs, threads, processes and databases. 0.4.0 removes it, and with it every one of those bugs.

This takes the library back to 0.1.0's behaviour, which the first review called data loss: a discarded value is never written. Now it is the documented cost. `test_a_discarded_change_is_not_written_later` asserts the opposite of 0.2.0's `test_skipped_values_are_written_with_the_next_real_change`, on purpose. See [CHANGELOG.md](CHANGELOG.md). Releases are tagged.

## License

MIT
