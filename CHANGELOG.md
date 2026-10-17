# Changelog

## 0.2.1 (2026-10-17)

Bugs found in review of 0.2.0. Each one has a test that fails on 0.2.0 and passes now.

- **Fixed: evicting skipped values brought back the stretched interval.**
  - The stored clock was kept with the skipped values. Evicting them left the object's own clock, which a skip had
    just overwritten in memory. With `expire_on_commit=False` and more rows than `max_remembered`, rows were never
    written again.
  - The stored clock is now kept per object, in a weak dictionary that eviction never touches.
  - Test: `test_evicted_values_cannot_stretch_the_interval`.
- **Fixed (data loss): a write that was not committed lost the skipped values.**
  - A write took them out of memory at flush time. If its transaction rolled back, or the session closed without a
    commit, they were gone.
  - Memory changes are now journalled per transaction. A commit keeps them; a rollback or close undoes them.
  - Test: `test_a_write_that_is_not_committed_gives_the_skipped_values_back`, for rollback and for close.
- **Fixed: values skipped in a rolled-back transaction were still written later.** Test:
  `test_skips_in_a_rolled_back_transaction_are_forgotten`.
- **Fixed: a clock read inside a rolled-back transaction was still trusted.** A skip right after a write
  remembered the clock that write had stored, and kept using it after the rollback. Test:
  `test_a_clock_read_inside_a_rolled_back_transaction_is_not_trusted`.
- **Fixed: a rolled-back SAVEPOINT lost what its write took.** Now it gives it back and keeps the skips of the
  transaction around it. Test: `test_a_rolled_back_savepoint_gives_back_what_its_write_took`.
- **Fixed: the stored clock was read from the database on every flush of an expired object,** even when it was
  already known. A default argument was evaluated eagerly. Test: `test_the_stored_clock_is_read_once_per_object`.

## 0.2.0 (2026-10-17)

Bugs found in review of 0.1.0. Each one has a test that fails on 0.1.0 and passes now.

- **Fixed (data loss): skipped values were never written.**
  - After a skip, a later UPDATE that changed only a data column wrote that column alone. The skipped bookkeeping
    values were thrown away, and the database kept the old ones for good.
  - Remembered values are now carried into the row's next UPDATE.
  - Test: `test_skipped_values_are_written_with_the_next_real_change`.
- **Fixed: `min_interval` could be stretched forever.**
  - The freshness check read the clock from the object, and a skip had just overwritten it in memory. With
    `expire_on_commit=False`, or repeated flushes, a row stamped every minute was never written again.
  - The check now uses the clock value the database really holds. It is remembered at the first skip, or read
    straight from the database when the object was expired.
  - Test: `test_repeated_skips_cannot_stretch_past_the_interval`.
- **Fixed: a naive `now()` raised TypeError.** It is now read as UTC, like stored values. Test:
  `test_a_naive_clock_is_read_as_utc`.
- **Fixed: the memory of skipped values grew without limit.** It is now an LRU capped by `max_remembered` (default
  100,000), with a lock for threaded use. Test: `test_memory_is_bounded`.
- **Added:**
  - `test_expired_objects_compare_against_the_stored_clock`;
  - `test_skip_then_real_change_in_the_same_session_writes_the_newest_values`, which also checks that setting
    attributes never triggers an extra flush.
- **CI:** the suite now also runs against PostgreSQL 16, not only SQLite.
- **Docs:** Core `update()` statements are not seen by the hook.

## 0.1.0 (2026-10-17)

First release.
