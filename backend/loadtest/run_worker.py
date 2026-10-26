"""The real worker, with names resolved to the fixture (see patch.py). Test tooling only."""
import asyncio

from loadtest import patch

patch.apply()

from app.jobs import worker  # (after the patch, so everything it imports sees it)

if __name__ == "__main__":
    asyncio.run(worker.run_forever())
