"""The production-owned TLS client refuses control and RFB redirects."""

import asyncio
import socket
import ssl
import time
from types import SimpleNamespace

import pytest
from aiohttp import web

from foxprofile_ext.dto import BrowserRunRefDTO
from foxprofile_ext.workers.agent import _ManualGate
from tests.test_ext_worker_agent import _agent
from tests.test_ext_worker_roundtrip import _write_self_signed_certificate


@pytest.mark.parametrize("operation", ["control", "relay"])
def test_owned_worker_session_refuses_redirect_before_second_origin(
    tmp_path, monkeypatch, operation
):
    certificate, key = _write_self_signed_certificate(tmp_path)
    monkeypatch.setenv("SSL_CERT_FILE", str(certificate))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    runtime = SimpleNamespace(
        ledger=SimpleNamespace(active_runs=lambda: ()), shutdown=lambda *_: None
    )
    agent = _agent(tmp_path, runtime=runtime)

    async def exercise():
        connections, redirects, failures = [], [], []

        async def destination(request):
            connections.append(request.path)
            websocket = web.WebSocketResponse()
            await websocket.prepare(request)
            await websocket.receive(timeout=2)
            if operation == "control":
                await websocket.send_json(
                    {
                        "type": "welcome",
                        "worker_id": agent.worker_id,
                        "connection_generation": 1,
                        "heartbeat_interval": 1,
                        "loss_timeout": 10,
                    }
                )
            await websocket.close()
            return websocket

        async def serve(handler):
            app = web.Application()
            app.router.add_get("/{path:.*}", handler)
            runner = web.AppRunner(app)
            await runner.setup()
            listener = socket.socket()
            listener.bind(("127.0.0.1", 0))
            address = f"127.0.0.1:{listener.getsockname()[1]}"
            await web.SockSite(runner, listener, ssl_context=tls).start()
            return runner, address

        destination_runner, destination_address = await serve(destination)

        async def redirect(request):
            redirects.append(request.path)
            raise web.HTTPFound(f"https://{destination_address}/sink")

        source_runner, source_address = await serve(redirect)
        agent.master_url = f"wss://{source_address}"
        original_connect = agent._connect_once
        left, right = socket.socketpair()
        right.sendall(b"fixture-rfb")
        gate = _ManualGate(
            "fixture-session",
            BrowserRunRefDTO("fixture-profile", "fixture-run"),
            None,
            {"url": f"wss://{source_address}/relay", "token": "fixture-relay-token"},
            time.time() + 10,
            time.time() + 10,
            time.time() + 10,
        )
        gate.backend_session = SimpleNamespace(channel=SimpleNamespace(socket=left))

        async def connect_once():
            try:
                if operation == "control":
                    await original_connect()
                else:
                    await agent._relay_manual(gate)
            except ConnectionError as error:
                failures.append(str(error))
            finally:
                await agent.stop()

        monkeypatch.setattr(agent, "_connect_once", connect_once)
        try:
            await asyncio.wait_for(agent.run(), 5)
            assert len(redirects) == 1
            assert connections == []
            assert failures == ["Worker transport redirects are disabled"]
        finally:
            left.close()
            right.close()
            await source_runner.cleanup()
            await destination_runner.cleanup()

    asyncio.run(exercise())
