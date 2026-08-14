from typing import Annotated, Literal

from fastapi import APIRouter, Cookie, Depends, HTTPException, Response, status
from pydantic import BaseModel

from secagent.auth.dependencies import auth_service
from secagent.services.auth_service import (
    AuthenticationConfigurationError,
    AuthenticationError,
    AuthResult,
    AuthService,
    UserRead,
)


router = APIRouter(prefix="/api/auth", tags=["auth"])
REFRESH_COOKIE = "refresh_token"


class LoginRequest(BaseModel):
    username: str
    password: str


class AuthResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"
    expires_in: int
    user: UserRead


@router.post("/login", response_model=AuthResponse)
def login(
    payload: LoginRequest,
    response: Response,
    service: Annotated[AuthService, Depends(auth_service)],
) -> AuthResponse:
    try:
        result = service.login(payload.username, payload.password)
    except AuthenticationError:
        raise _unauthorized("Invalid username or password") from None
    except AuthenticationConfigurationError:
        raise _unavailable() from None
    _set_refresh_cookie(response, service, result.refresh_token)
    return _response(service, result)


@router.post("/refresh", response_model=AuthResponse)
def refresh(
    response: Response,
    service: Annotated[AuthService, Depends(auth_service)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> AuthResponse:
    if refresh_token is None:
        raise _unauthorized("Invalid refresh token")
    try:
        result = service.refresh(refresh_token)
    except AuthenticationError:
        raise _unauthorized("Invalid refresh token") from None
    except AuthenticationConfigurationError:
        raise _unavailable() from None
    _set_refresh_cookie(response, service, result.refresh_token)
    return _response(service, result)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    response: Response,
    service: Annotated[AuthService, Depends(auth_service)],
    refresh_token: Annotated[str | None, Cookie(alias=REFRESH_COOKIE)] = None,
) -> None:
    service.logout(refresh_token)
    response.delete_cookie(
        REFRESH_COOKIE,
        httponly=True,
        samesite="strict",
        secure=service.settings.cookie_secure,
        path="/api/auth",
    )


def _response(service: AuthService, result: AuthResult) -> AuthResponse:
    return AuthResponse(
        access_token=result.access_token,
        expires_in=service.settings.jwt_access_minutes * 60,
        user=result.user,
    )


def _set_refresh_cookie(
    response: Response, service: AuthService, refresh_token: str
) -> None:
    response.set_cookie(
        REFRESH_COOKIE,
        refresh_token,
        httponly=True,
        samesite="strict",
        secure=service.settings.cookie_secure,
        path="/api/auth",
        max_age=service.settings.jwt_refresh_days * 86400,
    )


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)


def _unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication unavailable",
    )
