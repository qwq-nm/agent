import hashlib
from datetime import datetime, timedelta, timezone
from typing import Callable, Protocol
from uuid import uuid4

import jwt
from pydantic import BaseModel, ValidationError

ALGORITHM = "HS256"
TICKET_TTL_SECONDS = 60
TICKET_PURPOSE = "task_events"


class StreamTicketError(ValueError):
    pass


class StreamTicketClaims(BaseModel):
    sub: str
    task_id: str
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
