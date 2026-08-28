import hashlib
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import uuid4

import jwt
from pydantic import BaseModel, ConfigDict, ValidationError

ALGORITHM = "HS256"
TICKET_TTL_SECONDS = 60
TICKET_PURPOSE = "task_events"
CONVERSATION_TICKET_PURPOSE = "conversation_events"


class StreamTicketError(ValueError):
    pass


class ConversationStreamTicketError(ValueError):
    """A conversation ticket is invalid, expired, replayed, or out of scope."""


class ConversationStreamUnavailable(RuntimeError):
    """Conversation ticket signing or replay protection is unavailable."""


class StreamTicketClaims(BaseModel):
    sub: str
    task_id: str
    exp: int
    jti: str
    purpose: str


class ConversationStreamTicketClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sub: str
    conversation_id: str
    exp: int
    jti: str
    purpose: str


class TicketReplayStore(Protocol):
    def consume(self, jti_hash: str, ttl_seconds: int) -> bool: ...


class FakeTicketReplayStore:
    """In-memory single-use store for tests; it never stores the raw jti."""

    def __init__(self) -> None:
        self._consumed: set[str] = set()

    def consume(self, jti_hash: str, ttl_seconds: int) -> bool:
        del ttl_seconds
        if jti_hash in self._consumed:
            return False
        self._consumed.add(jti_hash)
        return True


class RedisTicketReplayStore:
    def __init__(self, client) -> None:
        self.client = client

    def consume(self, jti_hash: str, ttl_seconds: int) -> bool:
        return bool(
            self.client.set(
                f"secagent:stream-ticket:{jti_hash}",
                "1",
                nx=True,
                ex=ttl_seconds,
            )
        )


class StreamTicketService:
    def __init__(
        self,
        signing_key: str | None,
        replay_store: TicketReplayStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.signing_key = signing_key
        self.replay_store = replay_store
        self.now = now or (lambda: datetime.now(timezone.utc))

    def issue(self, user_id: str, task_id: str) -> str:
        if not self.signing_key:
            raise StreamTicketError("stream ticket authentication unavailable")
        current = self.now()
        return jwt.encode(
            {
                "sub": user_id,
                "task_id": task_id,
                "exp": current + timedelta(seconds=TICKET_TTL_SECONDS),
                "jti": str(uuid4()),
                "purpose": TICKET_PURPOSE,
            },
            self.signing_key,
            algorithm=ALGORITHM,
        )

    def consume(
        self,
        ticket: str,
        *,
        task_id: str,
        expected_user_id: str | None = None,
    ) -> StreamTicketClaims:
        if not self.signing_key:
            raise StreamTicketError("stream ticket authentication unavailable")
        try:
            payload = jwt.decode(
                ticket,
                self.signing_key,
                algorithms=[ALGORITHM],
                options={"require": ["sub", "task_id", "exp", "jti", "purpose"]},
            )
            claims = StreamTicketClaims.model_validate(payload)
        except (jwt.PyJWTError, ValidationError) as exc:
            raise StreamTicketError("invalid or expired stream ticket") from exc
        if claims.purpose != TICKET_PURPOSE:
            raise StreamTicketError("invalid stream ticket purpose")
        if claims.task_id != task_id:
            raise StreamTicketError("stream ticket task mismatch")
        if expected_user_id is not None and claims.sub != expected_user_id:
            raise StreamTicketError("stream ticket user mismatch")
        digest = hashlib.sha256(claims.jti.encode()).hexdigest()
        if not self.replay_store.consume(digest, TICKET_TTL_SECONDS):
            raise StreamTicketError("stream ticket replay rejected")
        return claims


class ConversationStreamTicketService:
    """Single-use conversation event tickets with independent claims and errors."""

    def __init__(
        self,
        signing_key: str | None,
        replay_store: TicketReplayStore,
        *,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.signing_key = signing_key
        self.replay_store = replay_store
        self.now = now or (lambda: datetime.now(timezone.utc))

    def issue(self, user_id: str, conversation_id: str) -> str:
        if not self.signing_key:
            raise ConversationStreamUnavailable(
                "conversation event streaming is unavailable"
            )
        current = self.now()
        try:
            return jwt.encode(
                {
                    "sub": user_id,
                    "conversation_id": conversation_id,
                    "exp": current + timedelta(seconds=TICKET_TTL_SECONDS),
                    "jti": str(uuid4()),
                    "purpose": CONVERSATION_TICKET_PURPOSE,
                },
                self.signing_key,
                algorithm=ALGORITHM,
            )
        except Exception as exc:
            raise ConversationStreamUnavailable(
                "conversation event streaming is unavailable"
            ) from exc

    def consume(
        self,
        ticket: str,
        *,
        conversation_id: str,
        expected_user_id: str | None = None,
    ) -> ConversationStreamTicketClaims:
        if not self.signing_key:
            raise ConversationStreamUnavailable(
                "conversation event streaming is unavailable"
            )
        try:
            payload = jwt.decode(
                ticket,
                self.signing_key,
                algorithms=[ALGORITHM],
                options={
                    "require": [
                        "sub",
                        "conversation_id",
                        "exp",
                        "jti",
                        "purpose",
                    ]
                },
            )
            claims = ConversationStreamTicketClaims.model_validate(payload)
        except (jwt.PyJWTError, ValidationError) as exc:
            raise ConversationStreamTicketError(
                "invalid or expired conversation stream ticket"
            ) from exc
        except Exception as exc:
            raise ConversationStreamUnavailable(
                "conversation event streaming is unavailable"
            ) from exc
        if claims.purpose != CONVERSATION_TICKET_PURPOSE:
            raise ConversationStreamTicketError(
                "invalid conversation stream ticket purpose"
            )
        if claims.conversation_id != conversation_id:
            raise ConversationStreamTicketError(
                "conversation stream ticket resource mismatch"
            )
        if expected_user_id is not None and claims.sub != expected_user_id:
            raise ConversationStreamTicketError(
                "conversation stream ticket user mismatch"
            )
        digest = hashlib.sha256(claims.jti.encode()).hexdigest()
        try:
            consumed = self.replay_store.consume(digest, TICKET_TTL_SECONDS)
        except Exception as exc:
            raise ConversationStreamUnavailable(
                "conversation event streaming is unavailable"
            ) from exc
        if not consumed:
            raise ConversationStreamTicketError(
                "conversation stream ticket replay rejected"
            )
        return claims
