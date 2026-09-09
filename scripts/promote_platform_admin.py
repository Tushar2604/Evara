"""One-time bootstrap: grant an existing account the platform-wide Super Admin
role.

There is no UI path to create the first Super Admin — every route that could
grant the flag is itself gated behind already having it. Run this once,
directly against the target database, for the owner's own account:

    python -m scripts.promote_platform_admin owner@example.com

Uses `SqlAlchemyUnitOfWork` directly rather than the full DI container: this
is a database write, not an API request, and building the whole container
would pull in LLM/storage/email credentials this script never touches.
"""

from __future__ import annotations

import asyncio
import sys

from src.infrastructure.persistence.unit_of_work import SqlAlchemyUnitOfWork


async def promote(email: str) -> None:
    async with SqlAlchemyUnitOfWork() as uow:
        user = await uow.users.get_by_email(email.strip().lower())
        if user is None:
            print(f"No account found for {email!r}.", file=sys.stderr)
            raise SystemExit(1)
        await uow.users.set_platform_admin(user.id, True)
        await uow.commit()
    print(f"{email} is now a platform admin.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("Usage: python -m scripts.promote_platform_admin <email>", file=sys.stderr)
        raise SystemExit(1)
    asyncio.run(promote(sys.argv[1]))
