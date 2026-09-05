"""Real dashboard runtime verifier.

Run on a machine with project dependencies installed. It starts the actual Flask
application on a loopback port, probes the HTML and JSON API contract, then
shuts the process down. No live order is sent.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from urllib.request import Request, urlopen
from urllib.error import URLError

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.getenv("DASHBOARD_TEST_PORT", "18000"))
BASE = f"http://127.0.0.1:{PORT}"
ROUTES = ["/", "/health", "/status", "/data", "/scanner", "/watchlist", "/execution",
          "/positions", "/portfolio", "/news", "/radar", "/metrics"]


def get(path):
    req = Request(BASE + path, headers={"User-Agent": "RF-Liquidity-Pro-Dashboard-Test/1.0"})
    with urlopen(req, timeout=5) as r:
        body = r.read().decode("utf-8", errors="replace")
        return r.status, r.headers.get("content-type", ""), body


def main():
    missing = []
    for mod in ("flask", "ccxt", "pandas", "numpy", "requests", "dotenv"):
        try:
            __import__(mod)
        except Exception:
            missing.append(mod)
    if missing:
        print("DASHBOARD_RUNTIME_BLOCKED: missing dependencies: " + ", ".join(missing))
        return 2

    env = os.environ.copy()
    env.update({"PAPER_MODE": "True", "PORT": str(PORT), "NEWS_ENABLED": "False"})
    proc = subprocess.Popen([sys.executable, "main.py"], cwd=ROOT, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 25
        last_error = None
        while time.time() < deadline:
            try:
                status, ctype, body = get("/health")
                if status == 200:
                    break
            except Exception as exc:
                last_error = exc
            if proc.poll() is not None:
                raise RuntimeError(f"application exited early: {proc.returncode}")
            time.sleep(0.5)
        else:
            raise RuntimeError(f"dashboard did not start: {last_error}")

        for route in ROUTES:
            status, ctype, body = get(route)
            if status != 200:
                raise AssertionError(f"{route}: HTTP {status}")
            if route != "/":
                json.loads(body)
            if route == "/" and "RF Liquidity" not in body and "RF LIQUIDITY" not in body:
                raise AssertionError("dashboard HTML marker missing")
        print("DASHBOARD_RUNTIME=PASS")
        print("ROUTES_CHECKED=" + str(len(ROUTES)))
        return 0
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
