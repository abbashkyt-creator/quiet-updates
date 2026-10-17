# quiet-updates

Skip SQLAlchemy UPDATEs that change nothing but bookkeeping.

Many tables carry columns that move every time you look at the outside world: a "last checked at" timestamp, a countdown, a poll counter. Writing them on every pass creates a new row version every time, with WAL, index maintenance and lock traffic, even when the data the row exists for did not change.

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
    s.commit()                            # no UPDATE: only bookkeeping changed, row is fresh

    quiet.value(item, "countdown")        # 540, the newest value, even after expiry
    quiet.stats                           # QuietStats(skipped=1, written=0)
# The next real change to this row also writes countdown=540 and the new checked_at.
```

## The rules

For a watched model, at flush time:

| What changed | Stored `clock` age | Result |
|---|---|---|
| Only bookkeeping columns | below `min_interval` | Not written. The new values are remembered. |
| Only bookkeeping columns | at or above `min_interval`, or never set | Written, with any remembered values |
| Any other column | any | Written, with any remembered values |

"Stored `clock` age" means the value the database holds, not the one on the object. A skip never moves it, so a row stamped every minute is still written once per `min_interval`.

Remembered values are written with a guard: each one is set only where the stored `clock` is still older than the skip (`CASE WHEN clock < :skipped THEN :value ELSE column END`). If another process has written the row since, its newer values stay.

Unwatched models are untouched. Naive datetimes are read as UTC, so SQLite and Postgres behave the same.

## Transactions

Remembered values follow the transaction, and each SAVEPOINT on its own:

- A write takes them with it. If its transaction or SAVEPOINT rolls back, or the session closes without a commit, they come back.
- Values skipped inside a transaction or SAVEPOINT that rolls back are forgotten.
- Rolling back a SAVEPOINT undoes only what happened inside it.

When `max_remembered` is reached, the oldest row's skipped values are dropped and never written. The interval still holds: the stored clock is kept per object, apart from the skipped values.

## When not to use it

- **When another process reads the bookkeeping value for correctness.** The database keeps the older value for up to `min_interval`.
- **When several processes need the newest skipped value.** Remembered values live in the memory of the process that skipped them. Writes from several processes are safe (see the guard above), but each one only knows its own skips.
- **With a `before_update` hook that stamps a column on every call.** SQLAlchemy calls `before_update` for every dirty object, even one with no net changes, so such a hook turns a skip back into an UPDATE, and `stats` still counts it as skipped. Check `object_session(target).is_modified(target)` inside the hook, as SQLAlchemy's documentation advises, or use the column's `onupdate=`, which only fires when an UPDATE is sent.
- **On columns with triggers that must fire on every change.**
- **For writes made with Core `update()` statements.** Only ORM flushes are seen.

## Tests

```
pip install -e ".[test]"
pytest
```

The tests count the UPDATE statements the database actually receives and read rows back through a separate session, rather than trusting the library's own statistics. They run on SQLite locally. Set `QU_TEST_DB_URL` to run them on PostgreSQL; CI runs both.

See [CHANGELOG.md](CHANGELOG.md) for the bugs fixed in each release. Each fix comes with a test that fails on the release before it. Releases are tagged, so you can run the tests against an older one (`git show v0.2.1:src/quiet_updates/__init__.py`).

## How it was written

I wrote it with Claude Code as a pair programmer, and every commit carries its co-author line. All releases so far came out on the same day. The bugs they fix were found by AI code review (a Claude agent asked to break the library) and each was reproduced as a failing test before it was fixed. No one else uses it yet.

## License

MIT
