"""The real API, with names resolved to the fixture (see patch.py). Test tooling only."""
import os

import uvicorn

from loadtest import patch

patch.apply()

if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
