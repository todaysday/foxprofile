from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException, Query, Request

from foxprofile_ext.dto import Role
from foxprofile_ext.errors import DomainError

from ...core.config import API_TOKEN
from ...services import mcp_setup
from ..dependencies import (
    _container,
    domain_http_error,
    extension_enabled,
    get_request_principal,
)
from ..mcp_http import MCP_PATH

router = APIRouter(prefix="/mcp", tags=["mcp"])


@router.get("/setup")
def setup(
    request: Request,
    base_url: str = Query(description="Address the client uses to reach FoxProfile"),
) -> dict:
    """MCP client configs for this server. The token appears as a placeholder."""
    if extension_enabled():
        principal = get_request_principal(request)
        try:
            user = _container(request).auth_service.current_user(principal)
        except DomainError as error:
            raise domain_http_error(error) from error
        if user.role is not Role.ADMIN:
            raise HTTPException(status_code=403, detail="Administrator access required")
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(status_code=400, detail="base_url must be an http(s) URL")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    mcp_url = origin + MCP_PATH
    auth_required = bool(API_TOKEN) or extension_enabled()
    token = mcp_setup.TOKEN_PLACEHOLDER if API_TOKEN else None
    return {
        "mcp_url": mcp_url,
        "auth_required": auth_required,
        "token_placeholder": mcp_setup.TOKEN_PLACEHOLDER,
        "configs": mcp_setup.as_dicts(mcp_setup.build_configs(mcp_url, token, local=False)),
    }
