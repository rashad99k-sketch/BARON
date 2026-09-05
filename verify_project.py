"""Offline project verification for Windows CI/manual checks."""
from __future__ import annotations
import ast
import py_compile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
errors = []
for path in ROOT.rglob("*.py"):
    if any(x in path.parts for x in (".venv", "__pycache__")):
        continue
    try:
        ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        py_compile.compile(str(path), doraise=True)
    except Exception as exc:
        errors.append(f"{path}: {exc}")
if errors:
    print("PROJECT VERIFY: FAILED")
    print("\n".join(errors))
    raise SystemExit(1)
print("PROJECT VERIFY: PASS — all Python modules parse and compile")
