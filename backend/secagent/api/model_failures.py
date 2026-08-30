from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from secagent.auth.dependencies import AuthenticatedUser, current_user
from secagent.dag_domain import ModelFailureDecisionInput
from secagent.services.turn_control_service import (
    InvalidFailureDecision,
    ModelFailureConflict,
    TurnControlService,
    UnknownModelFailure,
)

router = APIRouter(prefix="/api/model-failures", tags=["model-failures"])


def get_turn_control_service(request: Request) -> TurnControlService:
    return TurnControlService(
        session_factory=request.app.state.session_factory,
        queue=request.app.state.job_queue,
        logical_providers=request.app.state.model_router.logical_assignment_providers(),
        max_parallel=request.app.state.settings.max_parallel_subtasks_per_conversation,
    )


ControlDep = Annotated[TurnControlService, Depends(get_turn_control_service)]
ActorDep = Annotated[AuthenticatedUser, Depends(current_user)]


@router.post("/{failure_id}/decision")
def decide_model_failure(
    failure_id: str,
    payload: ModelFailureDecisionInput,
    control: ControlDep,
    actor: ActorDep,
) -> dict:
    try:
        failure = control.resolve_failure(failure_id, payload, actor.id)
    except UnknownModelFailure as exc:
        raise HTTPException(status_code=404, detail="model failure not found") from exc
    except InvalidFailureDecision as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ModelFailureConflict as exc:
        raise HTTPException(
            status_code=409,
            detail=str(exc) or "model failure is not awaiting a decision",
        ) from exc
    return {
        "id": failure.id,
        "turn_id": failure.turn_id,
        "subtask_id": failure.subtask_id,
        "stage": failure.stage.value,
        "status": failure.status.value,
        "decision": failure.decision.value if failure.decision else None,
    }
