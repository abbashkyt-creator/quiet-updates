"""Plant bugs in a copy of quiet_updates and check that the oracle test catches each one.

It first checks that the oracle passes on the unmodified library: a broken
oracle would otherwise "catch" every mutant.

Run from the repository root: python tools/check_oracle_catches_bugs.py
"""
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parent.parent
SOURCE = (REPO / "src/quiet_updates/__init__.py").read_text(encoding="utf-8")

MUTANTS = {
    "never skips": (
        "            if self._fresh(stored[id(state)], rule):",
        "            if False and self._fresh(stored[id(state)], rule):",
    ),
    "always skips (no interval)": (
        "        return abs(_aware(self.now()) - _aware(stored)) < rule.min_interval",
        "        return True",
    ),
    "0.1.0 stretch: clock read from the object": (
        "            if history.deleted:\n                found[id(state)] = history.deleted[0]",
        "            if history.added:\n                found[id(state)] = history.added[0]\n"
        "            elif history.deleted:\n                found[id(state)] = history.deleted[0]",
    ),
    "object keeps the discarded value": (
        "                set_committed_value(obj, column, history.deleted[0])",
        "                set_committed_value(obj, column, history.added[0])",
    ),
    "a data change with bookkeeping is discarded whole": (
        "            if set(changed) <= rule.columns and not any(",
        "            if set(changed) & rule.columns and not any(",
    ),
}


def run_oracle(source: str) -> bool:
    """Whether the oracle passes on this version of the library."""
    tmp = pathlib.Path(tempfile.mkdtemp())
    try:
        (tmp / "quiet_updates").mkdir()
        (tmp / "quiet_updates/__init__.py").write_text(source, encoding="utf-8")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "-x", "-o", "pythonpath=", "-p", "no:cacheprovider",
             str(REPO / "tests/test_against_plain_sqlalchemy.py")],
            env={**os.environ, "PYTHONPATH": str(tmp), "QU_ORACLE_PROGRAMS": "50"},
            capture_output=True, text=True, cwd=str(tmp),
        )
        return "1 passed" in result.stdout
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if not run_oracle(SOURCE):
    print("the oracle fails on the unmodified library")
    sys.exit(2)
print("passes   the unmodified library")

missed = 0
for name, (old, new) in MUTANTS.items():
    assert SOURCE.count(old) == 1, f"mutant {name!r} no longer matches the source"
    caught = not run_oracle(SOURCE.replace(old, new))
    print(f"{'caught' if caught else 'MISSED'}   {name}")
    missed += not caught
sys.exit(1 if missed else 0)
