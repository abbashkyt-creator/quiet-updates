"""Plant bugs in a copy of quiet_updates and check that the oracle test catches each one.

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
    "never skips": ("            if quiet_kind and self._fresh(", "            if False and self._fresh("),
    "always skips (no interval)": ("        return _aware(self.now()) - _aware(stored) < rule.min_interval",
                                   "        return True"),
    "0.1.0 stretch: clock read from the object": (
        "        history = state.attrs[clock].history\n        if history.deleted:\n            return history.deleted[0]",
        "        history = state.attrs[clock].history\n        if history.added:\n            return history.added[0]\n        if history.deleted:\n            return history.deleted[0]"),
    "object keeps the discarded value": ("                set_committed_value(obj, column, history.deleted[0])",
                                         "                set_committed_value(obj, column, history.added[0])"),
}

missed = 0
for name, (old, new) in MUTANTS.items():
    assert SOURCE.count(old) == 1, name
    tmp = pathlib.Path(tempfile.mkdtemp())
    (tmp / "quiet_updates").mkdir()
    (tmp / "quiet_updates/__init__.py").write_text(SOURCE.replace(old, new), encoding="utf-8")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-x", "-o", "pythonpath=", "-p", "no:cacheprovider",
         str(REPO / "tests/test_against_plain_sqlalchemy.py")],
        env={**os.environ, "PYTHONPATH": str(tmp), "QU_ORACLE_PROGRAMS": "50"},
        capture_output=True, text=True, cwd=str(tmp),
    )
    caught = "1 failed" in result.stdout
    print(f"{'caught' if caught else 'MISSED'}  {name}")
    missed += not caught
    shutil.rmtree(tmp, ignore_errors=True)
sys.exit(1 if missed else 0)
