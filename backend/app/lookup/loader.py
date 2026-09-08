"""One-time import of the Open Cookie Database CSV into `cookie_lookup`
(Apache 2.0, commercial use permitted — https://github.com/jkwakman/Open-Cookie-Database).

Run standalone once the CSV is placed at the given path:

    python -m app.lookup.loader backend/data/open-cookie-database.csv

The source dataset uses 5 categories (Functional, Personalization, Analytics,
Marketing, Security); this project's taxonomy only has 4 (see purpose_taxonomy).
The mapping below is an explicit, visible design decision, not silent logic —
Security cookies are treated as functional/strictly-necessary (standard practice
across CMPs); Personalization is treated as marketing, since DPDP-relevant consent
risk for profiling/personalization tracking is closer to marketing than to
operational necessity.
"""

import argparse
import asyncio
import csv
from pathlib import Path

from app.db.models import CookieLookup
from app.db.session import async_session_factory

CATEGORY_MAP = {
    "functional": "functional",
    "security": "functional",
    "analytics": "analytics",
    "marketing": "marketing",
    "personalization": "marketing",
}


def _map_category(raw: str) -> str:
    return CATEGORY_MAP.get(raw.strip().lower(), "other")


async def load_csv(path: Path) -> int:
    rows = []
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for record in reader:
            name = (record.get("Cookie / Data Key name") or "").strip()
            if not name:
                continue
            rows.append(CookieLookup(
                name_pattern=name,
                is_prefix_pattern=record.get("Wildcard match", "0").strip() == "1",
                domain_pattern=(record.get("Domain") or "").strip() or None,
                vendor=(record.get("Platform") or "").strip() or None,
                category=_map_category(record.get("Category") or ""),
                source="open_cookie_database",
                raw_metadata={
                    "original_category": record.get("Category"),
                    "description": record.get("Description"),
                    "retention_period": record.get("Retention period"),
                    "data_controller": record.get("Data Controller"),
                    "rights_portal": record.get("User Privacy & GDPR Rights Portals"),
                    "source_id": record.get("ID"),
                },
            ))

    async with async_session_factory() as db:
        db.add_all(rows)
        await db.commit()
    return len(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="Load the Open Cookie Database CSV into cookie_lookup")
    parser.add_argument("path", type=Path)
    args = parser.parse_args()

    count = asyncio.run(load_csv(args.path))
    print(f"Loaded {count} cookie_lookup rows from {args.path}")


if __name__ == "__main__":
    main()
