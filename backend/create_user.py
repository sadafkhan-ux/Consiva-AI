"""Bootstrap the first organization and admin user.

Needed because /api/v1/auth/users requires an authenticated admin, and there is
no admin until one exists -- a chicken-and-egg that has to be broken outside the
API. Run this once per deployment; every user after that is created through the
API by an admin.

Usage:
    python create_user.py --email you@example.com --org "Swaransoft" --admin
    python create_user.py --email member@example.com --org-id <uuid>

The password is read interactively (never passed as an argument, which would
leave it in shell history and in the process list). Use --password only for
automated provisioning where you accept that exposure.
"""

import argparse
import asyncio
import getpass
import sys
import uuid

from sqlalchemy import select

from app.core import passwords
from app.db.models import Organization
from app.db.repositories import user_repository
from app.db.session import async_session_factory, engine


async def run(args: argparse.Namespace) -> int:
    password = args.password or getpass.getpass("Password: ")
    if not args.password:
        if password != getpass.getpass("Confirm password: "):
            print("ERROR: passwords do not match.", file=sys.stderr)
            return 1
    try:
        passwords.validate_password(password)
    except passwords.PasswordError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    async with async_session_factory() as db:
        if await user_repository.get_user_by_email(db, args.email) is not None:
            print(f"ERROR: a user with email {args.email!r} already exists.", file=sys.stderr)
            return 1

        if args.org_id:
            org = await user_repository.get_organization(db, uuid.UUID(args.org_id))
            if org is None:
                print(f"ERROR: organization {args.org_id} not found.", file=sys.stderr)
                return 1
        elif args.org:
            existing = await db.execute(select(Organization).where(Organization.name == args.org))
            org = existing.scalar_one_or_none()
            if org is None:
                org = await user_repository.create_organization(db, name=args.org)
                print(f"created organization {org.name!r} -> {org.id}")
            else:
                print(f"using existing organization {org.name!r} -> {org.id}")
        else:
            print("ERROR: pass --org NAME to create/reuse an org, or --org-id UUID.", file=sys.stderr)
            return 1

        user = await user_repository.create_user(
            db,
            org_id=org.id,
            email=args.email,
            password_hash=passwords.hash_password(password),
            full_name=args.full_name,
            role="admin" if args.admin else "member",
        )
        await db.commit()

    print(f"created {user.role} {user.email} -> {user.id}")
    print(f"org_id: {org.id}")
    print("\nLog in with:")
    print("  curl -X POST http://127.0.0.1:8000/api/v1/auth/login \\")
    print('    -H "Content-Type: application/json" \\')
    print(f'    -d \'{{"email":"{user.email}","password":"<password>"}}\'')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--email", required=True)
    parser.add_argument("--org", help="organization NAME to create or reuse")
    parser.add_argument("--org-id", help="existing organization UUID to attach this user to")
    parser.add_argument("--full-name", default=None)
    parser.add_argument("--admin", action="store_true", help="grant the admin role (can create other users)")
    parser.add_argument(
        "--password", default=None,
        help="NOT RECOMMENDED: exposes the password in shell history and the process list",
    )
    args = parser.parse_args()

    try:
        return asyncio.run(run(args))
    finally:
        asyncio.run(engine.dispose())


if __name__ == "__main__":
    raise SystemExit(main())
