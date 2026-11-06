"""Supported WebSocket transport and its bounded dead-peer detection."""

from .config import extension_enabled

PING_INTERVAL_SECONDS = 10
PING_TIMEOUT_SECONDS = 10
DETECTION_BUDGET_SECONDS = PING_INTERVAL_SECONDS + PING_TIMEOUT_SECONDS


def websocket_transport_options() -> dict:
    """Keep legacy defaults; protected mode requires immediate Ping-timeout closure."""
    if not extension_enabled():
        return {}
    message = (
        "Protected mode requires uvicorn[standard]>=0.54.0 with WebSocket keepalive; "
        "install requirements-extension.txt."
    )
    try:
        from uvicorn.protocols.websockets.websockets_sansio_impl import WebSocketsSansIOProtocol
    except ImportError:
        raise RuntimeError(message) from None
    if not callable(getattr(WebSocketsSansIOProtocol, "keepalive_timeout", None)):
        raise RuntimeError(message)
    return {
        "ws": "websockets-sansio",
        "ws_ping_interval": PING_INTERVAL_SECONDS,
        "ws_ping_timeout": PING_TIMEOUT_SECONDS,
    }
