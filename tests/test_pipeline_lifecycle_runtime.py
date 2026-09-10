"""Pipeline lifecycle runtime tests live in a dedicated subprocess.

The worker (`_worker_pipeline_lifecycle_runtime.py`) is deliberately hermetic:
it re-imports core.engine/core.runtime/scanner/portfolio/news/strategy fresh
behind fake ccxt/flask and replaces the process module table. That world-wide
mutation (plus the event-bus worker threads its fresh engine spins up) is
incompatible with the real-engine book tests that share the suite process,
regardless of how the module table is restored afterwards.

Running it as a subprocess keeps the hermetic guarantees and lets the rest of
the suite stay deterministic.
"""
import os
import subprocess
import sys
import unittest


class PipelineLifecycleRuntimeSubprocessTest(unittest.TestCase):
    def test_worker_passes_in_isolation(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        worker = os.path.join(root, "tests", "_worker_pipeline_lifecycle_runtime.py")
        env = dict(os.environ)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", worker, "-p", "no:cacheprovider", "-q"],
            cwd=root, env=env, capture_output=True, text=True, timeout=900,
        )
        self.assertEqual(
            result.returncode, 0,
            "\n--- worker stdout ---\n%s\n--- worker stderr ---\n%s"
            % (result.stdout[-4000:], result.stderr[-2000:]),
        )


if __name__ == "__main__":
    unittest.main()