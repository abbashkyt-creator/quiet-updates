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
```

## The rules

For a watched model, at flush time:

| What changed | Stored `clock` age | Result |
|---|---|---|
| Only bookkeeping columns | below `min_interval` | Not written. The new values are remembered. |
| Only bookkeeping columns | at or above `min_interval`, or never set | Written |
| Any other column | any | Written, including the bookkeeping columns |

Unwatched models are untouched. Naive datetimes are read as UTC, so SQLite and Postgres behave the same.

## When not to use it

- **When another process reads the bookkeeping value for correctness.** The database keeps the older value for up to `min_interval`.
- **When several processes need the newest skipped value.** Remembered values live in the process memory of the process that skipped them.
- **On columns with triggers that must fire on every change.**

## Tests

```
pip install -e ".[test]"
pytest
```

The tests use SQLite in memory. They count the actual UPDATE statements sent to the database, rather than trusting the library's own statistics.

## License

MIT
