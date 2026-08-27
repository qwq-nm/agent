from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from secagent.conversation_domain import (
    CanonicalUUID,
    ConversationEventCreate,
    ConversationEventRead,
    canonical_json_dumps,
    canonical_json_loads,
)
from secagent.conversation_repository import ConversationRepository
from secagent.security.redaction import redact_mapping

if TYPE_CHECKING:
    from secagent.auth.dependencies import AuthenticatedUser


MAX_CONVERSATION_EVENT_PAYLOAD_BYTES = 16 * 1024


class MessageCreatedEventPayload(BaseModel):
    """The only fields permitted in a durable message-created event."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: CanonicalUUID
    message_id: CanonicalUUID
    turn_id: CanonicalUUID
    task_id: CanonicalUUID
    sequence: int = Field(ge=1, strict=True)
    plan_version: int = Field(ge=1, strict=True)
    attachment_count: int = Field(ge=0, strict=True)


def encode_redacted_event_payload(payload: object) -> str:
    """Redact recursively and return bounded canonical JSON."""

    data = payload.model_dump(mode="json") if isinstance(payload, BaseModel) else payload
    encoded = canonical_json_dumps(redact_mapping(data))
    if len(encoded.encode("utf-8")) > MAX_CONVERSATION_EVENT_PAYLOAD_BYTES:
        raise ValueError("conversation event payload exceeds 16 KiB")
    return encoded


class ConversationEventService:
    """Typed conversation event writer composed into the caller transaction."""

    def __init__(self, repository: ConversationRepository) -> None:
        self.repository = repository
        self.session = repository.session

    def append_message_created(
        self,
        actor: AuthenticatedUser,
        *,
        conversation_id: str,
        message_id: str,
        turn_id: str,
        task_id: str,
        sequence: int,
        plan_version: int,
        attachment_count: int,
    ) -> ConversationEventRead:
        whitelisted = MessageCreatedEventPayload(
            conversation_id=conversation_id,
            message_id=message_id,
            turn_id=turn_id,
            task_id=task_id,
            sequence=sequence,
            plan_version=plan_version,
            attachment_count=attachment_count,
        )
        encoded = encode_redacted_event_payload(whitelisted)
        payload = canonical_json_loads(encoded, dict[str, object])
        return self.repository.append_event(
            actor,
            whitelisted.conversation_id,
            ConversationEventCreate(
                event_type="conversation.message.created",
                payload=payload,
                turn_id=whitelisted.turn_id,
            ),
            commit=False,
        )
