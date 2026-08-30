from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from secagent.auth.dependencies import AuthenticatedUser, current_user
from secagent.services.turn_control_service import (
    ApprovalConflict,
    TurnControlService,
    UnknownApproval,
)
from secagent.api.model_failures import ControlDep, ActorDep

router = APIRouter(prefix="/api/approvals", tags=["approvals"])


class ApprovalDecisionInput(BaseModel):
    model_config = {"extra": "forbid"}

    approved: bool
    reason: str = Field(default="", max_length=2000)


@router.post("/{approval_id}/decision")
def decide_subtask_approval(
    approval_id: str,
    payload: ApprovalDecisionInput,
    control: ControlDep,
    actor: ActorDep,
) -> dict:
    try:
        decided = control.decide_subtask_approval(
            approval_id,
            approved=payload.approved,
            reason=payload.reason,
            actor_id=actor.id,
        )
    except UnknownApproval as exc:
        raise HTTPException(status_code=404, detail="approval not found") from exc
    except ApprovalConflict as exc:
        raise HTTPException(
            status_code=409, detail="approval is not pending"
        ) from exc
    return {
        "id": decided.id,
        "status": decided.status,
        "reason": decided.reason,
    }
