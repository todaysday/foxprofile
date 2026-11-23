"""Relay teardown across accept failure and ASGI task-group cancellation."""

import asyncio
from types import SimpleNamespace

import anyio
import pytest

from foxprofile_ext.errors import DomainError
from foxprofile_ext.workers.manual import ManualRelay


def relay_fixture():
    coordinator = SimpleNamespace(
        public_url="https://master.example.test",
        hub=SimpleNamespace(connection_for=lambda _: True),
    )
    service = ManualRelay(coordinator)
    identifier, channel, grant = service.create(object())
    return service, identifier, channel, grant, service._relays[identifier]


@pytest.mark.parametrize("accept_failure", [True, False])
def test_consumed_relay_releases_both_sockets_on_handler_exit(accept_failure):
    service, identifier, channel, grant, record = relay_fixture()

    async def scenario():
        receiving = asyncio.Event()

        class Transport:
            async def accept(self):
                if accept_failure:
                    raise ConnectionError("transport closed during accept")

            async def receive_bytes(self):
                receiving.set()
                await asyncio.Event().wait()

            async def close(self):
                pass

        if accept_failure:
            with pytest.raises(ConnectionError):
                await service.serve(Transport(), identifier, grant["token"])
        else:
            async with anyio.create_task_group() as group:
                group.start_soon(service.serve, Transport(), identifier, grant["token"])
                await asyncio.wait_for(receiving.wait(), 1)
                group.cancel_scope.cancel()

    try:
        asyncio.run(scenario())
        assert identifier not in service._relays
        assert channel.revoked
        assert channel.socket.fileno() == record.peer.fileno() == -1
    finally:
        service.revoke(identifier)


def test_invalid_relay_token_cannot_revoke_the_owners_channel():
    service, identifier, channel, _, record = relay_fixture()
    try:
        with pytest.raises(DomainError):
            asyncio.run(service.serve(object(), identifier, "invalid-token"))
        assert identifier in service._relays
        assert not record.consumed and not channel.revoked
        assert channel.socket.fileno() >= 0 and record.peer.fileno() >= 0
    finally:
        service.revoke(identifier)
