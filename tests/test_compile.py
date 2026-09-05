import py_compile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class CompileTest(unittest.TestCase):
    def test_modules_compile(self):
        for path in ROOT.rglob("*.py"):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            py_compile.compile(str(path), doraise=True)


if __name__ == "__main__":
    unittest.main()
