"""Agent endpoint: answer a message with the multi-step tool-using agent.

Sits alongside the single-shot `chat` router. Same auth/tenant scoping; the
response includes the agent trace (steps + tools used) so callers can inspect how
the answer was reached — the same trace the request log and eval harness consume.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter

from src.application.dtos import AgentAskInput
from src.application.use_cases.run_agent import RunAgent
from src.domain.shared.identifiers import SessionId
from src.interfaces.api.deps import ContainerDep, RunAgentPrincipalDep
from src.interfaces.api.schemas import AgentAnswerResponse, AgentStepResponse, CitationResponse

router = APIRouter(tags=["agent"])

# Every route here spends model tokens on the caller's behalf, so an API key
# needs `agent.run` — the Scale ($15/mo) scope. Signed-in dashboard users are
# unaffected; see `require_scope`.


@router.post("/sessions/{session_id}/agent", response_model=AgentAnswerResponse)
async def ask_agent(
    session_id: uuid.UUID,
    body: AgentAskInput,
    principal: RunAgentPrincipalDep,
    container: ContainerDep,
) -> AgentAnswerResponse:
    use_case = RunAgent(
        container.unit_of_work(),
        container.agent_loop,
        rate_limiter=container.agent_rate_limiter,
    )
    result = await use_case.execute(principal.tenant_id, SessionId(session_id), body)
    return AgentAnswerResponse(
        answer=result.answer,
        citations=[
            CitationResponse(
                document_id=c.document_id, ordinal=c.ordinal, score=c.score, snippet=c.snippet
            )
            for c in result.citations
        ],
        tokens_used=result.tokens_used,
        provider=result.provider,
        stop_reason=result.stop_reason,
        tools_used=result.tools_used,
        steps=[
            AgentStepResponse(
                index=s.index,
                thought=s.thought,
                action=s.action,
                observation=s.observation,
                model=s.model,
            )
            for s in result.steps
        ],
    )
