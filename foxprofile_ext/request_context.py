"""Trusted request principal scope for synchronous internal service adapters."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from .dto import PrincipalDTO
from .errors import DomainError, ErrorCode
from .policy import require_authenticated

_principal: ContextVar[PrincipalDTO | None] = ContextVar("foxprofile_principal", default=None)


@contextmanager
def principal_scope(principal: PrincipalDTO | None) -> Iterator[None]:
    """Set a verified principal only for one adapter call, then restore prior state."""
    actor = require_authenticated(principal)
    token = _principal.set(actor)
    try:
        yield
    finally:
        _principal.reset(token)


def current_principal() -> PrincipalDTO:
    """Return scoped identity; missing context never defaults to service access."""
    principal = _principal.get()
    if principal is None:
        raise DomainError(ErrorCode.AUTH_REQUIRED)
    return principal
