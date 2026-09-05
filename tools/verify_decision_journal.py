#!/usr/bin/env python3
"""Verify the persistent decision journal hash chain."""
import argparse
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core.decision_journal import verify_file

p = argparse.ArgumentParser()
p.add_argument("path", nargs="?", default=None)
args = p.parse_args()
ok, count, message = verify_file(args.path)
print(f"DECISION_JOURNAL valid={ok} records={count} status={message}")
raise SystemExit(0 if ok else 1)
