"""Tenant & user aggregates.

A Tenant is the unit of isolation: every document, chatbot, and chat belongs to
exactly one tenant. A User authenticates and belongs to a tenant via a role.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from src.domain.shared.identifiers import TenantId, UserId, new_id


class Role(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    VIEWER = "viewer"


@dataclass
class Tenant:
    name: str
    slug: str
    id: TenantId = field(default_factory=lambda: TenantId(new_id()))
    daily_token_quota: int = 2_000_000
    max_documents: int = 200
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def deactivate(self) -> None:
        self.is_active = False


@dataclass
class User:
    email: str
    password_hash: str
    tenant_id: TenantId
    role: Role = Role.OWNER
    id: UserId = field(default_factory=lambda: UserId(new_id()))
    is_active: bool = True
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def can_manage(self) -> bool:
        return self.role in (Role.OWNER, Role.ADMIN)


@dataclass
class ApiKey:
    """A programmatic key scoped to a tenant. We store only the hash.

    `scopes` and `plan_tier` record what the *plan* granted at the moment the
    key was minted. They are not the authority on what the key may do today —
    `src/domain/billing/entities.py` explains why, and `deps.py` intersects
    these with the live plan on every request — but they are what the customer
    sees in the dashboard, and they are how a key issued under Scale is
    recognisable as an agent key after a downgrade.
    """

    tenant_id: TenantId
    name: str
    key_hash: str
    prefix: str  # first chars, shown in UI to identify the key
    id: uuid.UUID = field(default_factory=new_id)
    scopes: list[str] = field(default_factory=list)
    plan_tier: str = "free"
    is_active: bool = True
    # Touched (coarsely — see the repository) so a customer can tell a key that
    # is carrying traffic from one they can safely delete.
    last_used_at: datetime | None = None
    revoked_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def revoke(self) -> None:
        self.is_active = False
        self.revoked_at = datetime.now(UTC)
