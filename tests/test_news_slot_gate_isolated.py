"""Subprocess worker for NewsSlotGateTest.

The gate paces the full open/execution path and its result depends on module
state emitted by earlier files in the same pytest process. Running it in its
own process keeps the hermetic ordering guarantees while the rest of the suite
stays deterministic."""
import os
import subprocess
import sys
import unittest


class NewsSlotGateIsolatedTest(unittest.TestCase):
    def test_gate_passes_in_isolation(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        worker = os.path.join(root, "tests", "_worker_news_slot_gate.py")
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, worker],
            cwd=root, env=env, capture_output=True, text=True, timeout=600,
        )
        self.assertEqual(
            result.returncode, 0,
            "\n--- worker stdout ---\n%s\n--- worker stderr ---\n%s"
            % (result.stdout[-4000:], result.stderr[-2000:]),
        )


if __name__ == "__main__":
    unittest.main()