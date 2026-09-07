"""API keys — the credential another platform integrates with.

Owner/Admin only, and deliberately so: a key is full tenant scope for whatever
it can reach, so minting one is an account-level act, not something a Member
should be able to do quietly.

The gate that matters is not here — it is in `CreateApiKey`, which refuses the
free tier and refuses scopes above the plan. This router only translates.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from src.application.use_cases.api_keys import (
    CreateApiKey,
    ListApiKeys,
    RevokeApiKey,
    effective_scopes,
)
from src.domain.billing.entities import plan_for
from src.domain.tenant.entities import ApiKey
from src.interfaces.api.deps import AdminPrincipalDep, ContainerDep
from src.interfaces.api.schemas import (
    ApiKeyListResponse,
    ApiKeyResponse,
    CreateApiKeyRequest,
    CreatedApiKeyResponse,
)

router = APIRouter(prefix="/api-keys", tags=["api-keys"])


def _to_response(key: ApiKey, tier: str) -> ApiKeyResponse:
    return ApiKeyResponse(
        id=key.id,
        name=key.name,
        prefix=key.prefix,
        scopes=list(key.scopes),
        # Recomputed against the live plan rather than read off the row, so the
        # dashboard shows what the key does now, not what it did when minted.
        active_scopes=sorted(s.value for s in effective_scopes(key, tier)),
        plan_tier=key.plan_tier,
        is_active=key.is_active,
        last_used_at=key.last_used_at,
        created_at=key.created_at,
    )


@router.get("", response_model=ApiKeyListResponse)
async def list_keys(principal: AdminPrincipalDep, container: ContainerDep) -> ApiKeyListResponse:
    use_case = ListApiKeys(container.unit_of_work())
    keys, tier = await use_case.execute(principal.tenant_id)
    plan = plan_for(tier)
    return ApiKeyListResponse(
        keys=[_to_response(k, tier.value) for k in keys],
        plan_tier=tier.value,
        api_access=plan.api_access,
        agent_api=plan.agent_api,
        available_scopes=sorted(s.value for s in plan.scopes),
    )


@router.post("", response_model=CreatedApiKeyResponse, status_code=201)
async def create_key(
    body: CreateApiKeyRequest, principal: AdminPrincipalDep, container: ContainerDep
) -> CreatedApiKeyResponse:
    """Mint a key. The plaintext in this response is never retrievable again."""
    use_case = CreateApiKey(container.unit_of_work())
    issued = await use_case.execute(
        principal.tenant_id, name=body.name, requested_scopes=body.scopes
    )
    return CreatedApiKeyResponse(
        key=_to_response(issued.key, issued.key.plan_tier),
        raw_key=issued.raw_key,
    )


@router.delete("/{key_id}", status_code=204)
async def revoke_key(
    key_id: uuid.UUID, principal: AdminPrincipalDep, container: ContainerDep
) -> None:
    use_case = RevokeApiKey(container.unit_of_work())
    await use_case.execute(principal.tenant_id, key_id)
