"""Run the unittest suite and close the shared async SQLite pool before exit."""
from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    suite = unittest.defaultTestLoader.discover(
        start_dir=str(PROJECT_ROOT / "tests"),
        pattern="test_*.py",
    )
    result = unittest.TextTestRunner(
        verbosity=0 if args.quiet else 2 if args.verbose else 1,
    ).run(suite)

    # Individual tests use database.get_db() as a shared aiosqlite pool.
    # Close it once the suite is done so its non-daemon worker cannot keep the
    # CI process alive after unittest has printed its result.
    try:
        import database

        asyncio.run(database.close_db())
    except Exception as exc:
        print(f"Could not close the test database pool: {exc}", file=sys.stderr)
        return 1

    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
