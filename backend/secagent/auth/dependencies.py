from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from secagent.domain import UserRole
from secagent.services.auth_service import (
    AuthenticationConfigurationError,
    AuthenticationError,
    AuthService,
)


class AuthenticatedUser(BaseModel):
    id: str
    username: str
    role: UserRole


_bearer = HTTPBearer(auto_error=False)


def auth_service(request: Request) -> Iterator[AuthService]:
    with request.app.state.session_factory() as session:
        yield AuthService.from_session(session, request.app.state.settings)


def current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None, Depends(_bearer)
    ],
    service: Annotated[AuthService, Depends(auth_service)],
) -> AuthenticatedUser:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise _unauthorized()
    try:
        user = service.authenticate_access(credentials.credentials)
    except AuthenticationError:
        raise _unauthorized() from None
    except AuthenticationConfigurationError:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication unavailable",
        ) from None
    return AuthenticatedUser.model_validate(user.model_dump())


def require_admin(
    actor: Annotated[AuthenticatedUser, Depends(current_user)],
) -> AuthenticatedUser:
    if actor.role is not UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Administrator required"
        )
    return actor


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid authentication credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
