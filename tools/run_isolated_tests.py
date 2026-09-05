"""Release-gate test runner: one pytest process per test module.

Fresh processes make the legacy suite deterministic because several tests
reload/stub core.engine and intentionally mutate module-level state.
"""
from __future__ import annotations
import os, signal, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = sorted((ROOT / "tests").glob("test_*.py"))
failed = []
for path in TESTS:
    print(f"\n=== {path.name} ===", flush=True)
    cmd = [sys.executable, "-m", "pytest", "-q", str(path), "--tb=short"]
    try:
        result = subprocess.run(cmd, cwd=ROOT, timeout=120, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        print(f"TIMEOUT: {path.name}")
        failed.append(path.name)
        continue
    if result.returncode != 0:
        failed.append(path.name)
print(f"\nISOLATED TEST FILES: {len(TESTS)-len(failed)}/{len(TESTS)} passed")
if failed:
    print("FAILED FILES:")
    for name in failed:
        print(f" - {name}")
    raise SystemExit(1)
print("ALL ISOLATED TEST FILES PASSED")
