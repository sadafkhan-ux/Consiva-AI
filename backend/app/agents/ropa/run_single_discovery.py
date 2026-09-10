"""Standalone entrypoint for running exactly one ROPA discovery, so Agent 2 can
be tested on its own before it is wired into Agent 1's pipeline.

The website URL is the IDENTIFIER for the run, not the thing being read: it is
normalized to a domain with urlparse().netloc -- the same way services/
scan_service.py derives `websites.domain` for Agent 1 -- so that once the two
agents are connected, the domain is the join key between a consent scan and a
data-source discovery for the same customer.

The data actually comes from that customer's database, whose credentials are
supplied by flag, by ROPA_SOURCE_* environment variable, or by ROPA_SOURCE_*
entries in backend/.env (see SourceSettings below).

Usage:
    python -m app.agents.ropa.run_single_discovery <website_url> [options]

Prints the DiscoveryEvidence as JSON to stdout on success, and a short
human-readable summary to stderr. On failure, prints the error to stderr and
exits non-zero.
"""

import argparse
import asyncio
import sys
from urllib.parse import urlparse

from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agents.ropa.connectors.base import ConnectorError
from app.agents.ropa.connectors.postgres import (
    PostgresConnectionConfig,
    PostgresConnector,
    PostgresConnectorError,
)
from app.agents.ropa.services import discovery_service


class SourceSettings(BaseSettings):
    """Defaults for the discovery target, read from backend/.env the same way
    app/config.py reads its own settings. Plain os.getenv() would NOT see these
    -- nothing loads .env into the process environment; pydantic-settings reads
    the file itself. Every field stays optional so the flags can supply them
    instead, and so importing this never fails on an .env without them."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    ropa_org_id: str = "test-org"
    ropa_source_host: str | None = None
    ropa_source_port: int = 5432
    ropa_source_dbname: str | None = None
    ropa_source_user: str | None = None
    ropa_source_password: str | None = None
    ropa_source_sslmode: str = "require"

# Table/column names and sampled patterns can carry non-ASCII characters, and on
# Windows sys.stdout otherwise encodes with the console's ANSI codepage (cp1252)
# and raises UnicodeEncodeError. No-op where stdout is already UTF-8.
sys.stdout.reconfigure(encoding="utf-8")
sys.stderr.reconfigure(encoding="utf-8")


def _website_domain(website_url: str) -> str:
    """Normalize the supplied URL to a bare domain, matching how Agent 1 stores
    `websites.domain`. A bare domain typed without a scheme parses with an empty
    netloc, so fall back to the path in that case."""
    parsed = urlparse(website_url if "//" in website_url else f"https://{website_url}")
    domain = parsed.netloc or parsed.path
    if not domain:
        raise ValueError(f"could not derive a domain from {website_url!r}")
    return domain


def _build_config(args: argparse.Namespace) -> PostgresConnectionConfig:
    missing = [
        name
        for name, value in (
            ("--host", args.host),
            ("--dbname", args.dbname),
            ("--user", args.user),
            ("--password", args.password),
        )
        if not value
    ]
    if missing:
        raise ValueError(
            f"missing database credential(s): {', '.join(missing)} "
            "(pass the flag or set the matching ROPA_SOURCE_* environment variable)"
        )

    return PostgresConnectionConfig(
        host=args.host,
        port=args.port,
        dbname=args.dbname,
        user=args.user,
        password=args.password,
        sslmode=args.sslmode,
        schemas=tuple(args.schema) if args.schema else None,
        collect_sample_patterns=args.samples,
        allow_superuser=args.allow_superuser,
    )


async def _main(args: argparse.Namespace) -> None:
    domain = _website_domain(args.website_url)
    config = _build_config(args)
    connector = PostgresConnector(config)

    if args.evidence_only:
        evidence = await connector.discover(org_id=args.org_id, source_name=domain)
        sys.stdout.write(evidence.model_dump_json())
        print(
            f"\ndiscovered {len(evidence.tables)} tables / {len(evidence.columns)} columns "
            f"/ {len(evidence.relationships)} relationships for {domain!r}",
            file=sys.stderr,
        )
        return

    output = await discovery_service.discover_and_analyze(
        connector, org_id=args.org_id, source_name=domain, review_threshold=args.review_threshold
    )

    sys.stdout.write(output.model_dump_json())
    summary = output.discovery_summary
    print(
        f"\n{domain}: {summary.tables_scanned} tables / {summary.columns_scanned} columns scanned"
        f"\n  personal-data elements : {summary.personal_data_elements_found}"
        f"\n  processing activities  : {len(output.processing_activities)}"
        f"\n  ROPA records           : {len(output.ropa_records)}"
        f"\n  risk/gap findings      : {len(output.risk_and_gap_findings)}"
        f"\n  human review items     : {len(output.human_review_items)}"
        f"\n  overall confidence     : {output.confidence_summary.overall_confidence}",
        file=sys.stderr,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.agents.ropa.run_single_discovery",
        description="Run one ROPA discovery against a customer's database, keyed by their website URL.",
    )
    settings = SourceSettings()
    parser.add_argument("website_url", help="the customer's website, used as the identifier for this run")
    parser.add_argument("--org-id", default=settings.ropa_org_id)
    parser.add_argument("--host", default=settings.ropa_source_host)
    parser.add_argument("--port", type=int, default=settings.ropa_source_port)
    parser.add_argument("--dbname", default=settings.ropa_source_dbname)
    parser.add_argument("--user", default=settings.ropa_source_user)
    parser.add_argument("--password", default=settings.ropa_source_password)
    parser.add_argument("--sslmode", default=settings.ropa_source_sslmode)
    parser.add_argument(
        "--schema",
        action="append",
        help="limit discovery to this schema (repeatable); default is every permitted non-system schema",
    )
    parser.add_argument(
        "--samples",
        action="store_true",
        help="collect redacted shape-only sample patterns (never raw values)",
    )
    parser.add_argument(
        "--allow-superuser",
        action="store_true",
        help="bypass the least-privilege check -- local testing only, never for customer data",
    )
    parser.add_argument(
        "--evidence-only",
        action="store_true",
        help="stop after discovery and emit raw evidence, skipping the ROPA analysis pipeline",
    )
    parser.add_argument(
        "--review-threshold",
        type=float,
        default=0.7,
        help="classifications below this confidence are routed to human review (default 0.7)",
    )
    return parser


if __name__ == "__main__":
    parsed_args = _parser().parse_args()
    try:
        asyncio.run(_main(parsed_args))
    except (ConnectorError, PostgresConnectorError, ValueError) as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
