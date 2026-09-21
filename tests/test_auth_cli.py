from __future__ import annotations

from app.commands import cli


class StatusClient:
    def __init__(self) -> None:
        self.connected = False
        self.send_code_requests = 0

    async def connect(self) -> None:
        self.connected = True

    async def disconnect(self) -> None:
        self.connected = False

    def is_connected(self) -> bool:
        return self.connected

    async def is_user_authorized(self) -> bool:
        return False

    async def send_code_request(self, phone: str) -> None:
        self.send_code_requests += 1


async def test_auth_status_never_requests_a_login_code(settings, monkeypatch) -> None:
    client = StatusClient()
    monkeypatch.setattr(cli, "create_client", lambda _: client)

    lines, connected, authorized = await cli._telegram_status(settings)

    assert connected is True
    assert authorized is False
    assert "Authorized: no" in lines
    assert client.send_code_requests == 0
