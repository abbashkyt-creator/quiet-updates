# Changelog

## 0.4.1 (2026-10-17)

Bugs found in review of 0.4.0. Each one has a test that fails on 0.4.0 and passes now.

- **Fixed: a `synonym()` turned the library off.** The synonym's own key looked like a changed column that is not
  bookkeeping, so every change was written. Test: `test_a_synonym_does_not_turn_the_library_off`.
- **Fixed: a stored clock ahead of `now()` stopped all writes** until `now()` caught up. Local time read as UTC
  was 3 hours ahead. It is now trusted for one interval only. Test:
  `test_a_stored_clock_ahead_of_now_is_trusted_for_one_interval_only`.
- **Fixed: `session.flush(objects=[...])` discarded changes on objects outside the flush.** Test:
  `test_flushing_other_objects_leaves_this_ones_changes_pending`.
- **Changed: expired clocks are read in one query per model,** not one per row. Test:
  `test_expired_clocks_are_read_in_one_query_per_model`.
- **Oracle test:**
  - it now also changes data and bookkeeping in one flush, which must be written whole;
  - the review showed a planted bug that dropped such a data change passed it, and that bug is now the tool's fifth
    mutant;
  - `tools/check_oracle_catches_bugs.py` first checks that the oracle passes on the unmodified library, and CI runs
    it.
- **Docs:** "stored clock" means SQLAlchemy's last loaded or flushed value; each flush, autoflush included, is
  judged on its own; a clock set by a SQL expression saves nothing; 0.4.0 deliberately reverses 0.2.0's first fix.

## 0.4.0 (2026-10-17)

Remembering skipped values is removed. Every review so far found a bug that lost or overwrote data. Since 0.2.0
every one of them was in that feature, and four reviews of it in a row found new ones. It is a cache that has to stay coherent across sessions, SAVEPOINTs, threads,
processes and databases.

- **Changed (breaking): a skipped change is discarded.** No UPDATE is sent, and the object goes back to the values the
  database holds. Nothing is carried into a later UPDATE. The library keeps no state between flushes.
- **Removed:** `QuietUpdates.value()` and `max_remembered`.
- **Fixed by the removal.** The round-5 review reproduced these in 0.3.0:
  - `Session.merge(load=False)` raised AttributeError for any watched model. Test:
    `test_merge_without_load_works_with_and_without_the_library` (fails on 0.3.0).
  - With `load_only`, the clock was never read and the interval stretched forever. Test:
    `test_a_clock_that_was_not_loaded_still_keeps_the_interval` (fails on 0.3.0).
  - The guard against another process's newer write could be lost, through a cut or a thread race. A clock-only write
    by anyone else made every remembered value be dropped.
  - Memory was keyed by model and primary key, so two databases shared it.
  - `uninstall()` left the model listeners behind.
- **Changed: the oracle test compares against an executable model.** Plain SQLAlchemy applies the rule by hand. After
  every step the ORM objects and the rows each transaction sees must match exactly, so too much, too little or a
  stretched skip fails at once. The programs keep their objects alive, as applications do.
- **Added:** `tools/check_oracle_catches_bugs.py`. It plants four bugs and checks that the oracle catches each one.

## 0.3.0 (2026-10-17)

A new design. Each review of 0.2.0, 0.2.1 and 0.2.2 found data loss in its rollback handling, so this release
replaces the patched undo journal instead of patching it again.

- **Changed: skips are a redo log.**
  - A skip stays with its session, in the SAVEPOINT that made it, until the outermost transaction commits. Only then
    do other sessions see it.
  - A released SAVEPOINT hands what it did to the scope around it. A rollback drops its scope.
  - A write records a cut: it consumes every remembered value of its row below its scope, and names the committed
    ones, so newer values committed meanwhile by other sessions survive.
- **Added: loaded and refreshed objects show their remembered values,** so the ORM and `quiet.value()` agree. A
  session the library is not installed on still sees the database.
- **Added: an oracle test** (`tests/test_against_plain_sqlalchemy.py`). It runs 300 random programs on plain
  SQLAlchemy and with the library. After every step both sides must see the same values, and at the end both
  databases must be identical.
- **Tests:** SQLite now runs with real SAVEPOINTs (SQLAlchemy's pysqlite workaround) and in WAL mode, so the
  rollback tests mean what they say.

Fixed. Each fix has a test that fails on 0.2.2:

- **B1 (data loss): releasing a SAVEPOINT wiped the rollback journal.** SQLAlchemy fires `after_commit` on a release.
  Test: `test_releasing_a_savepoint_keeps_what_a_rollback_must_give_back`.
- **B2: a skip in a released SAVEPOINT survived its parent's rollback.** Test:
  `test_a_skip_in_a_released_savepoint_rolls_back_with_its_parent`.
- **B3 (data loss): a skip that did not move the clock had no guard,** and overwrote another process's newer write.
  It now lands only where the stored clock is unchanged since the skip. Test:
  `test_a_skip_that_did_not_move_the_clock_never_overwrites_a_newer_write`.
- **B4: another session's uncommitted skip could be written.** Test:
  `test_another_sessions_uncommitted_skip_is_not_written`.
- **B5: a skipped SQL expression poisoned the row.** Every later write failed. A SQL expression is now always
  written. Test: `test_a_sql_expression_is_written_not_remembered`.
- **B6: skipped increments in sequential sessions were stored as one.** Test:
  `test_skipped_increments_in_one_process_add_up`.

Found by the oracle test while building this release, never in a release:

- A re-stamp with the same time as the stored clock was refused by the guard, which is now `<=`.
- A session's own skip shadowed a committed one, and a write consumed only the visible value, so the committed one
  came back. This is what the cut fixes.
- A reloaded object showed the stored value, so setting a column back to it was no change to SQLAlchemy, and the
  remembered value was written instead. Test: `test_setting_a_column_back_to_its_stored_value_after_a_reload_counts`
  (it fails on 0.2.2).

## 0.2.2 (2026-10-17)

Bugs found in review of 0.2.1. Each one has a test that fails on 0.2.1 and passes now.

- **Fixed (data loss): a remembered value could overwrite a newer write from another process.**
  - Worker A skipped at T+1 with countdown 540. Worker B then wrote T+6 with countdown 240. A's next UPDATE, a price
    change, carried T+1/540 with it: the stored clock went backwards and B's value was lost.
  - Remembered values are now written as `CASE WHEN clock < :skipped THEN :value ELSE column END`, so they only land
    where the stored clock is still older than the skip.
  - Test: `test_a_remembered_value_never_overwrites_a_newer_write_from_another_process`.
- **Fixed: a write that did not store the clock still forgot the stored clock.**
  - After an eviction, the object kept the skipped clock that a skip had put on it, so the row looked fresh for good.
  - Now only an UPDATE that stores the clock resets it.
  - Test: `test_a_write_without_the_clock_keeps_the_stored_clock`.
- **Fixed: a SAVEPOINT rollback undid the whole session's journal.**
  - Rolling back an empty SAVEPOINT gave back values that an earlier write had already stored, and the next UPDATE
    sent them again.
  - A skip made inside a rolled-back SAVEPOINT survived it.
  - The journal is now kept per SAVEPOINT; a rollback undoes only its own scope and the SAVEPOINTs inside it.
  - Tests: `test_a_rolled_back_empty_savepoint_gives_nothing_back`,
    `test_a_skip_inside_a_rolled_back_savepoint_is_forgotten`.
- **Docs:** a `before_update` hook that stamps a column on every call turns a skip back into an UPDATE, and `stats`
  still counts it as skipped. The README says how to avoid it.

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
