"""Dev convenience launcher: starts the API server and the background worker together,
so you don't need two terminal tabs.

This does NOT change how either one runs -- it just spawns them as two separate child
processes, exactly as `python -m app.run_server` and `python -m app.jobs.worker` would
from two terminals (worker.py's own docstring: "Run as a separate process from the
API"). The worker already idles/polls the job queue on its own and only picks up work
once a job is queued -- that part needed no code change, just one command to start both.

Usage:
    python -m app.run_dev [--host 127.0.0.1] [--port 8000]

Ctrl+C stops both processes together.
"""

import argparse
import signal
import subprocess
import sys
import threading
import time


def _stream(prefix: str, pipe) -> None:
    for line in iter(pipe.readline, ""):
        if not line:
            break
        print(f"[{prefix}] {line}", end="")
    pipe.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the API server and worker together (dev convenience).")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    api_cmd = [sys.executable, "-m", "app.run_server", "--host", args.host, "--port", str(args.port)]
    worker_cmd = [sys.executable, "-m", "app.jobs.worker"]

    api_proc = subprocess.Popen(api_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    worker_proc = subprocess.Popen(worker_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)

    threads = [
        threading.Thread(target=_stream, args=("api", api_proc.stdout), daemon=True),
        threading.Thread(target=_stream, args=("worker", worker_proc.stdout), daemon=True),
    ]
    for t in threads:
        t.start()

    # Ctrl+C in an interactive terminal already reaches both children directly on
    # Windows (they share the console), but a non-interactive stop (e.g. a process
    # manager sending SIGTERM) only signals this process -- without this handler that
    # would leave api_proc/worker_proc running as orphans, still holding DB connections
    # against the pooler's tight 15-client budget. Raising KeyboardInterrupt here routes
    # both stop paths through the same cleanup in `finally` below.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _on_sigterm)

    try:
        while True:
            if api_proc.poll() is not None or worker_proc.poll() is not None:
                break
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping api + worker...")
    finally:
        for p in (api_proc, worker_proc):
            if p.poll() is None:
                p.terminate()
        for p in (api_proc, worker_proc):
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


if __name__ == "__main__":
    main()
