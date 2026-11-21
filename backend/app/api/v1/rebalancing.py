"""``/api/v1/rebalancing`` — Target allocation & intelligent rebalancing (Milestone 4, PRD §F7/§F8).

Provides:
- CRUD for portfolio and account-scoped allocation targets
- Batch-set allocation targets for atomic updates
- Portfolio drift evaluation against Swedroe 5/25 tolerance heuristic
- Intelligent rebalancing optimization planning (Mode A Water-Filling and Mode B Tax-Aware)
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.api.deps import CurrentUserId, SessionDep
from app.core import clock
from app.models.allocation_target import AllocationScopeTypeStr
from app.schemas.rebalancing import (
    AllocationTargetBatchCreate,
    AllocationTargetCreate,
    AllocationTargetRead,
    AllocationTargetUpdate,
    RebalanceEvaluateRequest,
    RebalanceEvaluationRead,
    RebalancePlanRead,
    RebalancePlanRequest,
)
from app.services import rebalancing_service

router = APIRouter(prefix="/rebalancing", tags=["rebalancing"])


@router.get("/targets", response_model=list[AllocationTargetRead])
def list_allocation_targets(
    session: SessionDep,
    user_id: CurrentUserId,
    scope: AllocationScopeTypeStr = "all",
    account_id: int | None = None,
) -> list[AllocationTargetRead]:
    """List allocation targets for the current user in the requested scope."""
    targets = rebalancing_service.list_targets(
        session, user_id=user_id, scope_type=scope, account_id=account_id
    )
    return [AllocationTargetRead.model_validate(t) for t in targets]


@router.post("/targets", response_model=AllocationTargetRead, status_code=status.HTTP_201_CREATED)
def create_allocation_target(
    session: SessionDep,
    user_id: CurrentUserId,
    target_in: AllocationTargetCreate,
) -> AllocationTargetRead:
    """Create a new allocation target with scope, tenant isolation, and bps validation."""
    target = rebalancing_service.create_target(session, user_id=user_id, target_in=target_in)
    session.commit()
    return AllocationTargetRead.model_validate(target)


@router.post("/targets/batch", response_model=list[AllocationTargetRead])
def batch_set_allocation_targets(
    session: SessionDep,
    user_id: CurrentUserId,
    batch_in: AllocationTargetBatchCreate,
) -> list[AllocationTargetRead]:
    """Atomically replace all allocation targets for a given scope."""
    targets = rebalancing_service.batch_set_targets(session, user_id=user_id, batch_in=batch_in)
    session.commit()
    return [AllocationTargetRead.model_validate(t) for t in targets]


@router.get("/targets/{target_id}", response_model=AllocationTargetRead)
def get_allocation_target(
    session: SessionDep,
    user_id: CurrentUserId,
    target_id: int,
) -> AllocationTargetRead:
    """Fetch single allocation target by ID (scoped to current user)."""
    target = rebalancing_service.get_target_or_404(session, user_id=user_id, target_id=target_id)
    return AllocationTargetRead.model_validate(target)


@router.patch("/targets/{target_id}", response_model=AllocationTargetRead)
def update_allocation_target(
    session: SessionDep,
    user_id: CurrentUserId,
    target_id: int,
    target_in: AllocationTargetUpdate,
) -> AllocationTargetRead:
    """Update allocation target bps weight or threshold."""
    target = rebalancing_service.update_target(
        session, user_id=user_id, target_id=target_id, target_in=target_in
    )
    session.commit()
    return AllocationTargetRead.model_validate(target)


@router.delete("/targets/{target_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_allocation_target(
    session: SessionDep,
    user_id: CurrentUserId,
    target_id: int,
) -> Response:
    """Delete single allocation target."""
    rebalancing_service.delete_target(session, user_id=user_id, target_id=target_id)
    session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/evaluate", response_model=RebalanceEvaluationRead)
def evaluate_portfolio_drift(
    session: SessionDep,
    user_id: CurrentUserId,
    request: RebalanceEvaluateRequest,
) -> RebalanceEvaluationRead:
    """Evaluate portfolio drift against target allocation and Swedroe 5/25 tolerance heuristic."""
    return rebalancing_service.evaluate_rebalancing(
        session,
        user_id=user_id,
        scope_type=request.scope_type,
        account_id=request.account_id,
        as_of=clock.today(),
    )


@router.post("/plan", response_model=RebalancePlanRead)
def plan_rebalancing(
    session: SessionDep,
    user_id: CurrentUserId,
    request: RebalancePlanRequest,
) -> RebalancePlanRead:
    """Solve optimal cash distribution via water-filling (Mode A)
    or full tax-aware plan (Mode B).
    """
    return rebalancing_service.plan_rebalancing(
        session,
        user_id=user_id,
        scope_type=request.scope_type,
        account_id=request.account_id,
        mode=request.mode,
        inflow_paise=request.inflow_paise,
        as_of=clock.today(),
    )
