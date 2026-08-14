from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import BaseModel
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from secagent.auth.passwords import hash_password, verify_password
from secagent.auth.tokens import (
    TokenValidationError,
    decode_access_token,
    hash_refresh_token,
    issue_access_token,
    new_refresh_token,
)
from secagent.config import Settings
from secagent.domain import UserRole
from secagent.repository import AuthRepository


class AuthenticationError(ValueError):
    pass


class AuthenticationConfigurationError(RuntimeError):
    pass


class UserAlreadyExistsError(ValueError):
    pass


class UserRead(BaseModel):
    id: str
    username: str
    role: UserRole


@dataclass(frozen=True)
class AuthResult:
    access_token: str
    refresh_token: str
    user: UserRead


_dummy_password_hash = hash_password("dummy-password-not-used-for-login")


class AuthService:
    def __init__(
        self, session: Session, settings: Settings, repository: AuthRepository
    ) -> None:
        self.session = session
        self.settings = settings
        self.repository = repository

    @classmethod
    def from_session(cls, session: Session, settings: Settings) -> "AuthService":
        return cls(session, settings, AuthRepository(session))

    def create_user(self, username: str, password: str, role: UserRole) -> UserRead:
        try:
            row = self.repository.add_user(
                username=username,
                password_hash=hash_password(password),
                role=role.value,
            )
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise UserAlreadyExistsError(username) from exc
        return self._read_user(row)

    def login(self, username: str, password: str) -> AuthResult:
        user = self.repository.get_user_by_username(username)
        password_hash = user.password_hash if user is not None else _dummy_password_hash
        password_valid = verify_password(password_hash, password)
        if user is None or not password_valid or not user.is_active:
            raise AuthenticationError("invalid credentials")

        access_token = self._issue_access(user.id, user.role, self._key())
        raw_refresh, refresh_hash = new_refresh_token()
        self.repository.add_refresh_session(
            user.id,
            refresh_hash,
            datetime.now(timezone.utc)
            + timedelta(days=self.settings.jwt_refresh_days),
        )
        self.session.commit()
        return AuthResult(
            access_token=access_token,
            refresh_token=raw_refresh,
            user=self._read_user(user),
        )

    def refresh(self, raw_refresh_token: str) -> AuthResult:
        key = self._key()
        raw_replacement, replacement_hash = new_refresh_token()
        now = datetime.now(timezone.utc)
        rotated = self.repository.rotate_refresh_session(
            hash_refresh_token(raw_refresh_token),
            replacement_hash,
            now + timedelta(days=self.settings.jwt_refresh_days),
            now,
        )
        if rotated is None:
            self.session.rollback()
            raise AuthenticationError("invalid refresh token")
        _, user = rotated
        access_token = self._issue_access(user.id, user.role, key)
        self.session.commit()
        return AuthResult(
            access_token=access_token,
            refresh_token=raw_replacement,
            user=self._read_user(user),
        )

    def logout(self, raw_refresh_token: str | None) -> None:
        if raw_refresh_token:
            self.repository.revoke_refresh_session(
                hash_refresh_token(raw_refresh_token), datetime.now(timezone.utc)
            )
            self.session.commit()

    def authenticate_access(self, token: str) -> UserRead:
        try:
            claims = decode_access_token(token, self._key())
        except TokenValidationError as exc:
            raise AuthenticationError("invalid access token") from exc
        user = self.repository.get_user(claims.sub)
        if user is None or not user.is_active or user.role != claims.role:
            raise AuthenticationError("invalid access token")
        return self._read_user(user)

    def _issue_access(self, user_id: str, role: str, key: str) -> str:
        return issue_access_token(
            user_id,
            role,
            key,
            minutes=self.settings.jwt_access_minutes,
        )

    def _key(self) -> str:
        key = self.settings.jwt_key()
        if key is None:
            raise AuthenticationConfigurationError("JWT signing key is not configured")
        return key

    @staticmethod
    def _read_user(row) -> UserRead:
        return UserRead(id=row.id, username=row.username, role=UserRole(row.role))
