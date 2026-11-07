import asyncio

from src.mcp_server import server


def test_create_profile_omits_os_type_when_unset(monkeypatch):
    requests = []

    async def capture_request(method, path, payload):
        requests.append((method, path, payload))

    monkeypatch.setattr(server, "_arequest", capture_request)

    asyncio.run(server.create_profile("default-os"))

    assert requests == [
        (
            "POST",
            "/profiles",
            {
                "name": "default-os",
                "proxy": None,
                "timezone": None,
                "locale": None,
                "engine": "camoufox",
            },
        ),
    ]


def test_create_profile_preserves_explicit_os_type(monkeypatch):
    requests = []

    async def capture_request(method, path, payload):
        requests.append((method, path, payload))

    monkeypatch.setattr(server, "_arequest", capture_request)

    asyncio.run(server.create_profile("windows-os", os_type="windows"))

    assert requests[0][2]["os_type"] == "windows"
