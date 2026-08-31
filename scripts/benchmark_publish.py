#!/usr/bin/env python3
"""Run the explicit field-shaped publish benchmark and print elapsed wall time."""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    started = time.perf_counter()
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-m",
            "performance",
            "-s",
            "tests/performance/test_publish_operation_counts.py",
        ],
        cwd=root,
        check=False,
    )
    print(f"benchmark wall clock: {time.perf_counter() - started:.3f}s")
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
