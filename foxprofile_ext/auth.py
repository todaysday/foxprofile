"""Personal account authentication and administrator use cases."""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import dataclass, field

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError

from .dto import PrincipalDTO, PrincipalKind, Role
from .errors import DomainError, ErrorCode
from .policy import require_authenticated
from .storage.dao import (
    AccountRecord,
    AssignmentDAO,
    LastAdministratorError,
    SessionDAO,
    StorageConflict,
    UserDAO,
)

SESSION_TTL_SECONDS = 12 * 60 * 60
MAX_USERNAME_LENGTH = 128
MAX_PASSWORD_BYTES = 1024
_CSRF_CONTEXT = b"foxprofile/csrf/v1"
_PASSWORD_HASHER = PasswordHasher()


@dataclass(frozen=True, slots=True)
class UserDTO:
    """Public account data; authentication material never belongs here."""

    user_id: str
    username: str
    role: Role
    active: bool


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """Private adapter result. Only session cookie and CSRF values leave service."""

    user: UserDTO
    principal: PrincipalDTO = field(repr=False)
    session_token: str = field(repr=False)
    csrf_token: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class AuthenticatedSession:
    """Verified request context; the CSRF value is excluded from repr and DTOs."""

    user: UserDTO
    principal: PrincipalDTO
    csrf_token: str = field(repr=False)


def hash_password(password: str) -> str:
    """Hash one password with Argon2id and reject empty or unbounded input."""
    _validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def _validate_username(username: str) -> str:
    if not isinstance(username, str):
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    normalized = username.strip()
    if not normalized or len(normalized) > MAX_USERNAME_LENGTH:
        raise DomainError(ErrorCode.VALIDATION_ERROR)
    try:
        normalized.encode("utf-8")
    except UnicodeEncodeError:
        raise DomainError(ErrorCode.VALIDATION_ERROR) from None
    return normalized


def _valid_password(password: object) -> bool:
    if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_BYTES:
        return False
    try:
        return len(password.encode("utf-8")) <= MAX_PASSWORD_BYTES
    except UnicodeEncodeError:
        return False


def _validate_password(password: str) -> None:
    if not _valid_password(password):
        raise DomainError(ErrorCode.VALIDATION_ERROR)


def _role(value: Role | str) -> Role:
    try:
        return Role(value)
    except (TypeError, ValueError):
        raise DomainError(ErrorCode.VALIDATION_ERROR) from None


def _user_dto(account: AccountRecord) -> UserDTO:
    return UserDTO(account.user_id, account.username, account.role, account.active)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _csrf_token(token: str) -> str:
    return hmac.new(token.encode("ascii"), _CSRF_CONTEXT, hashlib.sha256).hexdigest()


def _valid_token(token: object) -> bool:
    return isinstance(token, str) and 32 <= len(token) <= 128 and token.isascii()


def _auth_required() -> DomainError:
    return DomainError(ErrorCode.AUTH_REQUIRED)


class AuthService:
    """Authenticate personal sessions and enforce account-admin authority.

    Profile operations belong to the later authenticated application facade.
    Service principals remain a separate kind and can never administer users.
    """

    def __init__(
        self,
        users: UserDAO,
        sessions: SessionDAO,
        assignments: AssignmentDAO | None = None,
        profile_exists: Callable[[str], bool] | None = None,
        *,
        clock: Callable[[], float] = time.time,
        session_ttl: int = SESSION_TTL_SECONDS,
    ) -> None:
        if (assignments is None) != (profile_exists is None):
            raise TypeError("assignments and profile_exists must be supplied together")
        if type(session_ttl) is not int or session_ttl < 1:
            raise ValueError("session_ttl must be a positive integer")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._users = users
        self._sessions = sessions
        self._assignments = assignments
        self._profile_exists = profile_exists
        self._clock = clock
        self._session_ttl = session_ttl
        self._dummy_hash = _PASSWORD_HASHER.hash(secrets.token_urlsafe(32))

    def login(self, username: str, password: str) -> IssuedSession:
        """Return private cookie material only after generic credential checks."""
        try:
            lookup_name = _validate_username(username)
            valid_username = True
        except DomainError:
            lookup_name = ""
            valid_username = False
        valid_input = valid_username and _valid_password(password)
        account = self._users.get_by_username(lookup_name) if lookup_name else None
        use_account_hash = account is not None and account.active
        encoded_hash = account.password_hash if use_account_hash else self._dummy_hash
        candidate = password if valid_input else ""
        matches = False
        with suppress(VerificationError):
            matches = _PASSWORD_HASHER.verify(encoded_hash, candidate)
        if not valid_input or account is None or not account.active or not matches:
            raise _auth_required()

        now = self._now()
        session_token = secrets.token_urlsafe(32)
        session_id = secrets.token_urlsafe(18)
        try:
            self._sessions.create(
                session_id,
                account.user_id,
                _token_hash(session_token),
                now,
                now + self._session_ttl,
                expected_auth_version=account.auth_version,
            )
        except StorageConflict:
            raise _auth_required() from None
        principal = PrincipalDTO(
            account.user_id,
            PrincipalKind.PERSONAL,
            account.role,
            session_id,
        )
        return IssuedSession(
            _user_dto(account), principal, session_token, _csrf_token(session_token)
        )

    def resolve_session(self, session_token: str) -> AuthenticatedSession:
        """Resolve cookie token hash to live user authority and its CSRF value."""
        if not _valid_token(session_token):
            raise _auth_required()
        record = self._sessions.get_active(_token_hash(session_token), self._now())
        if record is None:
            raise _auth_required()
        account = self._users.get(record.user_id)
        if account is None or not account.active:
            raise _auth_required()
        principal = PrincipalDTO(
            account.user_id,
            PrincipalKind.PERSONAL,
            account.role,
            record.session_id,
        )
        return AuthenticatedSession(_user_dto(account), principal, _csrf_token(session_token))

    def validate_principal(self, principal: PrincipalDTO | None) -> PrincipalDTO:
        """Recheck personal-session revocation for direct service/Flet calls."""
        principal = require_authenticated(principal)
        if principal.kind is PrincipalKind.SERVICE:
            return principal
        self._personal_account(principal)
        return principal

    def current_user(self, principal: PrincipalDTO | None) -> UserDTO:
        return _user_dto(self._personal_account(principal))

    def logout(self, principal: PrincipalDTO | None) -> None:
        principal = require_authenticated(principal)
        if principal.kind is not PrincipalKind.PERSONAL:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        self._personal_account(principal)
        self._sessions.revoke(principal.session_id)

    def csrf_token_for_session(self, session_token: str) -> str:
        """Return a deterministic per-session CSRF value after validating cookie."""
        return self.resolve_session(session_token).csrf_token

    def verify_csrf(self, session_token: str, presented: str | None) -> bool:
        """Validate session liveness, then compare the session-bound CSRF value."""
        context = self.resolve_session(session_token)
        return isinstance(presented, str) and hmac.compare_digest(context.csrf_token, presented)

    def list_users(self, principal: PrincipalDTO | None) -> tuple[UserDTO, ...]:
        self._require_admin(principal)
        return tuple(_user_dto(account) for account in self._users.list_accounts())

    def create_user(
        self,
        principal: PrincipalDTO | None,
        username: str,
        password: str,
        role: Role | str = Role.USER,
    ) -> UserDTO:
        self._require_admin(principal)
        username = _validate_username(username)
        password_hash = hash_password(password)
        selected_role = _role(role)
        try:
            account = self._users.create(
                secrets.token_urlsafe(18), username, password_hash, selected_role
            )
        except sqlite3.IntegrityError:
            raise DomainError(ErrorCode.VALIDATION_ERROR) from None
        return _user_dto(account)

    def update_user_state(
        self,
        principal: PrincipalDTO | None,
        user_id: str,
        *,
        role: Role | str | None = None,
        active: bool | None = None,
    ) -> UserDTO:
        self._require_admin(principal)
        if role is None and active is None:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if active is not None and type(active) is not bool:
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        selected_role = _role(role) if role is not None else None
        try:
            account = self._users.update_state(user_id, role=selected_role, active=active)
        except LastAdministratorError:
            raise DomainError(ErrorCode.VALIDATION_ERROR) from None
        except StorageConflict:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
        return _user_dto(account)

    def reset_password(
        self, principal: PrincipalDTO | None, user_id: str, password: str
    ) -> UserDTO:
        self._require_admin(principal)
        password_hash = hash_password(password)
        try:
            account = self._users.set_password_hash(user_id, password_hash)
        except StorageConflict:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
        return _user_dto(account)

    def replace_assignments(
        self,
        principal: PrincipalDTO | None,
        user_id: str,
        profile_ids: Iterable[str],
    ) -> tuple[str, ...]:
        self._require_admin(principal)
        if self._assignments is None or self._profile_exists is None:
            raise RuntimeError("assignment services are not configured")
        if self._users.get(user_id) is None:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        try:
            ids = tuple(profile_ids)
        except TypeError:
            raise DomainError(ErrorCode.VALIDATION_ERROR) from None
        if any(not isinstance(profile_id, str) or not profile_id.strip() for profile_id in ids):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if len(ids) != len(set(ids)):
            raise DomainError(ErrorCode.VALIDATION_ERROR)
        if not all(self._profile_exists(profile_id) for profile_id in ids):
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        try:
            self._assignments.replace_for_user(user_id, ids)
        except sqlite3.IntegrityError:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND) from None
        return ids

    def _personal_account(self, principal: PrincipalDTO | None) -> AccountRecord:
        principal = require_authenticated(principal)
        if principal.kind is not PrincipalKind.PERSONAL:
            raise DomainError(ErrorCode.FORBIDDEN_OR_NOT_FOUND)
        if principal.session_id is None:
            raise _auth_required()
        session = self._sessions.get_valid_by_id(principal.session_id, self._now())
        if session is None or session.user_id != principal.subject_id:
            raise _auth_required()
        account = self._users.get(session.user_id)
        if account is None or not account.active or account.role is not principal.role:
            raise _auth_required()
        return account

    def _require_admin(self, principal: PrincipalDTO | None) -> AccountRecord:
        principal = require_authenticated(principal)
        if principal.kind is not PrincipalKind.PERSONAL:
            raise DomainError(ErrorCode.ADMIN_REQUIRED)
        account = self._personal_account(principal)
        if account.role is not Role.ADMIN:
            raise DomainError(ErrorCode.ADMIN_REQUIRED)
        return account

    def _now(self) -> float:
        now = self._clock()
        if type(now) not in (int, float) or not math.isfinite(now):
            raise RuntimeError("clock must return finite Unix seconds")
        return float(now)
