"""Tests for the interactive Google OAuth flow (copy-paste desktop variant)."""

from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from app.google_oauth import (
    REDIRECT_URI,
    GoogleOAuthError,
    build_auth_url,
    exchange_code,
    extract_auth_code,
    fetch_calendar_list,
)

CLIENT_ID = "12345.apps.googleusercontent.com"
CLIENT_SECRET = "top-secret"
TOKEN_URL = "https://oauth2.googleapis.com/token"


class TestBuildAuthUrl:
    def test_contains_all_required_params(self) -> None:
        url = build_auth_url(CLIENT_ID)
        parts = urlsplit(url)
        assert parts.scheme == "https"
        assert parts.netloc == "accounts.google.com"
        params = {key: value[0] for key, value in parse_qs(parts.query).items()}
        assert params["client_id"] == CLIENT_ID
        assert params["redirect_uri"] == REDIRECT_URI
        assert params["response_type"] == "code"
        assert params["scope"] == "https://www.googleapis.com/auth/calendar.readonly"
        assert params["access_type"] == "offline"
        assert params["prompt"] == "consent"

    def test_redirect_uri_is_unreachable_localhost(self) -> None:
        # Port 1 is never served: the user copies the URL from the address
        # bar of the failed redirect instead of a local callback server.
        assert REDIRECT_URI == "http://localhost:1/"


class TestExtractAuthCode:
    def test_full_redirect_url(self) -> None:
        raw = "http://localhost:1/?code=4/0AbCdEf&scope=https://www.googleapis.com/auth/calendar.readonly"
        assert extract_auth_code(raw) == "4/0AbCdEf"

    def test_url_encoded_code_in_url_is_decoded(self) -> None:
        raw = "http://localhost:1/?code=4%2F0AbCdEf&scope=x"
        assert extract_auth_code(raw) == "4/0AbCdEf"

    def test_query_fragment_without_host(self) -> None:
        assert extract_auth_code("?code=4/0AbCdEf&scope=x") == "4/0AbCdEf"
        assert extract_auth_code("code=4/0AbCdEf") == "4/0AbCdEf"

    def test_raw_code_passes_through(self) -> None:
        assert extract_auth_code("4/0AbCdEf") == "4/0AbCdEf"

    def test_raw_percent_encoded_code_is_decoded(self) -> None:
        assert extract_auth_code("4%2F0AbCdEf") == "4/0AbCdEf"

    def test_surrounding_whitespace_is_stripped(self) -> None:
        assert extract_auth_code("  4/0AbCdEf\n") == "4/0AbCdEf"

    def test_empty_input_raises_german_error(self) -> None:
        with pytest.raises(GoogleOAuthError, match="Code"):
            extract_auth_code("   ")

    def test_url_without_code_param_raises(self) -> None:
        with pytest.raises(GoogleOAuthError, match="Code"):
            extract_auth_code("http://localhost:1/?error=access_denied")


def token_client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.anyio
class TestExchangeCode:
    async def test_successful_exchange_returns_persistable_tokens(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "access_token": "at-1",
                    "refresh_token": "rt-1",
                    "expires_in": 3599,
                    "scope": "https://www.googleapis.com/auth/calendar.readonly",
                    "token_type": "Bearer",
                },
            )

        async with token_client(handler) as client:
            tokens = await exchange_code(
                "4/0AbCdEf", client_id=CLIENT_ID, client_secret=CLIENT_SECRET, client=client
            )

        assert str(captured[0].url) == TOKEN_URL
        form = parse_qs(captured[0].content.decode())
        assert form["grant_type"] == ["authorization_code"]
        assert form["code"] == ["4/0AbCdEf"]
        assert form["redirect_uri"] == [REDIRECT_URI]
        assert form["client_id"] == [CLIENT_ID]
        assert form["client_secret"] == [CLIENT_SECRET]
        # The token dict must be directly usable by app.sources.google
        # (refresh flow reads client_id/client_secret/refresh_token).
        assert tokens["client_id"] == CLIENT_ID
        assert tokens["client_secret"] == CLIENT_SECRET
        assert tokens["refresh_token"] == "rt-1"
        assert tokens["access_token"] == "at-1"
        assert "access_token_expires_at" in tokens

    async def test_expired_code_gives_german_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(400, json={"error": "invalid_grant"})

        async with token_client(handler) as client:
            with pytest.raises(GoogleOAuthError, match="abgelaufen"):
                await exchange_code(
                    "4/old", client_id=CLIENT_ID, client_secret=CLIENT_SECRET, client=client
                )

    async def test_wrong_credentials_give_german_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": "invalid_client"})

        async with token_client(handler) as client:
            with pytest.raises(GoogleOAuthError, match="Client-ID"):
                await exchange_code(
                    "4/x", client_id="falsch", client_secret="falsch", client=client
                )

    async def test_missing_refresh_token_gives_german_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"access_token": "at-1", "expires_in": 3599})

        async with token_client(handler) as client:
            with pytest.raises(GoogleOAuthError, match="Refresh-Token"):
                await exchange_code(
                    "4/x", client_id=CLIENT_ID, client_secret=CLIENT_SECRET, client=client
                )

    async def test_error_message_never_contains_the_secret(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, text=f"boom {CLIENT_SECRET}")

        async with token_client(handler) as client:
            with pytest.raises(GoogleOAuthError) as excinfo:
                await exchange_code(
                    "4/x", client_id=CLIENT_ID, client_secret=CLIENT_SECRET, client=client
                )
        assert CLIENT_SECRET not in str(excinfo.value)


@pytest.mark.anyio
class TestFetchCalendarList:
    async def test_returns_id_and_name(self) -> None:
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"id": "marina@example.com", "summary": "Marina", "primary": True},
                        {"id": "abc123@group.calendar.google.com", "summary": "Verein"},
                    ]
                },
            )

        async with token_client(handler) as client:
            calendars = await fetch_calendar_list("at-1", client=client)

        assert captured[0].headers["Authorization"] == "Bearer at-1"
        assert calendars == [
            {"id": "marina@example.com", "name": "Marina"},
            {"id": "abc123@group.calendar.google.com", "name": "Verein"},
        ]

    async def test_follows_pagination(self) -> None:
        pages = iter(
            [
                {"items": [{"id": "a", "summary": "A"}], "nextPageToken": "p2"},
                {"items": [{"id": "b", "summary": "B"}]},
            ]
        )
        captured: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=next(pages))

        async with token_client(handler) as client:
            calendars = await fetch_calendar_list("at-1", client=client)

        assert [calendar["id"] for calendar in calendars] == ["a", "b"]
        assert dict(captured[1].url.params)["pageToken"] == "p2"

    async def test_unauthorized_gives_german_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(401, json={"error": {"code": 401}})

        async with token_client(handler) as client:
            with pytest.raises(GoogleOAuthError, match="Kalenderliste"):
                await fetch_calendar_list("bad", client=client)
