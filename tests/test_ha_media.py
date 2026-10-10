"""Tests for app.ha_media — deleting a media file via Home Assistant.

The add-on has no write access to /media; the unlink is delegated to HA's
``media_source/local_source/remove`` command over the Core WebSocket API.
Every WebSocket here is a fake — no test opens a connection.

The identifier tests matter most: Home Assistant uses the part after the
source id as a filesystem path WITHOUT unquoting it, so the relative path
has to be carried verbatim. The real share has folders with spaces,
umlauts and a literal ``#recycle``; percent-encoding any of them would
address a file that does not exist.
"""

import json

import pytest

from app import ha_media


class FakeWebSocket:
    """Replays canned messages and records what was sent (as parsed JSON)."""

    def __init__(self, messages: list[dict]) -> None:
        self._messages = list(messages)
        self.sent: list[dict] = []

    async def recv(self) -> str:
        if not self._messages:
            raise AssertionError("FakeWebSocket ran out of messages")
        return json.dumps(self._messages.pop(0))

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


class FakeConnect:
    """Stands in for websockets.connect; records the URL it was given."""

    def __init__(self, websocket: FakeWebSocket | None = None) -> None:
        self.websocket = websocket
        self.url: str | None = None

    def __call__(self, url: str):
        self.url = url
        return self

    async def __aenter__(self) -> FakeWebSocket:
        assert self.websocket is not None
        return self.websocket

    async def __aexit__(self, *exc_info) -> bool:
        return False


class FailingConnect(FakeConnect):
    def __call__(self, url: str):
        self.url = url
        raise OSError("connection refused")


def successful_socket() -> FakeWebSocket:
    return FakeWebSocket(
        [
            {"type": "auth_required", "ha_version": "2026.10.0"},
            {"type": "auth_ok", "ha_version": "2026.10.0"},
            {"id": 1, "type": "result", "success": True, "result": None},
        ]
    )


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch):
    for name in ("HA_WS_URL", "HA_API_TOKEN", "SUPERVISOR_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    yield


class TestMediaContentId:
    def test_plain_path(self) -> None:
        assert (
            ha_media.media_content_id("Photos/2019/IMG_1234.jpg")
            == "media-source://media_source/local/Photos/2019/IMG_1234.jpg"
        )

    def test_special_characters_are_carried_verbatim(self) -> None:
        """Spaces, umlauts, '#', '&', '+' and non-latin names go in as-is.

        HA does not unquote the identifier, so a percent-encoded path would
        make it look for a file that does not exist.
        """
        relative = "Photos/Grüne Woche #2 & mehr/Foto +1 ÄÖÜ 日本.jpg"
        content_id = ha_media.media_content_id(relative)
        assert content_id == ha_media.MEDIA_SOURCE_PREFIX + relative
        assert "%" not in content_id

    def test_recycle_folder_keeps_its_hash(self) -> None:
        content_id = ha_media.media_content_id("Photos/#recycle/old.jpg")
        assert content_id.endswith("/Photos/#recycle/old.jpg")

    def test_backslashes_are_normalized(self) -> None:
        # A path assembled on a Windows dev machine must produce the
        # identifier the Linux container would.
        assert ha_media.media_content_id(r"Photos\2019\a.jpg").endswith(
            "/Photos/2019/a.jpg"
        )

    @pytest.mark.parametrize(
        "relative",
        [
            "",
            "/Photos/a.jpg",
            "../etc/passwd",
            "Photos/../../etc/passwd",
            "Photos/./a.jpg",
            "Photos//a.jpg",
            "Photos/a\njpg",
            "Photos/a\x00.jpg",
            "x" * 2000,
        ],
    )
    def test_anything_but_a_plain_relative_path_is_refused(
        self, relative: str
    ) -> None:
        with pytest.raises(ha_media.MediaDeleteError):
            ha_media.media_content_id(relative)


class TestDeleteMedia:
    @pytest.mark.anyio
    async def test_speaks_the_ha_websocket_protocol(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        websocket = successful_socket()
        connect = FakeConnect(websocket)
        monkeypatch.setattr(ha_media, "connect", connect)

        await ha_media.delete_media("Photos/a.jpg")

        assert connect.url == "ws://supervisor/core/websocket"
        assert websocket.sent[0] == {"type": "auth", "access_token": "super-secret"}
        assert websocket.sent[1] == {
            "id": 1,
            "type": "media_source/local_source/remove",
            "media_content_id": "media-source://media_source/local/Photos/a.jpg",
        }

    @pytest.mark.anyio
    async def test_interleaved_messages_are_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {"id": 77, "type": "event", "event": {}},
                {"id": 1, "type": "result", "success": True},
            ]
        )
        monkeypatch.setattr(ha_media, "connect", FakeConnect(websocket))
        await ha_media.delete_media("Photos/a.jpg")

    @pytest.mark.anyio
    async def test_unsuccessful_result_raises_a_german_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        websocket = FakeWebSocket(
            [
                {"type": "auth_required"},
                {"type": "auth_ok"},
                {
                    "id": 1,
                    "type": "result",
                    "success": False,
                    "error": {"code": "not_found", "message": "Path does not exist"},
                },
            ]
        )
        monkeypatch.setattr(ha_media, "connect", FakeConnect(websocket))
        with pytest.raises(ha_media.MediaDeleteError) as excinfo:
            await ha_media.delete_media("Photos/a.jpg")
        assert "löschen" in str(excinfo.value)
        assert "not_found" in str(excinfo.value)
        # The foreign error text is logged, not pushed into the message.
        assert "Path does not exist" not in str(excinfo.value)

    @pytest.mark.anyio
    async def test_rejected_auth_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "wrong")
        websocket = FakeWebSocket(
            [{"type": "auth_required"}, {"type": "auth_invalid", "message": "nope"}]
        )
        monkeypatch.setattr(ha_media, "connect", FakeConnect(websocket))
        with pytest.raises(ha_media.MediaDeleteError):
            await ha_media.delete_media("Photos/a.jpg")

    @pytest.mark.anyio
    async def test_unexpected_handshake_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        websocket = FakeWebSocket([{"type": "surprise"}])
        monkeypatch.setattr(ha_media, "connect", FakeConnect(websocket))
        with pytest.raises(ha_media.MediaDeleteError):
            await ha_media.delete_media("Photos/a.jpg")

    @pytest.mark.anyio
    async def test_connection_failure_raises(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        monkeypatch.setattr(ha_media, "connect", FailingConnect())
        with pytest.raises(ha_media.MediaDeleteError):
            await ha_media.delete_media("Photos/a.jpg")

    @pytest.mark.anyio
    async def test_missing_token_fails_before_connecting(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        connect = FakeConnect(successful_socket())
        monkeypatch.setattr(ha_media, "connect", connect)
        with pytest.raises(ha_media.MediaDeleteError):
            await ha_media.delete_media("Photos/a.jpg")
        assert connect.url is None

    @pytest.mark.anyio
    async def test_an_invalid_path_never_opens_a_connection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SUPERVISOR_TOKEN", "t")
        connect = FakeConnect(successful_socket())
        monkeypatch.setattr(ha_media, "connect", connect)
        with pytest.raises(ha_media.MediaDeleteError):
            await ha_media.delete_media("../escape.jpg")
        assert connect.url is None

    @pytest.mark.anyio
    async def test_overridden_ws_url_requires_its_own_token(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The SUPERVISOR_TOKEN belongs to the default supervisor URL only.
        monkeypatch.setenv("HA_WS_URL", "ws://127.0.0.1:1/ws")
        monkeypatch.setenv("SUPERVISOR_TOKEN", "super-secret")
        connect = FakeConnect(successful_socket())
        monkeypatch.setattr(ha_media, "connect", connect)
        with pytest.raises(ha_media.MediaDeleteError):
            await ha_media.delete_media("Photos/a.jpg")
        assert connect.url is None
