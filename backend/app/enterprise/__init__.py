"""The Enterprise privacy scanner: a separate product that sits beside the 5-page consent scanner.

Isolation rules (enforced by tests/enterprise/test_enterprise_isolation.py):
  * nothing outside this package imports it, except the registration points added in a later phase (api/v1/router.py, jobs/worker.py);
  * nothing in this package imports app.scoring (the existing score) or app.api, app.services or app.jobs;
  * it may import small, stable, read-only helpers (scanner.url_normalize, scanner.url_safety, ...) and the database Base, and it never changes them.

Phase 1 (this commit's scope): settings, tables, the resumable crawl frontier and the evidence store. No endpoint, no job, no scoring yet.
"""
