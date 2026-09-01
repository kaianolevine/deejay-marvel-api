"""Seed or update one principal in the identity store.

Principals key on ``(issuer, subject)``, and the subject is a Clerk id that
only exists once the Clerk user or machine has been created. That is why this
is a script rather than a migration: migration 023 creates the tables, the
issuer and the role vocabulary, but it cannot know a ``mch_...`` id that has
not been minted yet.

Idempotent — safe to re-run. Re-running with a different role set replaces
the grants; it never deletes the principal.

Run it through uv, like everything else in this repo — bare `python` will not
see the project environment and the identity import will fail:

    uv run python scripts/seed_principal.py \
        --subject mch_deejay_cog \
        --kind machine \
        --display-name deejay-cog \
        --role catalog-ingest

    # a human admin
    uv run python scripts/seed_principal.py \
        --subject user_2abc... --kind human --role wcs-admin --role wcs-reader

DATABASE_URL must point at the database you intend to write to. There is no
guard here against pointing it at production by accident, so use --dry-run
first: it resolves everything and prints the change without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import uuid

from identity.store import Issuer, Principal, PrincipalRole, Role
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from kaianolevine_api.config import get_settings
from kaianolevine_api.database import get_engine

DEFAULT_ISSUER = "https://clerk.kaianolevine.com"


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Seed a principal in the identity store.")
    p.add_argument("--issuer", default=DEFAULT_ISSUER)
    p.add_argument("--subject", required=True, help="Clerk sub, e.g. mch_deejay_cog")
    p.add_argument("--kind", required=True, choices=["human", "machine"])
    p.add_argument("--display-name", default="")
    p.add_argument("--email", default=None)
    p.add_argument(
        "--role",
        action="append",
        default=[],
        dest="roles",
        help="Role name; repeat for several.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing.",
    )
    return p.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = get_settings()
    engine = get_engine(settings)
    maker = async_sessionmaker(engine, expire_on_commit=False)

    async with maker() as session:
        issuer = (
            (await session.execute(select(Issuer).where(Issuer.issuer == args.issuer)))
            .scalars()
            .first()
        )
        if issuer is None:
            print(
                f"ERROR: issuer {args.issuer!r} is not registered. "
                "Run migration 023 first, or add the issuer explicitly.",
                file=sys.stderr,
            )
            return 2

        # Unknown roles are refused rather than silently ignored. In the
        # decision path an unknown role is deliberately skipped, which is the
        # right behaviour at request time and the wrong one here: a typo would
        # produce a principal that authenticates and can do nothing.
        for name in args.roles:
            exists = (
                (await session.execute(select(Role).where(Role.name == name)))
                .scalars()
                .first()
            )
            if exists is None:
                print(f"ERROR: role {name!r} does not exist.", file=sys.stderr)
                return 2

        principal = (
            (
                await session.execute(
                    select(Principal).where(
                        Principal.issuer == args.issuer,
                        Principal.subject == args.subject,
                    )
                )
            )
            .scalars()
            .first()
        )

        if principal is None:
            principal = Principal(
                id=uuid.uuid4(),
                kind=args.kind,
                issuer=args.issuer,
                subject=args.subject,
                display_name=args.display_name,
                email=args.email,
            )
            action = "create"
        else:
            principal.kind = args.kind
            if args.display_name:
                principal.display_name = args.display_name
            if args.email is not None:
                principal.email = args.email
            action = "update"

        existing = {
            r
            for r in (
                await session.execute(
                    select(PrincipalRole.role_name).where(
                        PrincipalRole.principal_id == principal.id
                    )
                )
            )
            .scalars()
            .all()
        }
        wanted = set(args.roles)

        print(f"{action} principal {args.subject} ({args.kind}) on {args.issuer}")
        print(f"  id:      {principal.id}")
        print(f"  roles:   {sorted(existing)} -> {sorted(wanted)}")

        if args.dry_run:
            print("dry run — nothing written")
            return 0

        session.add(principal)
        await session.flush()

        for name in wanted - existing:
            session.add(
                PrincipalRole(
                    principal_id=principal.id,
                    role_name=name,
                    granted_by="seed_principal.py",
                )
            )
        for name in existing - wanted:
            row = (
                (
                    await session.execute(
                        select(PrincipalRole).where(
                            PrincipalRole.principal_id == principal.id,
                            PrincipalRole.role_name == name,
                        )
                    )
                )
                .scalars()
                .first()
            )
            if row is not None:
                await session.delete(row)

        await session.commit()
        print("done")
        return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_run(_parse_args(argv if argv is not None else sys.argv[1:])))


if __name__ == "__main__":
    raise SystemExit(main())
