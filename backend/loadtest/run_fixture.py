"""Serve the fixture sites. Test tooling only."""
import uvicorn

if __name__ == "__main__":
    uvicorn.run("loadtest.fixture_site:app", host="0.0.0.0", port=80, log_level="warning", limit_concurrency=2000, backlog=2048)
